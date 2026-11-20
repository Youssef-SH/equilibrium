# Authoritative online server

The root Godot 4.5.1 project hosts one private lobby/match per process. One to four human WebSocket clients own distinct seats. When the ready host starts, empty seats become deterministic server bots. All four seats draft through the existing lifecycle; every action uses the existing engine envelope and validator.

## Run

From the repository root:

```sh
godot --headless --path . --log-file /tmp/equilibrium-server.log --script server/main.gd -- --bind 127.0.0.1 --port 9080 --seed 42 --stop-file /tmp/equilibrium-server.stop
```

Choose a stop-file path that does not already exist. Create it to request graceful shutdown:

```sh
touch /tmp/equilibrium-server.stop
```

The stop file is a local operator control, never a network message. Remove an old stop file before a subsequent run. The listener address and port are configurable; use a LAN interface or `0.0.0.0` only when intentionally exposing it. WS loopback is tested. WSS requires an external TLS endpoint; LAN, Android and cross-network internet operation have not been tested.

See the [terminal client](../client/terminal/README.md) and [protocol artifacts](../shared/README.md).

## Ownership and timing

A single event loop drains input before stepping due ticks. `Time.get_ticks_msec()` supplies the monotonic clock. Ticks retain their one-second economic meaning. At most four overdue ticks run per iteration; remaining debt is retained. No client can advance time. A failed engine tick stops the session and reports failure. At tick 1200 the scheduler stops and final results are published.

Before start, a disconnected player releases its seat and credential. The lowest connected occupied seat becomes host; the last departure closes the lobby. During play, seats remain reserved and the human is never replaced by a bot. During drafting, a disconnected human has 30 seconds to rejoin by default; expiry cancels and cleans the incomplete draft and invalidates its credentials. The operator may set `--draft-grace-ms MILLISECONDS` to a value of at least 100.

Rejoin credentials expire after two hours, rotate on rejoin, and revoke the former socket. Rejoin sends the current view/draft plus receipts for the latest 64 actions; retry an older identical action to retrieve its retained receipts. At most 4096 distinct actions are retained per seat for the session. There is no process-crash persistence.

Bots reuse `LocalBot.PLAN`: one candidate construction every 90 ticks, submitted only if the engine preview permits it. This is a minimal opponent, not competitive AI. Bots receive no grants and never control human seats.

## Checks

```sh
godot --headless --path . --log-file /tmp/equilibrium-online-tests.log --script tests/integration/online_foundation.gd
uv run --locked python tools/check_repository.py
```

After installing the terminal requirements in the Python 3.13 `.venv`, run real socket cases separately:

```sh
uv run --locked --group test python tests/integration/network_authority.py --humans 2
uv run --locked --group test python tests/integration/network_authority.py --humans 1
uv run --locked --group test python tests/integration/network_authority.py --humans 3
uv run --locked --group test python tests/integration/network_authority.py --humans 4
uv run --locked --group test python tests/integration/network_authority.py --humans 0
uv run --locked --group test python tests/integration/network_authority.py --marketplace
uv run --locked --group test python tests/integration/network_authority.py --draft-disconnect
uv run --locked --group test python tests/integration/network_authority.py --terminal
```

`--humans 0` selects transport/host-departure edge tests; it is not a supported zero-human lobby. Each case launches a real server, creates independent WebSocket connections and requests graceful shutdown. It fails nonzero on assertion, timeout, server script error or abnormal shutdown.

`--terminal` launches the actual terminal program with piped input, exercises lobby creation, readiness, drafting and a policy action, checks private credential-file permissions and verifies clean exit with stdin still open.

The complete injected-clock test is separate and may take longer than 30 seconds. It executes all 1200 real engine ticks without waiting for wall-clock seconds:

```sh
godot --headless --path . --log-file /tmp/equilibrium-online-completion.log --script tests/integration/online_completion.gd
```

## Limitations

The server hosts one lobby and one match per process, and a completed match requires a process restart before another lobby can be hosted. Bots provide basic deterministic opposition rather than competitive play. There are no user accounts or process-crash recovery.

Loopback WebSocket operation is covered by the fixtures above. LAN, WSS, Android and independent-network operation have not been validated. Full-duration matches over continuously connected WebSocket clients have also not been validated.
