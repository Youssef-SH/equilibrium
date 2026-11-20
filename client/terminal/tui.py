"""Interactive, server-authoritative EQUILIBRIUM client."""
import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll, Container
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, DataTable, Footer, Input, Label, Select, Static, TabbedContent, TabPane

from network import connect, send, save_session, load_session, default_session_path, session_data
from websockets.exceptions import WebSocketException
from presentation import Model, name, units, amount, reason, clock, effects_text, PURPOSE, SYMBOLS


class PacketError(ValueError):
    """A safely reportable wire-envelope or payload-shape error."""


def parse_packet(raw):
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PacketError("invalid JSON") from exc
    if not isinstance(message, dict) or message.get("v") != 1 or not isinstance(message.get("type"), str):
        raise PacketError("invalid envelope")
    required = {
        "seat_assigned": {"lobby_id": str, "seat": int, "rejoin_token": str},
        "lobby_created": {"join_code": str},
        "catalogue": {"foundations": dict, "buildings": dict},
        "lobby_state": {"seats": list, "host_seat": int, "phase": str},
        "draft_state": {"choices": list, "turn_seat": int, "available_foundations": list},
        "protocol_error": {"code": str},
        "action_ack": {"action_id": str, "phase": str, "status": str},
        "action_preview": {"status": str, "tick": int},
        "player_view": {"tick": int, "own_city": dict, "public": dict},
    }.get(message["type"], {})
    if any(not isinstance(message.get(key), expected) or isinstance(message.get(key), bool)
           for key, expected in required.items()):
        raise PacketError("invalid message payload")
    if message["type"] == "player_view":
        city = message["own_city"]
        if any(not isinstance(city.get(key), int) or isinstance(city.get(key), bool)
               for key in ("city_id", "population", "money_micro", "materials_micro")):
            raise PacketError("invalid city payload")
        if not isinstance(city.get("foundation"), str):
            raise PacketError("invalid city payload")
    return message


class MarketTable(DataTable):
    """A table whose cursor becomes actionable only after explicit selection."""

    def action_select_cursor(self):
        self.show_cursor = True
        return super().action_select_cursor()

HELP = """ATTRACT RESIDENTS
Four cities share 4,000 residents. Final population determines the ranking, with
authoritative tie-breaks. Satisfaction and available housing influence migration.

FIRST STEPS
Ready in the lobby, then select a unique Foundation when your turn arrives.
Choose a city tile, open Build, inspect an authoritative preview, then confirm.
Treasury is limited; recovered materials can reduce construction payments.

CIRCULAR ECONOMY
Generation -> collection -> sorted waste -> treatment or landfill.
Collection only moves waste off the street. Recyclables can recover materials.
Organic processing provides biogas electricity next tick. Residue goes to landfill.
Landfill costs money and damages Environment; Green Space only cleans air.
Reduce Waste has ongoing costs and reduces some business income and feedstock.

COMPETITION
Housing, employment, services, power and Environment contribute to Satisfaction.
A useful investment depends on the city's constraints; no purchase guarantees gains.
Development unlocks construction and upgrades; it is not the victory score.

TRADING
Sell spare processing or electricity capacity; request quotes or accept an offer.
Treatment suppliers receive recovered materials, biogas and residue obligations.
Waste rates are units/minute; electricity rates are E, billed per E-minute.
Booking fees and actual usage payments apply. Delivery can be below the agreed rate.
Quotes can only be accepted during the server's selection window.
Both participants must agree to renewal. Cancellation may refund booking fees;
the authoritative preview checks legality, not whether trading is profitable.

CONTROLS
Tab moves focus, arrows navigate lists and the city grid, Enter selects.
Escape closes a dialog. Buttons also support the mouse. All actions use the server.
Sent and queued actions are pending; only executed receipts establish success.
A preview reserves nothing and can become stale before execution.
Use Reconnect after connection loss. The city keeps running while disconnected.
After a finished match, the server must restart to host another match.
"""

WARNINGS = {
    "collection_overload": "Street waste remains. Inspect collection or Reduce Waste.",
    "recycling_capacity": "Collected recyclables remain. Compare recycling or Market treatment.",
    "organic_capacity": "Collected organics remain. Compare Biogas or Market treatment.",
    "landfill_pressure": "Landfill damage lowers Environment. Review treatment and prevention.",
    "electricity_shortage": "Power delivery is below demand. Inspect generation or Market imports.",
    "housing_shortage": "Housing limits Satisfaction. Inspect Residential capacity.",
    "services_shortage": "Services limit Satisfaction. Inspect Public Services.",
    "employment_shortage": "Employment limits Satisfaction. Inspect jobs and facility operation.",
    "building_power_constraint": "A building lacks power. Inspect Waste & Energy.",
    "building_staffing_constraint": "Staffing limits output. Inspect jobs and building demand.",
    "industrial_pollution": "Factory operation adds air pollution. Inspect cleanup and treatment.",
    "contract_under_delivery": "A contract delivers less than nominal. Inspect My Contracts.",
    "population_decline": "Residents left in the latest migration. Compare Satisfaction and rankings.",
}


class HelpDialog(ModalScreen):
    BINDINGS = [("escape", "back", "Back")]

    def compose(self):
        with VerticalScroll(id="dialog"):
            yield Static(HELP, markup=False)
            yield Button("Back", id="help-back")

    def action_back(self):
        self.dismiss()

    @on(Button.Pressed, "#help-back")
    def close_help(self, event):
        event.stop()
        self.dismiss()


