"""Command-line WebSocket player. No local simulation or economy calculations."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import uuid
import threading
import sys

if sys.version_info[:2] != (3, 13):
    raise SystemExit("EQUILIBRIUM requires Python 3.13.x. Launch with python3.13 or py -3.13.")

from network import connect, send as send_wire, save_session, load_session, session_data


async def run(args):
    credentials = Path(args.session_file) if args.session_file else None
    async with connect(args.url, max_size=1048576, proxy=None) as socket:
        async def send(message_type, **fields):
            await send_wire(socket, message_type, **fields)

        await send("hello", client="terminal", client_version="1.0")
        welcome = json.loads(await socket.recv())
        if welcome["type"] != "welcome":
            raise RuntimeError("Server rejected protocol negotiation.")
        if args.rejoin:
            if not credentials:
                raise ValueError("--rejoin requires --session-file")
            saved = load_session(credentials)
            await send("rejoin", lobby_id=saved["lobby_id"], rejoin_token=saved["rejoin_token"])
        elif args.create:
            await send("create_lobby")
        elif args.join:
            await send("join_lobby", join_code=args.join)

        current_credentials = None
        async def persist():
            if credentials and current_credentials:
                try:
                    save_session(credentials, current_credentials)
                except (OSError, ValueError):
                    print("Session not saved. Keep this process open; use save-session to retry.", file=sys.stderr, flush=True)

        async def receive():
            nonlocal current_credentials
            async for raw in socket:
                m = json.loads(raw)
                if m["type"] == "seat_assigned":
                    current_credentials = session_data(m)
                    await persist()
                    m = {k: v for k, v in m.items() if k != "rejoin_token"}
                if m["type"] == "player_view" and not args.json:
                    c = m["own_city"]
                    print(f"tick={m['tick']} city={c['city_id']} population={c['population']} "
                          f"money_micro={c['money_micro']} environment_micro={c['environment_micro']} "
                          f"development_micro={c['development_micro']}", flush=True)
                else:
                    print(json.dumps(m), flush=True)

        lines = asyncio.Queue()
        loop = asyncio.get_running_loop()
        def read_input():
            # Read the raw descriptor so a disconnected client can exit while
            # stdin is idle, without a daemon holding Python's buffered IO lock.
            pending = b""
            try:
                while True:
                    chunk = os.read(sys.stdin.fileno(), 4096)
                    if not chunk:
                        if pending:
                            loop.call_soon_threadsafe(lines.put_nowait, pending.decode("utf-8", errors="replace"))
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        loop.call_soon_threadsafe(lines.put_nowait, line.decode("utf-8", errors="replace"))
                        if line.strip() == b"quit":
                            return
            except (OSError, RuntimeError):
                pass
            finally:
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(lines.put_nowait, None)
                    except RuntimeError:
                        pass
        threading.Thread(target=read_input, daemon=True).start()

        async def commands():
            print('Commands: ready | unready | start | foundation ID | action KIND JSON | save-session | quit', flush=True)
            while True:
                line = await lines.get()
                if line is None:
                    await socket.close()
                    return
                command, _, value = line.strip().partition(" ")
                if command == "save-session":
                    await persist()
                elif command == "quit":
                    await socket.close()
                    return
                if command in ("ready", "unready"):
                    await send("set_ready", ready=command == "ready")
                elif command == "start":
                    await send("start_match")
                elif command == "foundation":
                    await send("choose_foundation", action_id=uuid.uuid4().hex, foundation=value)
                elif command == "action":
                    kind, _, payload = value.partition(" ")
                    try:
                        await send("submit_action", action_id=uuid.uuid4().hex, kind=kind, payload=json.loads(payload))
                    except json.JSONDecodeError as exc:
                        print(f"Invalid JSON: {exc}", flush=True)

        tasks = [asyncio.create_task(receive()), asyncio.create_task(commands())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled():
                task.result()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="ws://127.0.0.1:9080")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--create", action="store_true")
    group.add_argument("--join")
    group.add_argument("--rejoin", action="store_true")
    p.add_argument("--session-file", help="Private credential file, created with mode 0600")
    p.add_argument("--json", action="store_true")
    p.add_argument("--tui", action="store_true", help="Launch the interactive network client")
    args = p.parse_args()
    if args.tui:
        from tui import EquilibriumApp
        EquilibriumApp(args).run()
        return
    try:
        asyncio.run(run(args))
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
