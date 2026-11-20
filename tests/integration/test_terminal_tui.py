"""Bounded presentation tests. Fixture comes from the authoritative short Godot probe."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import AsyncMock, Mock, patch, ANY

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client/terminal"))
from presentation import Model, units, amount, reason
from network import envelope, save_session, load_session, default_session_path
from tui import EquilibriumApp, ActionDialog, parse_packet, PacketError
from jsonschema import Draft202012Validator
from network_authority import safe
from textual.widgets import Button, Input, Select, Static, TabbedContent, DataTable

FIXTURE = Path(os.environ.get("EQUILIBRIUM_UI_FIXTURE", "/tmp/equilibrium-ui-fixture.json"))


class PresentationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(units(1234567890), "1,234.57")
        self.assertEqual(units(100000000), "100")
        self.assertEqual(units(-1500000), "-1.5")
        self.assertEqual(units(None), "—")
        self.assertEqual(units(1000), "0.001")
        self.assertEqual(units(1), "0.000001")
        self.assertEqual(amount("0.000001"), 1)
        for text in ("NaN", "-1", "0", "1.0000001", "abc", "Infinity", "1e1000000", "1.000000000000000000000000000000001", "1_000", "1e2", "100001", "1"*25):
            with self.assertRaises(ValueError):
                amount(text)

    def test_framing(self):
        self.assertEqual(envelope("preview_action",correlation_id="fixed")["correlation_id"], "fixed")

    def test_unknown_reason_and_bounded_dedup(self):
        self.assertIn("Server response:", reason("future_reason"))
        model = Model()
        model.note("one", "same"); model.note("two", "same")
        self.assertEqual(list(model.history), ["one"])
        for i in range(500):
            model.note(str(i), i)
        self.assertEqual(len(model.history), 80)
        self.assertEqual(len(model.seen), 256)
        model.ingest(None)
        for i in range(150):
            model.remember(str(i),"Build","construct",{})
            model.actions[str(i)]["status"] = "applied"
        self.assertEqual(len(model.actions),128)
        for record in model.actions.values(): record['status'] = 'uncertain'
        with self.assertRaises(ValueError): model.remember('overflow','Build','construct',{})
        self.assertEqual(len(model.actions),128)

    def test_incomplete_update_retains_state_and_auth_failure(self):
        fixture=json.loads(FIXTURE.read_text())
        model=Model();model.ingest(fixture["view"])
        model.ingest({"type":"player_view","tick":99,"own_city":{}})
        self.assertEqual(model.view,fixture["view"])
        model.ingest({"type":"protocol_error","code":"authentication_failed"})
        self.assertIn("Session rejected",model.connection)

    def test_new_schema_covers_payloads_and_selected_catalogue(self):
        schema=json.loads((ROOT/"shared/protocol_v1.schema.json").read_text())["$defs"]
        clients=schema["clientMessage"]["oneOf"]
        submit={m["properties"]["kind"]["const"] for m in clients if m["properties"]["type"]["const"]=="submit_action"}
        preview={m["properties"]["kind"]["const"] for m in clients if m["properties"]["type"]["const"]=="preview_action"}
        self.assertEqual(submit,preview)
        fixture=json.loads(FIXTURE.read_text())
        for key in ("catalogue","preview"):
            packet=fixture[key]
            spec=next(m for m in schema["serverMessage"]["oneOf"] if m["properties"]["type"]["const"]==packet["type"])
            self.assertTrue(set(spec["required"])<=packet.keys())
            self.assertTrue(packet.keys()<=spec["properties"].keys())
        self.assertEqual(set(fixture["catalogue"]["buildings"]),set(schema["serverMessage"]["oneOf"][-2]["properties"]["buildings"]["properties"]))

    def test_complete_schema_and_recursive_privacy(self):
        schema = json.loads((ROOT / 'shared/protocol_v1.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        fixture = json.loads(FIXTURE.read_text())
        for packet in [fixture['catalogue'], fixture['preview'], fixture['final_view'], *fixture['views']]:
            validator.validate(packet)
        for view in fixture['views']:
            safe(view, view['own_city']['city_id'])
        for packet in (fixture['catalogue'], fixture['preview']):
            def walk(value):
                if isinstance(value, dict):
                    self.assertFalse({'rng_state','pending_commands','rate_carries','rejoin_token','ledger','cities'} & value.keys())
                    for child in value.values(): walk(child)
                elif isinstance(value, list):
                    for child in value: walk(child)
            walk(packet)
        invalid = copy.deepcopy(fixture['catalogue'])
        invalid['buildings']['factory']['l1']['cost_micro'] = 'not an integer'
        self.assertFalse(validator.is_valid(invalid))

    def test_receipts_and_duplicate_snapshot(self):
        fixture=json.loads(FIXTURE.read_text())
        model=Model();model.remember("a","Build","construct",{})
        model.ingest(dict(type="action_ack",action_id="a",phase="admission",status="queued"))
        self.assertEqual(model.actions["a"]["status"],"queued")
        receipt=dict(type="action_ack",action_id="a",phase="execution",status="insufficient_funds")
        model.ingest(receipt);n=len(model.history);model.ingest(receipt)
        self.assertEqual(len(model.history),n)
        model.ingest(fixture["view"]);n=len(model.history);model.ingest(fixture["view"])
        self.assertEqual(len(model.history),n)

    def test_receipt_precedence_and_session_scope(self):
        session = (("ws://one", "room", 0), "match")
        model = Model()
        model.remember("success", "Build", "construct", {})
        model.actions["success"]["session"] = session
        model.correlations["late"] = {"action_id": "success", "session": session}
        model.ingest(dict(type="action_ack", action_id="success", phase="execution", status="applied"), session)
        model.ingest(dict(type="protocol_error", correlation_id="late", code="invalid_payload"), session)
        self.assertEqual((model.actions["success"]["phase"], model.actions["success"]["status"]), ("execution", "applied"))

        model.remember("failed", "Build", "construct", {})
        model.actions["failed"]["session"] = session
        model.ingest(dict(type="action_ack", action_id="failed", phase="execution", status="insufficient_funds"), session)
        model.ingest(dict(type="action_ack", action_id="failed", phase="admission", status="queued"), session)
        self.assertEqual(model.actions["failed"]["status"], "insufficient_funds")

        model.remember("retry", "Build", "construct", {})
        model.actions["retry"]["session"] = session
        model.correlations["first"] = {"action_id": "retry", "session": session}
        model.ingest(dict(type="protocol_error", correlation_id="first", code="invalid_payload"), session)
        self.assertEqual(model.actions["retry"]["phase"], "protocol")
        model.ingest(dict(type="action_ack", action_id="retry", phase="admission", status="queued"), session)
        model.correlations["second"] = {"action_id": "retry", "session": session}
        model.ingest(dict(type="protocol_error", correlation_id="second", code="invalid_payload"), session)
        self.assertEqual((model.actions["retry"]["phase"], model.actions["retry"]["status"]), ("admission", "queued"))

        model.ingest(dict(type="action_ack", action_id="unknown", phase="execution", status="applied"), session)
        stale = (("ws://two", "other", 1), "match")
        model.ingest(dict(type="action_ack", action_id="retry", phase="execution", status="applied"), stale)
        self.assertEqual(model.actions["retry"]["phase"], "admission")

    def test_packet_validation_is_bounded(self):
        for raw in ("not-json", "[]", '{"v":1,"type":"player_view","tick":1,"own_city":{},"public":{}}'):
            with self.assertRaises(PacketError):
                parse_packet(raw)


class UITests(unittest.IsolatedAsyncioTestCase):
    def fixture_app(self):
        fixture=json.loads(FIXTURE.read_text())
        app=EquilibriumApp()
        app.model.ingest(fixture["catalogue"])
        app.model.seat=0
        app.model.connection="Connected"
        app.model.ingest(fixture["view"])
        return app,fixture

    async def test_grid_form_updates_and_actual_confirmation(self):
        app,fixture=self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            grid=app.q("#grid",DataTable)
            grid.move_cursor(row=2,column=2)
            await pilot.pause()
            self.assertEqual(app.tile,14)
            self.assertTrue(all(c.width>=4 for c in grid.ordered_columns))
            app.q("#build",Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen,ActionDialog)
            dialog=app.screen
            dialog.query_one("#building",Select).value="residential_district"
            await pilot.pause()
            app.preview_action=AsyncMock(return_value=fixture["preview"])
            app.submit=AsyncMock()
            dialog.query_one("#check",Button).press()
            await pilot.pause()
            self.assertFalse(dialog.query_one("#confirm",Button).disabled)
            view=copy.deepcopy(fixture["view"]);view["tick"]+=1;view["view_seq"]+=1
            app.model.ingest(view);app.refresh_view()
            self.assertIs(app.screen,dialog)
            self.assertEqual(dialog.query_one("#building",Select).value,"residential_district")
            self.assertEqual(app.tile,14)
            dialog.query_one("#confirm",Button).press()
            await pilot.pause()
            app.submit.assert_awaited_once_with("construct",{"tile":14,"building_type":"residential_district"})

    async def test_market_forms_policy_and_selection(self):
        app,fixture=self.fixture_app()
        async with app.run_test(size=(100,32)) as pilot:
            app.refresh_view()
            app.q("#tabs",TabbedContent).active="market-tab"
            app.q("#publish_offer",Button).press()
            await pilot.pause()
            dialog=app.screen
            dialog.query_one("#rate",Input).value="10"
            dialog.query_one("#price",Input).value="1"
            await pilot.pause()
            app.model.ingest(fixture["view"]);app.refresh_view()
            self.assertEqual(dialog.query_one("#rate",Input).value,"10")
            self.assertEqual(dialog.read_payload(),{"type":"recyclable","rate":10000000,"price":1000000})
            await pilot.press("escape")
            app.q("#tabs",TabbedContent).active="waste-tab"
            app.q("#policy",Button).press()
            await pilot.pause()
            self.assertEqual(app.screen.kind,"set_reduce_waste")
            await pilot.press("escape")
            self.assertIn("CUMULATIVE MATCH TOTALS",str(app.q("#waste",Static).content))
            self.assertIn("collected awaiting treatment",str(app.q("#waste",Static).content))

    async def test_reconnect_stale_and_authoritative_final_fixture(self):
        app,fixture=self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            app.model.connection="Disconnected"
            app.refresh_view()
            self.assertTrue(app.q("#build",Button).disabled)
            self.assertIn("Disconnected",str(app.q("#status",Static).content))
            app.model.ingest(fixture["final_view"])
            app.refresh_view()
            self.assertTrue(app.q("#results").display)
            winner=fixture["final_view"]["final_result"]["winners"][0]+1
            self.assertIn(f"Winner: City {winner}",str(app.q("#final",Static).content))
            self.assertIn("Materials used in construction",str(app.q("#final",Static).content))
            app.q("#result-menu",Button).press()
            await pilot.pause()
            self.assertTrue(app.q("#menu").display)

    async def test_building_management_controls_and_retry_ids(self):
        app,fixture=self.fixture_app()
        view=next(v for v in fixture["views"] if v["own_city"]["buildings"])
        app.model.view=view;app.model.seat=view["own_city"]["city_id"]
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            plant=view["own_city"]["buildings"][0]
            app.q("#grid",DataTable).move_cursor(row=plant["tile"]//6,column=plant["tile"]%6)
            await pilot.pause()
            for command in ("upgrade","disable","demolish"):
                app.q("#"+command,Button).press()
                await pilot.pause(.05)
                self.assertIsInstance(app.screen,ActionDialog)
                self.assertEqual(app.screen.read_payload(),{"building_id":plant["id"]})
                self.assertTrue(app.screen.query_one("#confirm",Button).disabled)
                await pilot.press("escape")
                await pilot.pause(.05)
            self.assertTrue(app.q("#enable", Button).disabled)
            self.assertTrue(app.q("#cancel_project", Button).disabled)
            app.model.remember("stable-id","Build","construct",{"tile":30,"building_type":"green_space"})
            app.model.actions["stable-id"]["session"] = app.session_identity()
            app.wire=AsyncMock()
            app.q("#retry",Button).press()
            await pilot.pause()
            app.wire.assert_awaited_once_with("submit_action",correlation_id=ANY,action_id="stable-id",kind="construct",payload={"tile":30,"building_type":"green_space"})

    async def test_market_selection_and_remaining_controls(self):
        app,fixture=self.fixture_app()
        app.model.view=copy.deepcopy(app.model.view)
        app.model.view["own_city"]["contracts"]=[dict(id=8,buyer=0,seller=1,type="recyclable",rate=5000000,price=1000000,start_tick=1,end_tick=91,status="active",paid=100000,delivered=100000,last_delivery=100000,booking_micro=5000000,renewals=[])]
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            app.q("#market-filter",Select).value="contracts"
            await pilot.pause()
            table = app.q("#market-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            key=app.market_key
            app.refresh_view();await pilot.pause()
            self.assertEqual(app.market_key,key)
            self.assertEqual(app.q("#market-filter",Select).value,"contracts")
            for command in ("renew_contract","cancel_contract"):
                app.q("#"+command,Button).press()
                await pilot.pause(.05)
                self.assertEqual(app.screen.read_payload(),{"id":8})
                self.assertTrue(app.screen.query_one("#confirm",Button).disabled)
                await pilot.press("escape");await pilot.pause(.05)

    async def test_form_rejects_invalid_and_unknown_preview(self):
        app,fixture=self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            app.push_screen(ActionDialog("request_quotes"))
            await pilot.pause()
            app.screen.query_one("#rate",Input).value="garbage"
            app.screen.query_one("#check",Button).press()
            await pilot.pause()
            self.assertTrue(app.screen.query_one("#confirm",Button).disabled)
            self.assertIn("positive decimal",str(app.screen.query_one("#preview",Static).content))

class ReliabilityTests(unittest.TestCase):
    def test_python_and_atomic_storage(self):
        self.assertEqual(sys.version_info[:2], (3, 13))
        self.assertTrue(default_session_path().is_absolute())
        self.assertNotEqual(default_session_path(), default_session_path())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private' / 'city.session.json'
            old = dict(lobby_id='room', rejoin_token='old')
            new = dict(lobby_id='room', rejoin_token='new')
            save_session(path, old)
            self.assertEqual(load_session(path), old)
            with patch('network.os.replace', side_effect=OSError('failure')):
                with self.assertRaises(OSError): save_session(path, new)
            self.assertEqual(load_session(path), old)
            self.assertEqual(list(path.parent.iterdir()), [path])
            with patch('network.os.fsync', side_effect=OSError('failure')):
                with self.assertRaises(OSError): save_session(path, new)
            self.assertEqual(load_session(path), old)
            self.assertEqual(list(path.parent.iterdir()), [path])
            save_session(path, new)
            self.assertEqual(load_session(path), new)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                link = Path(directory) / 'link'
                link.symlink_to(path)
                with self.assertRaises(ValueError): save_session(link, old)
                with self.assertRaises(ValueError): load_session(link)

    def test_correlated_rejection(self):
        model = Model()
        model.remember('a', 'Build', 'construct', {})
        model.remember('b', 'Build', 'construct', {})
        model.correlations['c'] = 'a'
        model.ingest(dict(type='protocol_error', correlation_id='c', code='invalid_payload'))
        self.assertEqual(model.actions['a']['status'], 'rejected')
        self.assertEqual(model.actions['b']['status'], 'sent')

    def test_two_correlations_are_independent(self):
        model = Model()
        model.remember('a', 'Build', 'construct', {})
        model.remember('b', 'Policy', 'set_reduce_waste', {})
        model.correlations['ca'] = {'action_id':'a', 'session':None}
        model.correlations['cb'] = {'action_id':'b', 'session':None}
        model.ingest(dict(type='protocol_error', correlation_id='ca', code='invalid_payload'))
        model.ingest(dict(type='action_ack', correlation_id='cb', action_id='b', phase='admission', status='queued'))
        self.assertEqual(model.actions['a']['status'], 'rejected')
        self.assertEqual(model.actions['b']['status'], 'queued')

    def test_credential_failure_retains_memory(self):
        app = EquilibriumApp()
        app.credentials = dict(lobby_id='room', rejoin_token='sensitive-secret')
        app.credential_path = '/unused'
        with patch('tui.save_session', side_effect=OSError('sensitive-secret')):
            app.persist_credentials()
        self.assertTrue(app.persistence_failed)
        self.assertEqual(app.credentials['rejoin_token'], 'sensitive-secret')
        self.assertNotIn('sensitive-secret', str(app.model.history))
        with patch('tui.save_session'):
            app.persist_credentials()
        self.assertFalse(app.persistence_failed)


class ReliabilityUITests(unittest.IsolatedAsyncioTestCase):
    fixture_app = UITests.fixture_app

    class MessageSocket:
        def __init__(self, *messages):
            self.messages = list(messages)
            self.closed = False
        def __aiter__(self): return self
        async def __anext__(self):
            if not self.messages: raise StopAsyncIteration
            return self.messages.pop(0)
        async def close(self): self.closed = True

    async def test_receive_validation_and_programming_errors_are_separate(self):
        app = EquilibriumApp()
        malformed = self.MessageSocket('not-json-with-secret-token')
        app.socket = malformed
        app.refresh_view = Mock()
        await app.receive(malformed, '/unused', app.connection_generation)
        self.assertIn('Unexpected server data', app.model.history[-1])
        self.assertNotIn('secret-token', str(app.model.history))

        packet = json.dumps(dict(type='protocol_error',v=1,code='invalid_payload'))
        broken = self.MessageSocket(packet)
        app.socket = broken
        app.model.connection = 'Connected'
        app.refresh_view = Mock(side_effect=RuntimeError('render defect'))
        with self.assertRaisesRegex(RuntimeError, 'render defect'):
            await app.receive(broken, '/unused', app.connection_generation)

    async def test_stale_receive_cannot_resolve_new_waiter_or_disconnect_new_socket(self):
        app = EquilibriumApp()
        app.connection_generation = 2
        current = object()
        app.socket = current
        future = asyncio.get_running_loop().create_future()
        app.waiters['same-correlation'] = (2, future)
        old = self.MessageSocket(json.dumps(dict(type='action_preview',v=1,correlation_id='same-correlation',status='available',tick=1,kind='construct',payload={})))
        await app.receive(old, '/unused', 1)
        self.assertIs(app.socket, current)
        self.assertFalse(future.done())
        self.assertIn('same-correlation', app.waiters)

    async def test_help_preserves_context(self):
        app, _ = self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.show('menu')
            active = app.q('#tabs', TabbedContent).active
            app.action_help()
            await pilot.pause()
            await pilot.press('escape')
            await pilot.pause()
            self.assertTrue(app.q('#menu').display)
            self.assertEqual(app.q('#tabs', TabbedContent).active, active)

    async def test_preview_timeout_and_disconnect(self):
        app, _ = self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            app.push_screen(ActionDialog('set_reduce_waste', {'enabled':True}))
            await pilot.pause()
            app.preview_action = AsyncMock(return_value=dict(status='available',tick=3))
            app.screen.query_one('#check',Button).press(); await pilot.pause()
            self.assertFalse(app.screen.query_one('#confirm',Button).disabled)
            app.preview_action = AsyncMock(side_effect=asyncio.TimeoutError)
            app.screen.query_one('#check',Button).press(); await pilot.pause()
            self.assertTrue(app.screen.query_one('#confirm',Button).disabled)
            app.preview_action = AsyncMock(return_value=dict(status='available',tick=3))
            app.screen.query_one('#check',Button).press(); await pilot.pause()
            await app.disconnect()
            self.assertTrue(app.screen.query_one('#confirm',Button).disabled)
            self.assertFalse(app.can_act())

    async def test_late_preview_response_cannot_confirm_edited_form(self):
        app, _ = self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view()
            app.push_screen(ActionDialog('request_quotes'))
            await pilot.pause()
            future = asyncio.get_running_loop().create_future()
            async def delayed(*args): return await future
            app.preview_action = delayed
            app.screen.query_one('#rate',Input).value = '5'
            await pilot.pause()
            app.screen.query_one('#check',Button).press(); await pilot.pause()
            app.screen.query_one('#rate',Input).value = '6'; await pilot.pause()
            app.screen.query_one('#rate',Input).value = '5'; await pilot.pause()
            future.set_result(dict(status='available',tick=3))
            await pilot.pause()
            self.assertTrue(app.screen.query_one('#confirm',Button).disabled)

    async def test_removed_market_row_cannot_change_dialog_target(self):
        app, _ = self.fixture_app()
        app.model.view = copy.deepcopy(app.model.view)
        offers = [dict(id=i,seller=1,type='recyclable',rate=5000000,price=1000000,request_id=-1) for i in (8,9)]
        app.model.view['marketplace']['offers'] = offers
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view(); await pilot.pause()
            table = app.q('#market-table',DataTable)
            table.focus()
            table.move_cursor(row=1); await pilot.pause()
            await pilot.press("enter")
            self.assertEqual(app.market_key,'offer:9')
            app.q('#accept_quote',Button).press(); await pilot.pause()
            app.preview_action = AsyncMock(return_value=dict(status='available',tick=3))
            app.screen.query_one('#check',Button).press(); await pilot.pause()
            self.assertFalse(app.screen.query_one('#confirm',Button).disabled)
            app.model.view['marketplace']['offers'] = offers[:1]
            app.refresh_market(); await pilot.pause()
            self.assertIsNone(app.market_key)
            self.assertFalse(table.show_cursor)
            self.assertTrue(app.screen.query_one('#confirm',Button).disabled)
            self.assertTrue(app.screen.query_one('#check',Button).disabled)
            self.assertEqual(app.screen.read_payload(), {'id':9})

    async def test_market_preview_tracks_terms_but_ignores_usage_updates(self):
        app, _ = self.fixture_app()
        app.model.view = copy.deepcopy(app.model.view)
        offer = dict(id=8,seller=1,type='electricity',rate=5000000,price=1000000,request_id=-1,delivered=0)
        app.model.view['marketplace']['offers'] = [offer]
        async with app.run_test(size=(80,24)) as pilot:
            app.refresh_view(); await pilot.pause()
            table = app.q('#market-table',DataTable)
            table.focus(); table.move_cursor(row=0); await pilot.press('enter')
            app.q('#accept_quote',Button).press(); await pilot.pause()
            app.preview_action = AsyncMock(return_value=dict(status='available',tick=3))
            app.screen.query_one('#check',Button).press(); await pilot.pause()
            self.assertFalse(app.screen.query_one('#confirm',Button).disabled)
            app.model.view['marketplace']['offers'][0]['delivered'] = 1
            app.refresh_market(); await pilot.pause()
            self.assertFalse(app.screen.query_one('#confirm',Button).disabled)
            app.model.view['marketplace']['offers'][0]['price'] = 2000000
            app.refresh_market(); await pilot.pause()
            self.assertTrue(app.screen.query_one('#confirm',Button).disabled)
            self.assertIn('terms changed', str(app.screen.query_one('#preview',Static).content))

    async def test_failed_session_switch_preserves_previous_session(self):
        app, _ = self.fixture_app()
        class Socket:
            def __init__(self):
                self.messages = [dict(type='welcome',v=1), dict(type='protocol_error',v=1,code='authentication_failed')]
                self.closed = False
            async def send(self, raw): pass
            async def recv(self): return json.dumps(self.messages.pop(0))
            async def close(self): self.closed = True
        with tempfile.TemporaryDirectory() as directory:
            old_path = str(Path(directory)/'old.json')
            new_path = Path(directory)/'new.json'
            save_session(new_path, dict(lobby_id='other',rejoin_token='other-token'))
            app.endpoint='ws://old'; app.credential_path=old_path
            app.identity=('ws://old','old-room',0)
            app.credentials=dict(lobby_id='old-room',rejoin_token='old-token')
            app.model.remember('pending','Build','construct',{})
            async with app.run_test(size=(80,24)):
                app.q('#url',Input).value='ws://new'; app.q('#session',Input).value=str(new_path)
                with patch('tui.connect', AsyncMock(return_value=Socket())):
                    await app.open_connection('rejoin')
                self.assertEqual(app.credentials['rejoin_token'],'old-token')
                self.assertEqual(app.identity,('ws://old','old-room',0))
                self.assertIn('pending',app.model.actions)

    async def test_cancelled_connection_attempt_closes_candidate(self):
        app, _ = self.fixture_app()
        class WaitingSocket:
            def __init__(self):
                self.receiving = asyncio.Event(); self.closed = False
            async def send(self, raw): pass
            async def recv(self):
                self.receiving.set()
                await asyncio.Future()
            async def close(self): self.closed = True
        candidate = WaitingSocket()
        previous = dict(lobby_id='old-room',rejoin_token='old-token')
        app.endpoint='ws://old'; app.credential_path='/tmp/old.session.json'
        app.identity=('ws://old','old-room',0); app.credentials=previous
        with tempfile.TemporaryDirectory() as directory:
            async with app.run_test(size=(80,24)):
                app.q('#url',Input).value='ws://new'
                app.q('#session',Input).value=str(Path(directory)/'new.session.json')
                with patch('tui.connect', AsyncMock(return_value=candidate)):
                    attempt = asyncio.create_task(app.open_connection('create'))
                    await candidate.receiving.wait()
                    attempt.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await attempt
                self.assertTrue(candidate.closed)
                self.assertEqual(app.credentials, previous)
                self.assertEqual(app.identity, ('ws://old','old-room',0))

    async def test_two_stored_sessions_and_memory_reconnect(self):
        app, fixture = self.fixture_app()
        class Socket:
            def __init__(self, lobby):
                self.closed = asyncio.Event()
                self.messages = [
                    dict(type='welcome', v=1),
                    dict(type='seat_assigned', v=1, lobby_id=lobby, seat=0, rejoin_token='rotated-'+lobby),
                ]
            async def send(self, raw): pass
            async def recv(self): return json.dumps(self.messages.pop(0))
            async def close(self): self.closed.set()
            def __aiter__(self): return self
            async def __anext__(self):
                await self.closed.wait()
                raise StopAsyncIteration
        with tempfile.TemporaryDirectory() as directory:
            first, second = [Path(directory) / (n + '.session.json') for n in ('first','second')]
            one = dict(lobby_id='one', rejoin_token='token-one')
            two = dict(lobby_id='two', rejoin_token='token-two')
            save_session(first, one); save_session(second, two)
            async with app.run_test(size=(80,24)) as pilot:
                app.endpoint = app.q('#url',Input).value
                app.credential_path = str(first)
                app.identity = (app.endpoint, 'one', 0)
                app.credentials = one
                app.model.remember('pending','Build','construct',{})
                app.q('#session',Input).value = str(first)
                with patch('tui.connect', AsyncMock(return_value=Socket('one'))), patch('tui.load_session', side_effect=AssertionError('Must use current in-memory credential')):
                    await app.open_connection('rejoin')
                self.assertIn('pending',app.model.actions)
                self.assertFalse(app.can_act())
                app.q('#session',Input).value = str(second)
                with patch('tui.connect', AsyncMock(return_value=Socket('two'))):
                    await app.open_connection('rejoin')
                self.assertFalse(app.model.actions)
                self.assertFalse(app.model.view)
                self.assertIsNone(app.model.final)
                self.assertFalse(app.market_rows)
                await app.disconnect()

    async def test_authentication_and_session_isolation(self):
        app, fixture = self.fixture_app()
        async with app.run_test(size=(80,24)) as pilot:
            app.model.connection='Joining'; app.refresh_view()
            self.assertFalse(app.can_act())
            self.assertTrue(app.q('#build',Button).disabled)
            app.model.connection='Connected'
            app.identity=('ws://one','room-one',0)
            app.model.remember('pending','Build','construct',{})
            app.model.actions['pending']['session']=app.session_identity()
            await app.disconnect()
            self.assertIn('pending',app.model.actions)
            app.reset_session()
            app.identity=('ws://two','room-two',1)
            self.assertFalse(app.model.actions)
            self.assertFalse(app.model.view)
            self.assertIsNone(app.model.final)


if __name__=="__main__":
    unittest.main()