class ActionDialog(ModalScreen):
    """One guided form and authoritative preview for all gameplay operations."""
    BINDINGS = [("escape", "dismiss_dialog", "Cancel")]
    def __init__(self, kind, payload=None):
        super().__init__()
        self.kind = kind
        self.payload = dict(payload or {})
        self.preview_payload = None
        self.preview = None
        self.requesting = False
        self.submitted = False
        self.revision = 0
        self.session = None
        self.market_signature = None
        self.preview_market_signature = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(name(self.kind), classes="title")
            if self.kind == "construct":
                options = [(name(k), k) for k in self.app.model.catalogue.get("buildings", {})]
                yield Select(options, prompt="Choose a building", id="building")
            if self.kind in ("publish_offer", "request_quotes", "submit_quote"):
                yield Select([(name(k), k) for k in ("recyclable", "organic", "electricity")], value=self.payload.get("type", "recyclable"), id="service", allow_blank=False)
                yield Input(placeholder="Rate: waste units/min or electricity E", id="rate", value=str(self.payload.pop("_rate", "")))
                if self.kind != "request_quotes":
                    yield Input(placeholder="Price: credits/unit or credits/E-minute", id="price")
                yield Static("", id="terms", markup=False)
            yield Static("", id="explanation", markup=False)
            yield Static("Request a preview to check the current authoritative state.", id="preview", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Check / Preview", id="check", variant="primary")
                yield Button("Confirm", id="confirm", disabled=True, variant="warning" if self.kind in ("demolish", "cancel_project", "cancel_contract") else "success")
                yield Button("Back", id="back")

    def on_mount(self):
        self.session = self.app.session_identity()
        self.market_signature = self.app.market_item_signature(self.payload.get("_market_key"))
        self.describe()

    def action_dismiss_dialog(self):
        self.dismiss()

    def describe(self):
        model = self.app.model
        lines = []
        if self.kind == "construct":
            selected = self.query_one("#building", Select).value
            if isinstance(selected, str):
                entry = model.catalogue["buildings"][selected]
                lines = [PURPOSE.get(selected, ""), effects_text(entry["l1"]), f"Development required: {units(entry['build_unlock_micro'])}", "Upgrade additions:", effects_text(entry["upgrade_delta"]), f"Upgrade Development: {units(entry['upgrade_unlock_micro'])}"]
        elif self.kind == "upgrade":
            plant = next((b for b in model.view.get("own_city", {}).get("buildings", []) if b["id"] == self.payload["building_id"]), {})
            entry = model.catalogue.get("buildings", {}).get(plant.get("type"), {})
            lines = [name(plant.get("type")), "Upgrade adds to the existing building:", effects_text(entry.get("upgrade_delta", {}))]
        elif self.kind in ("demolish", "cancel_project", "cancel_contract"):
            lines = ["Confirm this destructive action only after reviewing the preview."]
            policy = model.catalogue.get("policy", {})
            if self.kind == "demolish":
                lines.append(f"Demolition refund: {units(policy.get('demolition_refund_micro'))} credits.")
            elif self.kind == "cancel_project":
                lines.append(f"Refund: {policy.get('cancel_refund_percent', '?')}% of cash paid; materials are not refunded.")
            else:
                lines.append("Ends future service. Seller cancellation refunds the booking fee; buyer cancellation does not.")
        elif self.kind == "set_reduce_waste":
            p = model.catalogue.get("policy", {})
            lines = [f"Turn Reduce Waste {'ON' if self.payload['enabled'] else 'OFF'}.",
                     f"When funded: {units(p.get('policy_fee_micro_per_minute'))} credits/min.",
                     f"Household waste becomes {p.get('policy_household_percent', '?')}%; Factory/Commerce waste {p.get('policy_factory_percent','?')}%/{p.get('policy_commerce_percent','?')}%.",
                     f"Factory/Commerce income becomes {p.get('policy_factory_income_percent','?')}%/{p.get('policy_commerce_income_percent','?')}%.",
                     f"Cooldown: {p.get('policy_cooldown_seconds','?')} seconds. Less waste also means less recoverable feedstock."]
        elif self.kind == "renew_contract":
            lines = ["Request renewal. Both buyer and seller must agree; ordinary contract rules still apply."]
        if self.kind in ("publish_offer", "request_quotes", "submit_quote"):
            service = self.query_one("#service", Select).value
            price = model.catalogue.get("prices", {}).get(service, {})
            terms = model.catalogue.get("market", {})
            self.query_one("#terms", Static).update(f"Rate units: {'E' if service=='electricity' else 'waste units/min'}.\nPrice: {units(price.get('minimum'))}–{units(price.get('maximum'))}; increment {units(price.get('step'))}; standard {units(price.get('standard'))}.\nBooking: {units(terms.get('booking_micro'))} credits. Duration: up to {terms.get('duration','?')} seconds. Actual delivery may vary.")
        row = self.app.market_rows.get(self.payload.get("_market_key"))
        if row:
            service = row["type"]
            terms = model.catalogue.get("market", {})
            lines += [f"{name(service)} | {row['category']} {row['id']}",
                      f"Buyer: {self.app.city_label(row.get('buyer'))} | Seller: {self.app.city_label(row.get('seller'))}",
                      f"Rate: {units(row.get('rate'))} {'E' if service == 'electricity' else 'units/min'}",
                      f"Price: {units(row.get('price'))} credits/{'E-minute' if service == 'electricity' else 'unit'}",
                      f"Booking: {units(row.get('booking_micro', terms.get('booking_micro')))} credits; duration up to {terms.get('duration', '?')} seconds.",
                      f"State: {row.get('status', 'offer/request')}; end tick: {row.get('end_tick', 'not assigned')}. Delivery is not guaranteed."]
        self.query_one("#explanation", Static).update("\n".join(lines))

    @on(Select.Changed)
    @on(Input.Changed)
    def edited(self):
        self.revision += 1
        self.preview_payload = None
        self.preview = None
        self.preview_market_signature = None
        self.query_one("#confirm", Button).disabled = True
        self.describe()

    def read_payload(self):
        payload = {k:v for k,v in self.payload.items() if not k.startswith("_")}
        if self.kind == "construct":
            value = self.query_one("#building", Select).value
            if not isinstance(value, str):
                raise ValueError("Choose a building first.")
            payload["building_type"] = value
        if self.kind in ("publish_offer", "request_quotes", "submit_quote"):
            payload["type"] = self.query_one("#service", Select).value
            payload["rate"] = amount(self.query_one("#rate", Input).value)
            if self.kind != "request_quotes":
                payload["price"] = amount(self.query_one("#price", Input).value)
        return payload

    @on(Button.Pressed)
    async def pressed(self, event):
        event.stop()
        if event.button.id == "back":
            self.dismiss(); return
        if event.button.id == "check":
            if self.requesting:
                return
            self.edited()
            self.requesting = True
            self.run_worker(self.check_preview(), group="preview", exclusive=True)
        elif event.button.id == "confirm" and not self.submitted:
            if not self.app.can_act() or self.session != self.app.session_identity() or not self.preview or self.preview.get("status") != "available" or self.preview_payload is None or self.preview_payload != self.read_payload() or self.preview_market_signature != self.app.market_item_signature(self.payload.get("_market_key")):
                return
            self.submitted = True
            event.button.disabled = True
            try:
                await self.app.submit(self.kind, self.preview_payload)
                self.dismiss()
            except (ValueError, ConnectionError) as exc:
                self.query_one("#preview", Static).update(str(exc))
                self.submitted = False
                self.edited()

    async def check_preview(self):
        self.edited()
        revision = self.revision
        self.requesting = True
        try:
            payload = self.read_payload()
            market_signature = self.app.market_item_signature(self.payload.get("_market_key"))
            result = await self.app.preview_action(self.kind, payload)
            # Ignore a response if the player edited while it was in flight.
            if not self.is_mounted or revision != self.revision or self.session != self.app.session_identity() or not self.app.can_act() or payload != self.read_payload() or market_signature != self.app.market_item_signature(self.payload.get("_market_key")):
                return
            self.preview = result
            self.preview_payload = payload
            self.preview_market_signature = market_signature
            lines = [reason(result["status"]), f"Observed at server tick {result['tick']}. This reserves nothing."]
            p = result.get("purchase")
            if p:
                lines += [f"Base cost: {units(p['base_cost_micro'])} credits",
                          f"Recovered materials used / savings: {units(p['materials_micro'])}",
                          f"Payable treasury: {units(p['cash_micro'])} credits",
                          f"Duration: {p['duration_seconds']} seconds; Development: {units(p['unlock_micro'])}"]
            self.query_one("#preview", Static).update("\n".join(lines))
            self.query_one("#confirm", Button).disabled = result["status"] != "available"
        except (ValueError, ConnectionError, asyncio.TimeoutError) as exc:
            self.query_one("#preview", Static).update(str(exc) or "The preview timed out. Check your connection.")
        finally:
            self.requesting = False


class EquilibriumApp(App):
    TITLE = "EQUILIBRIUM"
    BINDINGS = [("q", "quit", "Exit"), ("f1", "help", "Help")]
    CSS = """
    Screen { background: #101923; color: #e4ebef; }
    #status { height: 3; padding: 0 1; background: #203446; color: #b9e4ee; }
    #notice { height: 2; padding: 0 1; color: #ffd080; }
    #menu, #lobby, #draft, #results { padding: 1 2; height: 1fr; }
    .title { text-style: bold; color: #91dfb5; margin-bottom: 1; }
    .buttons { height: auto; min-height: 3; layout: horizontal; }
    Button { margin: 0 1 0 0; min-width: 10; }
    Input, Select { margin-bottom: 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    #grid { height: 10; }
    #market-table { height: 10; }
    #inspector, #health, #waste, #rankings, #help-text, #market-detail, #events, #history { height: auto; margin: 1 0; }
    ActionDialog { align: center middle; background: #000000 60%; }
    #dialog { width: 76; max-width: 98%; height: 90%; border: solid #69becd; padding: 1 2; background: #172736; }
    #explanation, #preview, #terms { height: auto; margin-bottom: 1; }
    """
    def __init__(self, args=None):
        super().__init__()
        self.args = args or SimpleNamespace(url="ws://127.0.0.1:9080", session_file=None, create=False, join=None, rejoin=False)
        self.model = Model()
        self.socket = None
        self.network_task = None
        self.waiters = {}
        self.tile = 0
        self.market_key = None
        self.market_rows = {}
        self.pending_foundation = False
        self.entered_game = False
        self.credentials = None
        self.credential_path = None
        self.endpoint = None
        self.identity = None
        self.persistence_failed = False
        self.connection_generation = 0

    def compose(self) -> ComposeResult:
        yield Static("EQUILIBRIUM | Disconnected", id="status", markup=False)
        yield Button("Retry Saving Session", id="save-session")
        yield Static("Build sustainably. Attract residents. Most residents wins.", id="notice", markup=False)
        with VerticalScroll(id="menu"):
            yield Label("Four cities. One shared population. Twenty minutes to win.", classes="title")
            yield Input(value=self.args.url, placeholder="Server WebSocket URL", id="url")
            yield Input(value=self.args.session_file or str(default_session_path()), placeholder="Private session file (keep for reconnecting)", id="session")
            yield Input(value=self.args.join or "", placeholder="Room code for Join", id="room")
            with Horizontal(classes="buttons"):
                yield Button("Create Room", id="create", variant="success")
                yield Button("Join Room", id="join", variant="primary")
                yield Button("Reconnect", id="rejoin")
            yield Button("How to Play", id="learn")
            yield Button("Exit", id="exit")
            yield Static("Keep the session-file path to reopen your city. Each instance defaults to a unique file.\nYour city keeps running if you disconnect.", markup=False)
        with VerticalScroll(id="lobby"):
            yield Static("", id="lobby-state", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Ready", id="ready", variant="success")
                yield Button("Start Match", id="start", variant="primary")
                yield Button("Leave Room", id="leave")
        with VerticalScroll(id="draft"):
            yield Static("", id="draft-state", markup=False)
            yield Select([], prompt="Inspect a Foundation", id="foundation")
            yield Static("", id="foundation-detail", markup=False)
            yield Button("Choose Foundation", id="choose", variant="success")
        with Container(id="game"):
            with TabbedContent(id="tabs"):
                with TabPane("City", id="city-tab"):
                    with VerticalScroll():
                        yield DataTable(id="grid", cursor_type="cell")
                        yield Static("", id="inspector", markup=False)
                        with Horizontal(classes="buttons"):
                            yield Button("Build", id="build", variant="success")
                            yield Button("Upgrade", id="upgrade")
                            yield Button("Enable", id="enable")
                            yield Button("Disable", id="disable")
                        with Horizontal(classes="buttons"):
                            yield Button("Demolish", id="demolish", variant="warning")
                            yield Button("Cancel Project", id="cancel_project", variant="warning")
                        with Collapsible(title="Building effects and grid legend"):
                            yield Static("", id="building-detail", markup=False)
                        with Collapsible(title="City health and possible responses"):
                            yield Static("", id="health", markup=False)
                with TabPane("Waste & Energy", id="waste-tab"):
                    with VerticalScroll():
                        yield Static("", id="waste", markup=False)
                        yield Button("Change Reduce Waste", id="policy", variant="success")
                        yield Button("Find Treatment / Power", id="go-market")
                with TabPane("Market", id="market-tab"):
                    with VerticalScroll():
                        yield Select([("Available Services","offers"),("Requests & Quotes","requests"),("My Contracts","contracts")], value="offers", allow_blank=False, id="market-filter")
                        yield MarketTable(id="market-table", cursor_type="row")
                        yield Static("Select a row to inspect.", id="market-detail", markup=False)
                        with Horizontal(classes="buttons"):
                            yield Button("Publish Offer", id="publish_offer")
                            yield Button("Request Quotes", id="request_quotes")
                        with Horizontal(classes="buttons"):
                            yield Button("Accept", id="accept_quote")
                            yield Button("Withdraw", id="withdraw_offer")
                            yield Button("Quote", id="submit_quote")
                        with Horizontal(classes="buttons"):
                            yield Button("Renew", id="renew_contract")
                            yield Button("Cancel Contract", id="cancel_contract", variant="warning")
                with TabPane("Rankings", id="rank-tab"):
                    with VerticalScroll():
                        yield Static("", id="rankings", markup=False)
                        yield Static("", id="events", markup=False)
                with TabPane("Help / Feed", id="help-tab"):
                    with VerticalScroll():
                        yield Static(HELP, id="help-text", markup=False)
                        yield Button("Retry Pending (same action IDs)", id="retry")
                        yield Static("", id="history", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Reconnect", id="game-rejoin")
                yield Button("Menu / Disconnect", id="game-menu")
        with VerticalScroll(id="results"):
            yield Static("", id="final", markup=False)
            yield Button("Return to Menu", id="result-menu")
        yield Footer()

    def q(self, selector, cls=None):
        return self.screen_stack[0].query_one(selector, cls) if cls else self.screen_stack[0].query_one(selector)

    def on_mount(self):
        grid = self.q("#grid", DataTable)
        for i in range(6):
            grid.add_column(str(i + 1), width=6)
        for row in range(6):
            grid.add_row(*["." for _ in range(6)], key=str(row), label=str(row + 1))
        self.q("#market-table", DataTable).add_columns("Ref", "City / Parties", "Service", "Rate", "Price", "Window / State")
        self.show("menu")
        if self.args.create or self.args.join or self.args.rejoin:
            self.run_worker(self.open_connection("rejoin" if self.args.rejoin else "join" if self.args.join else "create"), exclusive=True, group="connect")

    def show(self, section):
        for item in ("menu", "lobby", "draft", "game", "results"):
            self.q("#" + item).display = item == section

    def session_identity(self):
        return (self.identity, self.model.view.get("match_id"))

    @staticmethod
    def city_label(city_id):
        return "not assigned" if not isinstance(city_id, int) else f"City {city_id + 1}"

    def market_item_signature(self, key):
        if key is None:
            return None
        row = self.market_rows.get(key)
        if row is None:
            return None
        fields = (
            "category", "id", "type", "buyer", "seller", "rate", "price",
            "status", "request_id", "quote_until", "select_until", "start_tick",
            "end_tick", "booking_micro",
        )
        return tuple((field, row.get(field)) for field in fields)

    def can_act(self):
        return self.model.connection == "Connected" and self.model.seat is not None and self.model.view_fresh and bool(self.model.view) and not self.model.final

    def invalidate_dialog(self):
        for screen in self.screen_stack:
            if isinstance(screen, ActionDialog):
                screen.edited()

    def reset_session(self):
        self.invalidate_dialog()
        self.model = Model()
        self.entered_game = False
        self.market_key = None
        self.market_rows = {}
        self.tile = 0
        self.pending_foundation = False
        self.identity = None
        table = self.q("#market-table", DataTable)
        table.clear()
        table.show_cursor = False

    def persist_credentials(self):
        try:
            save_session(self.credential_path, self.credentials)
        except (OSError, ValueError):
            self.persistence_failed = True
            self.model.note("Session save failed. Keep this app open and retry saving; restart recovery is not safe.")
        else:
            self.persistence_failed = False

    def fail_waiters(self, generation, message):
        for correlation, (owner, future) in list(self.waiters.items()):
            if owner == generation:
                if not future.done():
                    future.set_exception(ConnectionError(message))
                self.waiters.pop(correlation, None)

    async def disconnect(self):
        generation = self.connection_generation
        self.connection_generation += 1
        self.model.connection = "Disconnected — cached data is stale; city continues running"
        self.model.recent = None
        self.model.view_fresh = False
        self.invalidate_dialog()
        if self.is_mounted and self.screen_stack:
            self.refresh_view()
        socket, self.socket = self.socket, None
        if socket:
            await socket.close()
        task, self.network_task = self.network_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.fail_waiters(generation, "Disconnected; reconnect before acting.")

    async def open_connection(self, mode):
        url = self.q("#url", Input).value.strip()
        path = str(Path(self.q("#session", Input).value.strip()).expanduser().absolute())
        previous = (self.endpoint, self.credential_path, self.identity, self.credentials)
        same = mode == "rejoin" and url == self.endpoint and path == self.credential_path
        socket = None
        committed = False
        try:
            saved = (self.credentials if same and self.credentials else load_session(path)) if mode == "rejoin" else None
            if mode != "rejoin" and Path(path).exists():
                raise ValueError("That session file already exists. Reconnect, or choose a new path.")
            if not same and self.persistence_failed:
                raise ValueError("Save the current session before changing sessions.")
            old_generation = self.connection_generation
            self.connection_generation += 1
            generation = self.connection_generation
            old_socket, self.socket = self.socket, None
            old_task, self.network_task = self.network_task, None
            if old_socket:
                await old_socket.close()
            if old_task and old_task is not asyncio.current_task():
                old_task.cancel()
                await asyncio.gather(old_task, return_exceptions=True)
            self.fail_waiters(old_generation, "Connection changed; request a new preview.")
            self.model.connection = "Connecting"
            self.model.view_fresh = False
            self.refresh_view()
            socket = await connect(url, max_size=1048576, proxy=None, open_timeout=5, close_timeout=1)
            if generation != self.connection_generation:
                return
            await send(socket, "hello", client="terminal", client_version="1.1")
            greeting = parse_packet(await asyncio.wait_for(socket.recv(), 5))
            if generation != self.connection_generation:
                return
            if greeting.get("type") != "welcome":
                raise ValueError("Server did not accept protocol negotiation.")
            self.model.connection = "Rejoining" if mode == "rejoin" else "Joining"
            self.refresh_view()
            if mode == "rejoin":
                await send(socket, "rejoin", **saved)
            else:
                await send(socket, "create_lobby" if mode == "create" else "join_lobby", **({"join_code":self.q("#room", Input).value.strip()} if mode == "join" else {}))
            pending_messages = []
            while True:
                assigned = parse_packet(await asyncio.wait_for(socket.recv(), 5))
                if generation != self.connection_generation:
                    return
                if assigned["type"] == "protocol_error":
                    raise ValueError(reason(assigned.get("code", "unknown")))
                if assigned["type"] == "seat_assigned":
                    break
                pending_messages.append(assigned)
            if generation != self.connection_generation:
                return
            identity = (url, assigned["lobby_id"], assigned["seat"])
            if self.identity is not None and self.identity != identity:
                self.reset_session()
            self.endpoint, self.credential_path, self.identity = url, path, identity
            self.credentials = session_data(assigned)
            committed = True
            self.persistence_failed = False
            self.socket = socket
            socket = None
            self.model.connection = "Connected"
            self.model.view_fresh = not bool(self.model.view)
            self.model.ingest(assigned, self.session_identity())
            for message in pending_messages:
                self.model.ingest(message, self.session_identity())
            self.persist_credentials()
            active_socket = self.socket
            await send(active_socket, "get_catalogue")
            if generation != self.connection_generation or self.socket is not active_socket:
                return
            self.network_task = asyncio.create_task(self.receive(active_socket, path, generation))
        except asyncio.CancelledError:
            if 'generation' in locals() and generation == self.connection_generation:
                if not committed:
                    self.endpoint, self.credential_path, self.identity, self.credentials = previous
                    self.model.connection = "Disconnected — previous session preserved for reconnect"
                else:
                    self.model.connection = "Disconnected — authenticated session preserved for reconnect"
            raise
        except (OSError, ValueError, asyncio.TimeoutError, WebSocketException, PacketError):
            if 'generation' in locals() and generation == self.connection_generation:
                if not committed:
                    self.endpoint, self.credential_path, self.identity, self.credentials = previous
                    self.model.connection = "Disconnected — previous session preserved for reconnect"
                    self.model.note("Connection or session failed. Previous session data was preserved.")
                else:
                    self.model.connection = "Disconnected — authenticated session preserved for reconnect"
                    self.model.note("Connection failed after authentication. The rotated session credential was preserved.")
                    if self.socket is not None:
                        await self.socket.close()
                        self.socket = None
            elif 'generation' not in locals():
                self.model.note("Connection or session failed. Existing session data was preserved.")
        finally:
            if socket is not None:
                await socket.close()
        self.refresh_view()

    async def receive(self, socket, path, generation):
        try:
            async for raw in socket:
                try:
                    m = parse_packet(raw)
                except PacketError:
                    if generation == self.connection_generation:
                        self.model.note("Unexpected server data; last valid display retained.")
                        self.refresh_view()
                    continue
                if generation != self.connection_generation or self.socket is not socket:
                    return
                correlation = m.get("correlation_id")
                waiter = self.waiters.get(correlation)
                if waiter and waiter[0] == generation:
                    future = waiter[1]
                    if not future.done():
                        if m["type"] == "protocol_error":
                            future.set_exception(ValueError(reason(m.get("code", "unknown"))))
                        else:
                            future.set_result(m)
                if m["type"] == "seat_assigned":
                    # Token rotation belongs to this authenticated generation.
                    identity = (self.endpoint, m["lobby_id"], m["seat"])
                    if identity != self.identity:
                        raise RuntimeError("Authenticated connection changed seat unexpectedly.")
                    self.credentials = session_data(m)
                    self.persist_credentials()
                    self.model.connection = "Connected"
                    await send(socket, "get_catalogue")
                if m["type"] == "player_view" and self.model.view and self.model.view.get("match_id") != m.get("match_id"):
                    self.invalidate_dialog()
                    self.market_key = None
                    self.market_rows.clear()
                    self.q("#market-table", DataTable).clear()
                self.model.ingest(m, self.session_identity())
                if m["type"] == "protocol_error" and m.get("code") == "authentication_failed":
                    self.invalidate_dialog()
                    await socket.close()
                if m["type"] in ("draft_state", "action_ack", "protocol_error"):
                    self.pending_foundation = False
                if m["type"] == "match_cancelled":
                    self.show("menu")
                if m["type"] == "catalogue":
                    selection = self.q("#foundation", Select)
                    selection.set_options([(name(k), k) for k in m["foundations"]])
                self.refresh_view()
        except (OSError, WebSocketException):
            self.model.note("Connection lost. Your city continues operating; displayed data may be outdated.")
        finally:
            await socket.close()
            if generation == self.connection_generation and self.socket is socket:
                self.socket = None
                self.model.connection = "Disconnected — displayed information may be stale"
                self.model.recent = None
                self.model.view_fresh = False
                self.invalidate_dialog()
                self.fail_waiters(generation, "Connection lost. Reconnect to recover authoritative receipts.")
                self.refresh_view()

    async def wire(self, message_type, **payload):
        if not self.socket or self.model.connection != "Connected" or self.model.seat is None:
            raise ConnectionError("Disconnected. Reconnect before acting.")
        socket = self.socket
        generation = self.connection_generation
        try:
            result = await send(socket, message_type, **payload)
            if generation != self.connection_generation or socket is not self.socket:
                raise ConnectionError("Connection changed while sending; authoritative delivery is uncertain.")
            return result
        except (OSError, WebSocketException):
            self.model.connection = "Disconnected — delivery uncertain"
            self.invalidate_dialog()
            raise ConnectionError("Send interrupted; reconnect to recover any server receipt.") from None

    async def preview_action(self, kind, payload):
        if not self.can_act():
            raise ConnectionError("An authenticated active match is required.")
        correlation = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        generation = self.connection_generation
        self.waiters[correlation] = (generation, future)
        try:
            # Use shared framing, with a known correlation before any response.
            await self.wire("preview_action", correlation_id=correlation, kind=kind, payload=payload)
            return await asyncio.wait_for(future, 5)
        finally:
            self.waiters.pop(correlation, None)

    async def submit(self, kind, payload):
        if not self.can_act():
            raise ConnectionError("An authenticated active match is required.")
        for record in self.model.actions.values():
            if record["kind"] == kind and record["payload"] == payload and record["status"] in ("sent", "queued", "uncertain"):
                raise ValueError("That action is already pending. Wait for its receipt or reconnect.")
        action_id = uuid.uuid4().hex
        self.model.remember(action_id, name(kind), kind, payload)
        correlation = uuid.uuid4().hex
        self.model.actions[action_id]["session"] = self.session_identity()
        self.model.correlations[correlation] = {
            "action_id": action_id,
            "session": self.session_identity(),
            "generation": self.connection_generation,
        }
        try:
            await self.wire("submit_action", correlation_id=correlation, action_id=action_id, kind=kind, payload=payload, observed_tick=self.model.view.get("tick", 0))
        except ConnectionError:
            self.model.actions[action_id]["status"] = "uncertain"
            self.model.note("Delivery uncertain. Reconnect; do not submit a replacement action.")
            raise
        finally:
            self.refresh_view()

    def action_help(self):
        self.push_screen(HelpDialog())

    async def on_unmount(self):
        await self.disconnect()

    @on(Select.Changed, "#foundation")
    def foundation_changed(self):
        selected = self.q("#foundation", Select).value
        if not isinstance(selected, str):
            return
        entry = self.model.catalogue.get("foundations", {}).get(selected, {})
        self.q("#foundation-detail", Static).update(f"{name(selected)}\n{PURPOSE.get(selected,'')}\nStarting treasury: {entry.get('money','?')} credits\nCompleted building: {name(entry.get('building'))}")
        self.refresh_draft()

    @on(Select.Changed, "#market-filter")
    def market_filter_changed(self):
        self.market_key = None
        self.q("#market-table", DataTable).show_cursor = False
        self.refresh_market()

    @on(DataTable.CellHighlighted, "#grid")
    def tile_highlighted(self, event):
        self.tile = event.coordinate.row * 6 + event.coordinate.column
        self.refresh_inspector()

    @on(DataTable.RowSelected, "#market-table")
    def market_selected(self, event):
        table = self.q("#market-table", DataTable)
        if event.row_key.value not in self.market_rows:
            return
        if self.market_key != event.row_key.value:
            self.invalidate_dialog()
        self.market_key = event.row_key.value
        table.show_cursor = True
        self.refresh_market_detail()

    @on(Button.Pressed)
    async def button(self, event):
        key = event.button.id
        try:
            if key in ("create", "join", "rejoin", "game-rejoin"):
                self.run_worker(self.open_connection("rejoin" if key in ("rejoin", "game-rejoin") else key), exclusive=True, group="connect")
            elif key == "exit":
                self.exit()
            elif key in ("leave", "game-menu", "result-menu"):
                await self.disconnect()
                self.show("menu")
                self.q("#notice", Static).update("Disconnected. Reconnect with the same session file to resume an existing match.")
            elif key == "save-session":
                if self.credentials:
                    self.persist_credentials()
                    self.refresh_view()
            elif key == "learn":
                self.action_help()
            elif key == "ready":
                rows = self.model.lobby.get("seats", [])
                mine = next((r for r in rows if r["seat"] == self.model.seat), {})
                await self.wire("set_ready", ready=not mine.get("ready", False))
            elif key == "start":
                await self.wire("start_match")
            elif key == "choose" and not self.pending_foundation:
                selected = self.q("#foundation", Select).value
                if selected in self.model.draft.get("available_foundations", []):
                    self.pending_foundation = True
                    await self.wire("choose_foundation", action_id=uuid.uuid4().hex, foundation=selected)
                    self.refresh_draft()
            elif key == "retry":
                record = next(((aid, r) for aid, r in self.model.actions.items()
                               if r["status"] == "uncertain" and r.get("session") == self.session_identity()), None)
                if record is None:
                    record = next(((aid, r) for aid, r in self.model.actions.items()
                                   if r["status"] == "sent" and r.get("session") == self.session_identity()), None)
                if record:
                    aid,r = record
                    if not self.can_act():
                        raise ConnectionError("Reconnect to the same active match first.")
                    correlation = uuid.uuid4().hex
                    self.model.correlations[correlation] = {
                        "action_id": aid,
                        "session": self.session_identity(),
                        "generation": self.connection_generation,
                    }
                    await self.wire("submit_action",correlation_id=correlation,action_id=aid,kind=r["kind"],payload=r["payload"])
                    self.model.note("Retried the existing pending action ID; no replacement action was created.")
                else:
                    self.model.note("No uncertain delivery to retry. Queued actions recover their receipt on reconnect.")
                self.refresh_view()
            elif key == "go-market":
                self.q("#tabs", TabbedContent).active = "market-tab"
            elif key == "build":
                self.push_screen(ActionDialog("construct", {"tile":self.tile}))
            elif key in ("upgrade", "enable", "disable", "demolish", "cancel_project"):
                plant = self.selected_plant()
                if plant:
                    self.push_screen(ActionDialog(key, {"building_id":plant["id"]}))
            elif key == "policy":
                self.push_screen(ActionDialog("set_reduce_waste", {"enabled":not self.model.view["own_city"]["reduce_waste"]}))
            elif key in ("publish_offer", "request_quotes"):
                self.push_screen(ActionDialog(key))
            elif key in ("accept_quote", "withdraw_offer", "submit_quote", "renew_contract", "cancel_contract"):
                row = self.market_rows.get(self.market_key)
                if row:
                    payload = {"request_id":row["id"],"type":row["type"],"_rate":units(row["rate"],6).replace(",","")} if key=="submit_quote" else {"id":row["id"]}
                    self.push_screen(ActionDialog(key,{**payload,"_market_key":self.market_key}))
        except (ValueError, ConnectionError) as exc:
            self.model.note(str(exc)); self.refresh_view()

    def selected_plant(self):
        return next((b for b in self.model.view.get("own_city", {}).get("buildings", []) if b["tile"] == self.tile), None)

    def refresh_draft(self):
        draft = self.model.draft
        choices = draft.get("choices", [])
        turn = draft.get("turn_seat", -1)
        lines = [f"Your city: {(self.model.seat or 0)+1}. " + ("Your turn." if turn==self.model.seat else f"Waiting for City {turn+1}.")]
        lines += [f"City {i+1}: {name(choice) if choice else 'Not selected'}" for i,choice in enumerate(choices)]
        lines.append("Disconnected humans retain their choice opportunity during the server grace period; expiry cancels the draft.")
        self.q("#draft-state", Static).update("\n".join(lines))
        selected = self.q("#foundation", Select).value
        self.q("#choose", Button).disabled = self.pending_foundation or turn != self.model.seat or selected not in draft.get("available_foundations", []) or self.model.connection != "Connected"

    def refresh_view(self):
        m = self.model
        connected = m.connection == "Connected" and m.seat is not None and (not m.view or m.view_fresh)
        notice = m.history[-1] if m.history else "Build sustainably. Attract residents. Most residents wins."
        conditions = m.view.get("presentation",{}).get("conditions",[])
        if conditions:
            notice += "\n" + WARNINGS.get(conditions[0]["condition"],name(conditions[0]["condition"]))
        self.q("#save-session", Button).display = self.persistence_failed
        if self.persistence_failed:
            notice = "SESSION NOT SAVED — keep this app open; retry saving before exiting.\n" + notice
        self.q("#notice", Static).update(notice)
        self.q("#history", Static).update("PENDING ACTIONS\n" + "\n".join(r["label"]+": "+reason(r["status"]) for r in m.actions.values() if r["status"] in ("sent","queued","uncertain")) + "\n\nRECENT EVENTS\n" + "\n".join(m.history))
        c = m.view.get("own_city", {})
        if c:
            public = m.view.get("public", {})
            rank = next((r["rank"] for r in public.get("rankings", []) if r["city_id"]==m.seat), "?")
            self.q("#status", Static).update(f"City {c['city_id']+1} | {name(c['foundation'])} | {clock(public.get('duration_ticks',1200)-m.view['tick'])} left\nPopulation {c['population']:,} | Rank {rank}/4 | Treasury {units(c['money_micro'])} | Materials {units(c['materials_micro'])}\n{m.connection}")
        else:
            self.q("#status", Static).update("EQUILIBRIUM | " + m.connection + "\nFour cities. 4,000 residents. Twenty minutes to win.")
        if m.final:
            self.show("results")
            rows = m.final.get("rankings", m.view.get("public",{}).get("rankings",[]))
            winners = ", ".join(f"City {i+1}" for i in m.final.get("winners", []))
            text = [f"MATCH COMPLETE — Winner: {winners}", f"Your Foundation: {name(c.get('foundation'))}", f"Duration: {clock(m.final.get('duration',1200))}"]
            text += [f"{r['rank']}. City {r['city_id']+1}: {r['population']:,} residents" for r in rows]
            text += ["", "YOUR CIRCULAR ECONOMY — cumulative match totals", self.circular_totals(c), "Another match requires a server restart."]
            self.q("#final", Static).update("\n".join(text))
        elif c:
            if not self.entered_game:
                self.entered_game = True; self.show("game")
            grid = self.q("#grid", DataTable)
            plants = {b["tile"]: b for b in c.get("buildings", [])}
            projects = {p["building_id"]:p for p in c.get("projects",[]) if p}
            for tile in range(36):
                b = plants.get(tile)
                label = "." if not b else SYMBOLS.get(b["type"],"?") + str(b["level"]) + ("*" if b["id"] in projects else "-" if not b["enabled"] else "")
                grid.update_cell(str(tile//6), grid.ordered_columns[tile%6].key, label)
            self.refresh_inspector()
            health = [f"Available treasury after holds: {units(c.get('available_money_micro'))} credits.", f"Satisfaction: {units(c.get('satisfaction_micro'))}/100 | Development: {units(c.get('development_micro'))}/100",
                      "Satisfaction contributions (points): " + " | ".join(f"{name(k)} {units(v)}" for k,v in c.get("satisfaction_components_micro",{}).items())]
            health += list(dict.fromkeys(WARNINGS.get(r["condition"],name(r["condition"])) for r in m.view.get("presentation",{}).get("conditions",[])))
            self.q("#health", Static).update("\n".join(health))
            self.q("#waste", Static).update(self.waste_text(c))
            self.q("#policy", Button).disabled = not connected
            self.refresh_market()
            rows = public.get("rankings",[])
            foundations = public.get("foundations",[])
            self.q("#rankings", Static).update("POPULATION DECIDES VICTORY\n" + "\n".join(f"{r['rank']}. City {r['city_id']+1} {'(You)' if r['city_id']==m.seat else ''}: {r['population']:,} residents | {name(foundations[r['city_id']]) if r['city_id'] < len(foundations) else ''} | Satisfaction {units(r['satisfaction_micro'])} | Environment {units(r['environment_micro'])}" for r in rows) + f"\nLast migration change: {m.view.get('presentation',{}).get('last_migration_delta','—')}\nNext migration tick: {m.view.get('presentation',{}).get('next_migration','—')}")
            event_state = public.get("events",{})
            events = [f"Active: {', '.join(name(e) for e in event_state.get('active',[])) or 'None'}",f"Warnings: {', '.join(name(e) for e in event_state.get('warnings',[])) or 'None'}"]
            for e in m.catalogue.get("events",[]):
                events.append(f"{name(e['id'])}: warning {clock(e['warning'])}; active {clock(e['start'])}–{clock(e['end'])}")
                labels={"air_percent":"Effective air % of stock","household_percent":"Household waste % of normal","factory_percent":"Factory waste % of normal","commerce_percent":"Commerce waste % of normal","municipal_E":"Municipal electricity E","uncollected_penalty_micro":"Street waste penalty per unit"}
                events.append("; ".join(f"{labels[k]}: {units(v) if k.endswith('micro') else v}" for k,v in e.get("modifiers",{}).items() if k in labels))
            self.q("#events", Static).update("\n".join(events))
        elif m.draft and m.lobby.get("phase") == "draft":
            self.show("draft"); self.refresh_draft()
        elif m.lobby:
            self.show("lobby")
            rows = m.lobby["seats"]
            human = sum(r["controller"]=="human" for r in rows)
            lines = [f"Room: {m.room or 'Use the code shared by the host'} | You: City {(m.seat or 0)+1}", f"{human} humans + {4-human} bots on start | Twenty-minute match"]
            for r in rows:
                detail = "Empty — bot on start" if r["controller"]=="open" else f"{name(r['controller'])} — {'Ready' if r['ready'] else 'Not ready'} — {'Connected' if r['connected'] else 'Disconnected'}"
                lines.append(f"City {r['seat']+1}{' (Host)' if r['seat']==m.lobby['host_seat'] else ''}: {detail}")
            can_start = connected and m.seat==m.lobby["host_seat"] and all(r["ready"] for r in rows if r["controller"]=="human")
            lines.append("Ready to start." if can_start else "Only the host can start, after all humans are ready.")
            self.q("#lobby-state", Static).update("\n".join(lines))
            self.q("#start", Button).disabled = not can_start
            mine = next((r for r in rows if r["seat"]==m.seat),{})
            self.q("#ready", Button).label = "Not Ready" if mine.get("ready") else "Ready"
            self.q("#ready", Button).disabled = not connected
        elif m.seat is None and not self.q("#game").display:
            self.show("menu")

    def refresh_inspector(self):
        if not self.is_mounted:
            return
        b = self.selected_plant()
        connected = self.can_act()
        self.q("#build", Button).disabled = b is not None or not connected or not self.model.catalogue
        for action in ("upgrade","enable","disable","demolish","cancel_project"):
            self.q("#"+action,Button).disabled = b is None or not connected
        if b and connected:
            project = any(p and p["building_id"] == b["id"] for p in self.model.view["own_city"].get("projects", []))
            self.q("#enable", Button).disabled = b["enabled"] or b["status"] != "complete"
            self.q("#disable", Button).disabled = not b["enabled"] or b["status"] != "complete"
            self.q("#cancel_project", Button).disabled = not project
            self.q("#upgrade", Button).disabled = project or b["level"] >= 2 or b["status"] != "complete"
        lines = [f"Selected: row {self.tile//6+1}, column {self.tile%6+1}"]
        extra = []
        if b:
            lines += [f"{name(b['type'])} — level {b['level']} — {name(b['status'])}", PURPOSE.get(b["type"],""),
                      f"{'Enabled' if b['enabled'] else 'Disabled'} | {'Funded' if b.get('funded') else 'Not funded'} | {'Operating' if b.get('operational') else 'Not operating'}"]
            for p in self.model.view["own_city"].get("projects",[]):
                if p and p["building_id"]==b["id"]:
                    lines.append(f"{name(p['kind'])}: {max(0,p['complete_tick']-self.model.view['tick'])} seconds remaining; completion at tick {p['complete_tick']}.")
                    lines.append(f"Paid {units(p['cash_paid'])} credits; recovered materials used {units(p['materials_paid'])}.")
            detail = next((d for d in self.model.view.get("presentation",{}).get("buildings",[]) if d["id"]==b["id"]),{})
            extra += [f"Operating efficiency: {units(detail.get('efficiency_micro'))} (0–1)", effects_text(detail.get("effects",{})), "Actions are checked by the server before confirmation."]
        else:
            lines.append("Empty tile. Select Build to compare all eleven buildings.")
        extra.append("Legend: " + " | ".join(f"{s} {name(k)}" for k,s in SYMBOLS.items()) + "\n* project in progress; - disabled; number is level.")
        self.q("#inspector",Static).update("\n".join(lines))
        self.q("#building-detail",Static).update("\n".join(extra))

    def circular_totals(self,c):
        w=c.get("waste_totals",{})
        return "\n".join([f"Waste collected: {units(w.get('collected'))} units",
            f"Recyclables processed: {units(w.get('recycled'))} units",
            f"Organic waste processed: {units(w.get('organic_processed'))} units",
            f"Materials recovered: {units(c.get('recovered_micro'))}",
            f"Materials used in construction: {units(c.get('materials_spent_micro'))}",
            f"Waste landfilled: {units(c.get('disposed_micro'))} units"])

    def waste_text(self,c):
        w=c.get("waste_totals",{})
        generated=w.get("generated_by_source",{})
        details=self.model.view.get("presentation",{})
        rows=["WASTE FLOW", "Households / industry -> collection -> sorted waste",
              "  Recyclables -> reuse / recycling -> materials -> construction / sales",
              "  Organics -> biogas treatment -> electricity next tick",
              "  Residuals + processing rejects -> landfill",
              "  Contracts transfer treatment to another city; suppliers own recovered output.",
              "", "CURRENT INVENTORIES (waste units)"]
        for i,label in enumerate(["Recyclable","Organic","Residual"]):
            rows.append(f"{label}: street {units(c.get('uncollected_micro',[0]*3)[i])} | collected awaiting treatment {units(c.get('sorted_micro',[0]*3)[i])}")
        rows += [f"Effective collection capacity: {units(details.get('collection_micro_per_minute'))} units/min",
                 f"Effective landfill capacity: {units(details.get('landfill_micro_per_minute'))} units/min",
                 f"Recovered materials available: {units(c.get('materials_micro'))}", "", "CUMULATIVE MATCH TOTALS"]
        recent = self.model.recent
        impact = "Recent activity unavailable until successive match snapshots arrive."
        if recent and self.can_act():
            ticks, values = recent
            impact = f"RECENT ACTIVITY over {ticks} server seconds (not a per-second rate): " + " | ".join(f"{k}: {units(v)}" for k, v in values.items())
        rows.insert(0, impact + "\n")
        for source, values in generated.items():
            rows.append(f"{name(source)} generated: {units(sum(values))} waste units")
        rows += [self.circular_totals(c), "", "ELECTRICITY (E, current interval)"]
        rows += [f"{name(k)}: {units(v)}" for k,v in c.get("electricity",{}).items() if k in ("supply","demand","delivered","unused","shortage","imports","exports","biogas_available")]
        rows += [f"Reliability (0–1): {units(c.get('capacity',{}).get('reliability_micro'))}",
                 "", f"Environment: {units(c.get('environment_micro'))}/100 | Air: {units(c.get('air_micro'))} | Landfill damage: {units(c.get('landfill_damage_micro'))} | Overflow penalty: {units(c.get('overflow_micro'))}",
                 f"Reduce Waste requested: {'ON' if c.get('reduce_waste') else 'OFF'}; funded this interval: {'yes' if c.get('policy_active') else 'no'}.",
                 "Cleanup affects air. Treatment and prevention may reduce disposal; outcomes also depend on capacity and funding."]
        return "\n".join(rows)

    def refresh_market(self):
        if not self.is_mounted:
            return
        table=self.q("#market-table",DataTable)
        mode=self.q("#market-filter",Select).value
        market=self.model.view.get("marketplace",{})
        rows = self.model.view.get("own_city",{}).get("contracts",[]) if mode=="contracts" else market.get("requests",[])+[o for o in market.get("offers",[]) if o.get("request_id",-1)>=0] if mode=="requests" else [o for o in market.get("offers",[]) if o.get("request_id",-1)<0]
        mapped={}
        for r in rows:
            category = "contract" if "end_tick" in r else "offer" if "seller" in r else "request"
            mapped[f"{category}:{r['id']}"]={**r,"category":category}
        for key in list(table.rows):
            if key.value not in mapped:
                table.remove_row(key)
        for key,r in mapped.items():
            parties=f"{r.get('buyer',-1)+1} <- {r['seller']+1}" if r["category"]=="contract" else str(r.get("seller",r.get("buyer",-1))+1)
            window=f"{r['quote_until']}–{r['select_until']}" if r["category"]=="request" else f"ends {r['end_tick']} / {name(r['status'])}" if r["category"]=="contract" else ("quote" if r.get("request_id",-1)>=0 else "open")
            rate=units(r["rate"])+(" E" if r["type"]=="electricity" else "/min")
            values=[key,parties,name(r["type"]),rate,units(r.get("price")),window]
            if key in table.rows:
                for col,value in zip(table.ordered_columns,values):
                    table.update_cell(key,col.key,value)
            else:
                table.add_row(*values,key=key)
        previous_key = self.market_key
        self.market_rows=mapped
        if previous_key not in mapped:
            self.market_key = None
            table.show_cursor = False
        if self.market_key is not None:
            table.move_cursor(row=table.get_row_index(self.market_key))
            table.show_cursor = True
        for screen in self.screen_stack:
            if isinstance(screen, ActionDialog) and screen.payload.get("_market_key"):
                key = screen.payload["_market_key"]
                if screen.market_signature != self.market_item_signature(key):
                    screen.edited()
                    screen.query_one("#check", Button).disabled = True
                    screen.query_one("#preview", Static).update("The selected market terms changed or disappeared. Close this dialog and select the current row again.")
        self.refresh_market_detail()

    def refresh_market_detail(self):
        r=self.market_rows.get(self.market_key)
        text="Select a service, request or contract. Preview checks server eligibility."
        if r:
            text=f"{name(r['type'])} — {r['category']} reference {r['id']}\n"
            if r["category"]=="contract":
                text+=f"Buyer City {r['buyer']+1}; seller City {r['seller']+1}. Ticks {r['start_tick']}–{r['end_tick']}. {name(r['status'])}.\nDelivered total: {units(r.get('delivered'))} {'E-seconds' if r['type']=='electricity' else 'waste units'}; usage paid: {units(r.get('paid'))} credits.\nLast interval delivery: {units(r.get('last_delivery'))}; booking: {units(r.get('booking_micro'))} credits. Renewal approvals: {', '.join(str(i+1) for i in r.get('renewals',[])) or 'none'}."
            elif r["category"]=="request":
                text+=f"Buyer City {r['buyer']+1}; quote before tick {r['quote_until']}; buyer selects during ticks {r['quote_until']}–{r['select_until']}."
            else:
                text+=f"Seller City {r['seller']+1}. Request reference: {r.get('request_id',-1) if r.get('request_id',-1)>=0 else 'direct offer'}.\nUse Accept to check eligibility and booking rules before committing."
        self.q("#market-detail",Static).update(text)
        connected=self.can_act()
        for k in ("publish_offer","request_quotes"):
            self.q("#"+k,Button).disabled=not connected
        for k,category in [("accept_quote","offer"),("withdraw_offer","offer"),("submit_quote","request"),("renew_contract","contract"),("cancel_contract","contract")]:
            applicable = bool(r) and r["category"] == category
            if applicable and k == "withdraw_offer": applicable = r.get("seller") == self.model.seat
            if applicable and k == "accept_quote": applicable = r.get("seller") != self.model.seat
            if applicable and k == "submit_quote": applicable = r.get("buyer") != self.model.seat
            if applicable and category == "contract": applicable = r.get("status") == "active"
            self.q("#"+k,Button).disabled=not connected or not applicable
