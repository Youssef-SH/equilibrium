"""Formatting and bounded observations of server messages, never game rules."""
from collections import OrderedDict, deque
from decimal import Decimal
import re

SCALE = 1_000_000
NAMES = {
    "residential_district": "Residential District", "factory": "Factory",
    "commercial_district": "Commercial District", "public_services_center": "Public Services",
    "recovery_hub": "Recovery Hub", "recycling_center": "Recycling Center",
    "biogas_facility": "Biogas Facility", "landfill": "Landfill",
    "fossil_power_plant": "Fossil Power", "renewable_power_plant": "Renewable Power",
    "green_space": "Green Space", "industrial_legacy": "Industrial Legacy",
    "circular_foundation": "Circular Foundation", "energy_pioneer": "Energy Pioneer",
    "investment_district": "Investment District", "recyclable": "Recyclable treatment",
    "organic": "Organic treatment", "electricity": "Electricity",
}
SYMBOLS = dict(zip(list(NAMES)[:11], ["HO", "FA", "CO", "PS", "RH", "RC", "BG", "LF", "FP", "RP", "GS"]))
PURPOSE = {
    "residential_district": "Housing admits residents and supports housing satisfaction.",
    "factory": "Jobs and revenue; needs power and creates waste and air pollution.",
    "commercial_district": "Jobs and revenue; needs power, generates waste and some air pollution.",
    "public_services_center": "Public services support satisfaction; requires workers and electricity.",
    "recovery_hub": "Collects street waste. Collection is separate from treatment.",
    "recycling_center": "Treats collected recyclables; recovered materials discount construction or are sold above the reserve.",
    "biogas_facility": "Treats collected organic waste; biogas electricity becomes available next tick.",
    "landfill": "Disposes of collected waste at a per-unit fee; contributes landfill damage.",
    "fossil_power_plant": "Electricity generation with upkeep and air pollution.",
    "renewable_power_plant": "Electricity generation without operating air emissions.",
    "green_space": "Reduces air pollution; does not repair landfill damage or treat waste.",
    "industrial_legacy": "An operating Factory gives early jobs and revenue, with power and waste obligations.",
    "circular_foundation": "An operating Recycling Center supports recovery; throughput depends on feedstock and power.",
    "energy_pioneer": "An operating Renewable Plant supplies clean power; unused surplus needs demand or a buyer.",
    "investment_district": "More starting cash and no completed building; purchased infrastructure takes time.",
}
REASONS = {
    "available": "Available", "queued": "Queued; awaiting execution", "applied": "Executed successfully",
    "insufficient_funds": "Not enough available treasury.", "development_locked": "Development prerequisite not reached.",
    "construction_slots_full": "Both construction slots are occupied.", "not_enough_time": "Too little match time remains.",
    "tile_occupied": "That tile is occupied.", "maximum_level": "Already at the highest level.",
    "policy_cooldown": "Wait for the waste-policy cooldown.", "reactivation_cooldown": "Wait before reactivating.",
    "no_eligible_buyer_need": "No eligible demand for this service.", "insufficient_seller_capacity": "Insufficient spare seller capacity.",
    "selection_window_closed": "Quote selection is not open now.", "quote_window_closed": "The quote submission window has closed.",
    "authentication_failed": "Session not accepted. It may have expired or the server restarted.",
    "invalid_join_code": "Room code is invalid or expired.", "not_ready": "All human players must be ready.",
    "cancel_project_first": "Cancel the active project first.", "invalid_price": "Use the listed price bounds and increment.",
    "duplicate_contract": "An active contract already exists for this pair and service.",
    "contract_limit": "A participant has reached its contract limit.", "contract_cooldown": "This trading pair is on cooldown.",
}
def name(key):
    return NAMES.get(key, str(key).replace("_", " ").capitalize()) if key else "None"


def units(value, digits=2):
    if not isinstance(value, int) or isinstance(value, bool):
        return "—"
    precision = 6 if digits and 0 < abs(value) < SCALE else digits
    rendered = f"{Decimal(value) / SCALE:,.{precision}f}"
    return rendered.rstrip("0").rstrip(".") if precision else rendered


