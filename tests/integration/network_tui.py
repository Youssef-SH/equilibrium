"""Short actual TUI + independent socket test. No accelerated simulation."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"client/terminal"))
from tui import EquilibriumApp, ActionDialog, JoinDialog
from textual.widgets import Button, Select, Input, DataTable, TabbedContent, Static
from network_authority import peer, safe
from presentation import units

async def until(predicate, timeout=5):
    async def poll():
        while not predicate():
            await asyncio.sleep(.03)
    await asyncio.wait_for(poll(),timeout)

async def press(app, pilot, selector):
    app.screen.query_one(selector).scroll_visible(animate=False,force=True)
    await pilot.pause(.2)
    # Actual mouse interaction at the same widgets used by a player.
    clicked = await pilot.click(selector)
    if not clicked:
        widget=app.screen.query_one(selector)
        raise AssertionError(f"Not clickable: {selector}; region={widget.region}; disabled={widget.disabled}; screen={app.screen.size}")

async def select_market(app, pilot, predicate=lambda row: True):
    key = next(key for key, row in app.market_rows.items() if predicate(row))
    table = app.q("#market-table", DataTable)
    table.focus()
    table.move_cursor(row=table.get_row_index(key))
    table.action_select_cursor()
    await until(lambda: app.market_key == key)
    await pilot.pause(.05)
    return key

async def confirm(app,pilot,kind):
    await until(lambda:isinstance(app.screen,ActionDialog))
    await press(app,pilot,"#check")
    await until(lambda:not app.screen.query_one("#confirm",Button).disabled)
    await press(app,pilot,"#confirm")
    await until(lambda:not isinstance(app.screen,ActionDialog))
    await until(lambda:any(r["kind"]==kind and r["phase"]=="execution" for r in app.model.actions.values()))
    await pilot.pause(.2)
    record=next(r for r in reversed(app.model.actions.values()) if r["kind"]==kind)
    assert record["status"]=="applied",record
    return record

async def scenario(url,directory):
    args=SimpleNamespace(url=url,session_file=str(directory/"human.session.json"),create=False,join=None,rejoin=False)
    app=EquilibriumApp(args)
    others=[]
    try:
        async with app.run_test(size=(100,36)) as pilot:
            await press(app,pilot,"#create")
            await until(lambda:bool(app.model.room) and bool(app.model.catalogue))
            assert len(app.model.catalogue["buildings"])==11
            for _ in range(3):
                p=await peer(url);others.append(p)
                await p.send("join_lobby",join_code=app.model.room)
                await p.wait("seat_assigned")
                await p.send("set_ready",ready=True)
            await press(app,pilot,"#ready")
            await until(lambda:not app.q("#start",Button).disabled)
            await press(app,pilot,"#start")
            foundations=["industrial_legacy","circular_foundation","energy_pioneer","investment_district"]
            for count in range(4):
                await until(lambda:bool(app.model.draft) and len([c for c in app.model.draft.get("choices",[]) if c])==count)
                turn=app.model.draft["turn_seat"]
                if turn==0:
                    app.q("#foundation",Select).value=foundations[0]
                    await pilot.pause(.05)
                    await press(app,pilot,"#choose")
                else:
                    await others[turn-1].send("choose_foundation",action_id=f"draft-{turn}",foundation=foundations[turn])
            await until(lambda:bool(app.model.view))
            safe(app.model.view,0)
            app.q("#grid",DataTable).move_cursor(row=1,column=1)
            await pilot.pause(.05)
            await press(app,pilot,"#build")
            app.screen.query_one("#building",Select).value="recovery_hub"
            await pilot.pause(.05)
            await confirm(app,pilot,"construct")
            await until(lambda:any(p and p["kind"]=="construct" for p in app.model.view["own_city"]["projects"]))
            assert app.tile==7
            await press(app,pilot,"#--content-tab-waste-tab")
            await pilot.pause(.05)
            assert "Organic waste processed" in str(app.q("#waste",Static).content)
            await press(app,pilot,"#policy")
            await confirm(app,pilot,"set_reduce_waste")
            await press(app,pilot,"#--content-tab-market-tab")
            await pilot.pause(.05)
            seller=others[0]
            await seller.send("submit_action",action_id="sell",kind="publish_offer",payload={"type":"recyclable","rate":5000000,"price":1000000})
            await seller.wait("action_ack",lambda m:m["action_id"]=="sell" and m["phase"]=="execution")
            await until(lambda:bool(app.market_rows))
            await select_market(app,pilot)
            await press(app,pilot,"#accept_quote")
            await confirm(app,pilot,"accept_quote")
            await until(lambda:any(k.get("delivered",0)>0 for k in app.model.view["own_city"]["contracts"]))
            app.q("#market-filter",Select).value="contracts"
            await until(lambda:any(r["category"]=="contract" for r in app.market_rows.values()))
            await select_market(app,pilot,lambda r:r["category"]=="contract")
            contract = next(k for k in app.model.view["own_city"]["contracts"] if k.get("delivered", 0) > 0)
            assert contract["paid"] > 0
            assert f"usage paid: {units(contract['paid'])} credits" in str(app.q("#market-detail",Static).content)
            # Disabling an owned, completed factory is ordinary building management.
            await press(app,pilot,"#--content-tab-city-tab")
            plant=next(b for b in app.model.view["own_city"]["buildings"] if b["type"]=="factory")
            app.q("#grid",DataTable).move_cursor(row=plant["tile"]//6,column=plant["tile"]%6)
            await pilot.pause(.05)
            await press(app,pilot,"#disable")
            await confirm(app,pilot,"disable")
            credential=json.loads(Path(args.session_file).read_text())
            await app.socket.close()
            await until(lambda:app.socket is None)
            await press(app,pilot,"#game-rejoin")
            await until(lambda:app.socket is not None and json.loads(Path(args.session_file).read_text())["rejoin_token"]!=credential["rejoin_token"])
            await until(lambda:app.model.connection=="Connected")
            assert app.model.seat==0
            assert Path(args.session_file).stat().st_mode & 0o777==0o600
            # Capture actual rendered screens from this live session.
            await press(app,pilot,"#--content-tab-city-tab")
            await pilot.pause(.05)
            app.q("#grid",DataTable).scroll_visible(animate=False,force=True)
            await pilot.pause(.1)
            await press(app,pilot,"#--content-tab-waste-tab")
            await pilot.pause(.05)
            app.q("#waste",Static).parent.scroll_home(animate=False)
            await pilot.pause(.1)
            print("PASS live TUI: create, ready, draft, construction preview/payment/project, policy, trade delivery/payment, disable, reconnect, private credentials",flush=True)
    finally:
        for p in others:
            p.ws.close_timeout=0.2
        await asyncio.gather(*(p.ws.close() for p in others),return_exceptions=True)

async def quotes_scenario(url,directory):
    args=SimpleNamespace(url=url,session_file=str(directory/"seller.session.json"),create=False,join=None,rejoin=False)
    app=EquilibriumApp(args)
    host=await peer(url)
    others=[host]
    await host.send("create_lobby")
    await host.wait("seat_assigned")
    code=(await host.wait("lobby_created"))["join_code"]
    try:
        async with app.run_test(size=(80,24)) as pilot:
            await press(app,pilot,"#join")
            await until(lambda:isinstance(app.screen,JoinDialog))
            app.screen.query_one("#join-code",Input).value=code
            await press(app,pilot,"#join-confirm")
            await until(lambda:app.model.seat==1 and bool(app.model.catalogue))
            for _ in range(2):
                p=await peer(url);others.append(p)
                await p.send("join_lobby",join_code=code)
                await p.wait("seat_assigned");await p.send("set_ready",ready=True)
            await host.send("set_ready",ready=True)
            await press(app,pilot,"#ready")
            await host.wait("lobby_state",lambda m:sum(r["ready"] for r in m["seats"])==4)
            await host.send("start_match")
            foundations=["industrial_legacy","circular_foundation","energy_pioneer","investment_district"]
            for count in range(4):
                await until(lambda:bool(app.model.draft) and len([c for c in app.model.draft.get("choices",[]) if c])==count)
                turn=app.model.draft["turn_seat"]
                if turn==1:
                    app.q("#foundation",Select).value=foundations[1]
                    await pilot.pause(.05);await press(app,pilot,"#choose")
                else:
                    await others[0 if turn==0 else turn-1].send("choose_foundation",action_id=f"draft-{turn}",foundation=foundations[turn])
            await until(lambda:bool(app.model.view))
            await press(app,pilot,"#--content-tab-market-tab")
            await press(app,pilot,"#publish_offer")
            app.screen.query_one("#rate",Input).value="5"
            app.screen.query_one("#price",Input).value="1"
            await confirm(app,pilot,"publish_offer")
            await until(lambda:bool(app.market_rows))
            await select_market(app,pilot,lambda r:r["category"]=="offer")
            await press(app,pilot,"#withdraw_offer")
            await confirm(app,pilot,"withdraw_offer")
            buyer=others[0]
            await buyer.send("submit_action",action_id="request",kind="request_quotes",payload={"type":"recyclable","rate":5000000})
            app.q("#market-filter",Select).value="requests"
            await until(lambda:any(r["category"]=="request" for r in app.market_rows.values()))
            await select_market(app,pilot,lambda r:r["category"]=="request")
            await press(app,pilot,"#submit_quote")
            app.screen.query_one("#price",Input).value="1"
            await confirm(app,pilot,"submit_quote")
            view=await buyer.wait("player_view",lambda m:any(o["request_id"]>=0 for o in m["marketplace"]["offers"]))
            offer=next(o for o in view["marketplace"]["offers"] if o["request_id"]>=0)
            request=next(r for r in view["marketplace"]["requests"] if r["id"]==offer["request_id"])
            await until(lambda:app.model.view["tick"]>=request["quote_until"],timeout=12)
            await buyer.send("submit_action",action_id="accept",kind="accept_quote",payload={"id":offer["id"]})
            outcome=await buyer.wait("action_ack",lambda m:m["action_id"]=="accept" and m["phase"]=="execution")
            assert outcome["status"]=="applied",outcome
            await until(lambda:any(k.get("delivered",0)>0 for k in app.model.view["own_city"]["contracts"]))
            app.q("#market-filter",Select).value="contracts"
            await until(lambda:any(r["category"]=="contract" for r in app.market_rows.values()))
            await select_market(app,pilot,lambda r:r["category"]=="contract")
            contract = next(k for k in app.model.view["own_city"]["contracts"] if k.get("delivered", 0) > 0)
            assert contract["paid"] > 0
            assert f"usage paid: {units(contract['paid'])} credits" in str(app.q("#market-detail",Static).content)
            print("PASS live 80x24 TUI: join, ready, Foundation, publish, withdraw, quote form, authoritative selection window, contract delivery/settlement",flush=True)
    finally:
        for p in others:p.ws.close_timeout=.2
        await asyncio.gather(*(p.ws.close() for p in others),return_exceptions=True)


async def two_tui_scenario(url, directory):
    """Two independent player UIs complete the authoritative draft and reconnect."""
    args_a=SimpleNamespace(url=url,session_file=str(directory/"player-a.session.json"),create=False,join=None,rejoin=False)
    args_b=SimpleNamespace(url=url,session_file=str(directory/"player-b.session.json"),create=False,join=None,rejoin=False)
    app_a,app_b=EquilibriumApp(args_a),EquilibriumApp(args_b)
    async with app_a.run_test(size=(90,30)) as pilot_a:
        async with app_b.run_test(size=(90,30)) as pilot_b:
            await press(app_a,pilot_a,"#create")
            await until(lambda:bool(app_a.model.room) and bool(app_a.model.catalogue))
            await press(app_b,pilot_b,"#join")
            await until(lambda:isinstance(app_b.screen,JoinDialog))
            app_b.screen.query_one("#join-code",Input).value=app_a.model.room
            await press(app_b,pilot_b,"#join-confirm")
            await until(lambda:app_b.model.seat==1 and bool(app_b.model.catalogue))
            await press(app_a,pilot_a,"#ready")
            await press(app_b,pilot_b,"#ready")
            await until(lambda:not app_a.q("#start",Button).disabled)
            await press(app_a,pilot_a,"#start")
            selected=set()
            while not (app_a.model.view and app_b.model.view):
                await until(lambda:bool(app_a.model.draft))
                turn=app_a.model.draft.get("turn_seat",-1)
                choices=app_a.model.draft.get("choices",[])
                if turn in (0,1) and turn not in selected and not choices[turn]:
                    app,pilot=(app_a,pilot_a) if turn==0 else (app_b,pilot_b)
                    await until(lambda:app.model.draft.get("turn_seat")==turn)
                    foundation=app.model.draft["available_foundations"][0]
                    app.q("#foundation",Select).value=foundation
                    await pilot.pause(.05)
                    await press(app,pilot,"#choose")
                    assert app.foundation_state in ("submitting","confirmed","starting","complete")
                    selected.add(turn)
                await asyncio.sleep(.03)
            safe(app_a.model.view,0);safe(app_b.model.view,1)
            assert app_a.q("#game").display and app_b.q("#game").display
            assert app_a.foundation_state=="complete" and app_b.foundation_state=="complete"
            await press(app_a,pilot_a,"#--content-tab-waste-tab")
            await press(app_a,pilot_a,"#policy")
            await confirm(app_a,pilot_a,"set_reduce_waste")
            await press(app_b,pilot_b,"#--content-tab-waste-tab")
            await press(app_b,pilot_b,"#policy")
            await confirm(app_b,pilot_b,"set_reduce_waste")
            before=json.loads(Path(args_b.session_file).read_text())
            await app_b.socket.close()
            await until(lambda:app_b.socket is None)
            await press(app_b,pilot_b,"#game-rejoin")
            await until(lambda:app_b.socket is not None and app_b.model.connection=="Connected")
            after=json.loads(Path(args_b.session_file).read_text())
            assert after["rejoin_token"]!=before["rejoin_token"]
            assert app_b.model.seat==1 and app_b.q("#game").display
            print("PASS two TUI players: create, join, ready, authoritative Foundation confirmations, automatic gameplay, actions, secure same-seat reconnect",flush=True)

async def main(output, quotes=False, two_player=False):
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="equilibrium-tui-") as tmp:
        directory=Path(tmp)
        with socket.socket() as s:
            s.bind(("127.0.0.1",0));port=s.getsockname()[1]
        with (directory/"server.log").open("w") as log:
            process=subprocess.Popen(["godot","--headless","--path",str(ROOT),"--log-file",str(directory/"godot.log"),"--script","server/main.gd","--","--port",str(port),"--stop-file",str(directory/"stop")],stdout=log,stderr=log)
            try:
                await until(lambda:"ONLINE_READY" in (directory/"server.log").read_text())
                selected = two_tui_scenario if two_player else quotes_scenario if quotes else scenario
                await asyncio.wait_for(selected(f"ws://127.0.0.1:{port}",directory),35)
                for name in ("city.svg","waste.svg"):
                    if (directory/name).exists(): (output/name).write_bytes((directory/name).read_bytes())
            finally:
                (directory/"stop").touch()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                text=(directory/"server.log").read_text()
                assert process.returncode==0 and "SCRIPT ERROR" not in text,text
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--output",type=Path,default=ROOT/"outputs/tui-preview")
    p.add_argument("--quotes",action="store_true")
    p.add_argument("--two-player",action="store_true")
    args=p.parse_args()
    asyncio.run(main(args.output,args.quotes,args.two_player))