def amount(text):
    if not isinstance(text, str) or len(text) > 24 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", text):
        raise ValueError("Use a positive decimal with at most six fractional digits.")
    whole, _, fraction = text.partition(".")
    value = int(whole) * SCALE + int(fraction.ljust(6, "0"))
    if not 0 < value <= 100_000_000_000:
        raise ValueError("Quantity must be greater than zero and at most 100,000.")
    return value


def reason(code):
    return REASONS.get(code, "Server response: " + name(code))


def clock(ticks):
    return f"{max(0, ticks)//60:02d}:{max(0, ticks)%60:02d}"


def effects_text(effects):
    labels = {
        "cost_micro": ("Base cost", " credits"), "duration_seconds": ("Duration", " seconds"),
        "upkeep_micro_per_minute": ("Upkeep", " credits/min"), "jobs": ("Jobs", ""),
        "demand_micro_E": ("Power demand", " E"), "generation_micro_E": ("Power capacity", " E"),
        "income_micro_per_minute": ("Revenue at full output", " credits/min"), "air_micro_per_minute": ("Air change", "/min"),
        "housing": ("Housing capacity", ""), "services": ("Service capacity", ""),
        "collection_per_minute": ("Collection capacity", " units/min"), "recycling_per_minute": ("Recyclable capacity", " units/min"),
        "reuse_per_minute": ("Reuse within recyclable capacity", " units/min"), "treatment_per_minute": ("Treatment capacity", " units/min"),
        "waste_per_minute": ("Waste generated at full output", " units/min"),
    }
    return "\n".join(f"{label}: {units(effects[k]) if 'micro' in k else effects[k]}{suffix}" for k, (label, suffix) in labels.items() if effects.get(k))


class Model:
    def __init__(self):
        self.connection = "Disconnected"
        self.seat = None
        self.room = ""
        self.catalogue = {}
        self.lobby = {}
        self.draft = {}
        self.view = {}
        self.final = None
        self.history = deque(maxlen=80)
        self.seen = deque(maxlen=256)
        self.actions = OrderedDict()
        self.correlations = {}
        self.recent = None
        self.view_fresh = False

    def note(self, text, key=None):
        if key is not None and key in self.seen:
            return
        if key is not None:
            self.seen.append(key)
        self.history.append(text)

    def ingest(self, m, session=None):
        if not isinstance(m, dict) or not isinstance(m.get("type"), str):
            self.note("Unexpected server response.")
            return
        kind = m["type"]
        if kind == "player_view":
            city=m.get("own_city")
            if not isinstance(city,dict) or not all(isinstance(city.get(k),int) and not isinstance(city[k],bool) for k in ("city_id","population","money_micro","materials_micro")) or not isinstance(city.get("foundation"),str) or not isinstance(m.get("tick"),int) or not isinstance(m.get("public"),dict):
                self.note("Incomplete city update ignored; retaining the last valid view.")
                return
        if kind == "seat_assigned":
            self.seat = m["seat"]
        elif kind == "lobby_created":
            self.room = m["join_code"]
        elif kind == "catalogue":
            self.catalogue = m
        elif kind == "lobby_state":
            self.lobby = m
        elif kind == "draft_state":
            self.draft = m
        elif kind == "match_cancelled":
            self.lobby = {}; self.draft = {}; self.view = {}; self.final = None
            self.seat = None; self.room = ""; self.actions.clear()
            self.note("Draft cancelled: a disconnected player did not return in time.")
        elif kind == "protocol_error":
            transmission = self.correlations.pop(m.get("correlation_id"), None)
            if isinstance(transmission, str):
                transmission = {"action_id": transmission, "session": session}
            record = self.actions.get((transmission or {}).get("action_id"))
            same_session = record is not None and (
                session is None or record.get("session") == session
            ) and (
                (transmission or {}).get("session") in (None, record.get("session"))
            )
            # A protocol error describes one transmission.  It cannot reverse an
            # authoritative admission or execution receipt for the action ID.
            if same_session and record.get("phase") == "request":
                record.update(status="rejected", phase="protocol")
            if record is not None and not same_session:
                return
            if m.get("code")=="authentication_failed":
                self.connection="Session rejected — expired credential or server restart"
            self.note(reason(m.get("code", "unknown")))
        elif kind == "action_ack":
            record = self.actions.get(m.get("action_id"))
            self.correlations.pop(m.get("correlation_id"), None)
            same_session = record is not None and (
                session is None or record.get("session") == session
            )
            if record is not None and not same_session:
                return
            current_phase = record.get("phase") if record else None
            incoming_phase = m["phase"]
            accept = same_session and (
                current_phase not in ("execution",)
                and (incoming_phase == "execution" or current_phase != "admission")
            )
            if accept:
                record["status"] = m["status"]
                record["phase"] = incoming_phase
            label = record["label"] if record else "Recovered action"
            self.note(label + ": " + reason(m["status"]), (m["action_id"], m["phase"], m["status"]))
        elif kind == "player_view":
            old = self.view
            if old.get("match_id") == m.get("match_id") and m.get("view_seq", 0) < old.get("view_seq", 0):
                return
            self.recent = None
            if old and old.get("match_id") != m.get("match_id"):
                self.actions.clear(); self.correlations.clear(); self.final = None; self.seen.clear()
            if old.get("match_id") == m.get("match_id") and old.get("tick", -1) < m["tick"]:
                before = old.get("own_city", {})
                self.recent = (m["tick"] - old["tick"], {
                    "Recyclables processed": m["own_city"].get("waste_totals", {}).get("recycled", 0) - before.get("waste_totals", {}).get("recycled", 0),
                    "Materials recovered": m["own_city"].get("recovered_micro", 0) - before.get("recovered_micro", 0),
                    "Organics treated": m["own_city"].get("waste_totals", {}).get("organic_processed", 0) - before.get("waste_totals", {}).get("organic_processed", 0),
                })
            self.view = m
            self.view_fresh = True
            city = m.get("own_city", {})
            if old.get("match_id") == m.get("match_id") and old.get("own_city"):
                delta = city["population"] - old["own_city"]["population"]
                if delta:
                    self.note(f"Population {delta:+d}; now {city['population']:,} residents.", ("pop",m["tick"]))
                for group,label in (("offers","Service offer"),("requests","Quote request")):
                    previous_ids={r["id"] for r in old.get("marketplace",{}).get(group,[])}
                    for row in m.get("marketplace",{}).get(group,[]):
                        if row["id"] not in previous_ids:
                            self.note(f"{label} {row['id']}: {name(row['type'])}. Inspect Market.",(group,row["id"]))
                previous_contracts={k["id"]:k for k in old["own_city"].get("contracts",[])}
                for contract in city.get("contracts",[]):
                    prior_contract=previous_contracts.get(contract["id"],{})
                    if not prior_contract:
                        self.note(f"Contract {contract['id']} started: {name(contract['type'])}.",("contract",contract["id"]))
                    if contract.get("delivered",0)>0 and not prior_contract.get("delivered",0):
                        self.note(f"Contract {contract['id']}: service delivered; usage payment {units(contract.get('paid'))} credits. Totals are in My Contracts.",("delivery",contract["id"]))
                old_buildings = {b["id"]: b for b in old["own_city"].get("buildings", [])}
                for b in city.get("buildings", []):
                    prior = old_buildings.get(b["id"], {})
                    if prior and (prior.get("status") != b.get("status") or prior.get("level") != b.get("level")):
                        self.note(f"{name(b['type'])}: {name(b['status'])}, level {b['level']}.", ("building",b["id"],b["status"],b["level"]))
            for event in m.get("public", {}).get("events", {}).get("active", []):
                self.note("Event active: " + name(event), ("event",event))
            for n in m.get("notifications", []):
                self.note(f"Contract {n.get('contract')}: {name(n.get('kind'))}", ("market", n.get("contract"), n.get("time"), n.get("kind")))
            if m.get("final_result"):
                self.final = m["final_result"]
        elif kind == "match_finished":
            self.final = m

    def remember(self, action_id, label, kind, payload):
        if len(self.actions) >= 128:
            resolved = next((aid for aid, r in self.actions.items() if r["status"] not in ("sent", "queued", "uncertain")), None)
            if resolved is None:
                raise ValueError("Too many unresolved actions. Reconnect and recover receipts first.")
            del self.actions[resolved]
            self.correlations = {
                key: value for key, value in self.correlations.items()
                if (value if isinstance(value, str) else value.get("action_id")) != resolved
            }
        self.actions[action_id] = dict(label=label, kind=kind, payload=dict(payload), status="sent", phase="request")
        self.note(label + ": sent; awaiting admission.")
