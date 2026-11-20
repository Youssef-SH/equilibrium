# Network terminal client

The interactive client uses Textual and connects only to the authoritative Godot server. It never runs a local simulation. The scripted CLI remains available.

## Install and launch

From the repository root, use Python 3.13.x exclusively:

~~~sh
python3.13 --version
uv sync --locked --group test
uv lock --check
~~~

Start the server in one terminal. Choose a stop-file path that does not already exist:

~~~sh
godot --headless --path . --log-file /tmp/equilibrium-server.log --script server/main.gd -- --bind 127.0.0.1 --port 9080 --seed 42 --stop-file /tmp/equilibrium-server.stop
~~~

Open the interactive menu in another terminal:

~~~sh
uv run --locked python client/terminal/client.py --tui --url ws://127.0.0.1:9080 --session-file /tmp/equilibrium-city1.session.json
~~~

Choose **Create Room**. Share the displayed room code. Each additional player launches an independent instance with a different session file, enters the code, and chooses **Join Room**:

~~~sh
uv run --locked python client/terminal/client.py --tui --session-file /tmp/equilibrium-city2.session.json
uv run --locked python client/terminal/client.py --tui --session-file /tmp/equilibrium-city3.session.json
uv run --locked python client/terminal/client.py --tui --session-file /tmp/equilibrium-city4.session.json
~~~

Run those commands in separate terminals. Without --session-file, each instance proposes a unique persistent path in its private per-user application-state directory. Retain that path for reconnecting. Existing session files are protected from accidental reuse when creating or joining a new room; select **Reconnect** or choose a new path.

All human players select **Ready**; the host selects **Start Match**. Start with any number from one through four humans. Empty seats become deterministic bots on start. Select an available Foundation when your city’s turn arrives.

## Playing

The header keeps population, authoritative rank, remaining time, Treasury, materials and connection state visible. The notice area shows recent feedback and an observed city problem. Server ticks drive the clock; displayed information may be stale after disconnection.

- **City:** navigate the 6×6 grid using arrows or mouse. Select **Build** on an empty tile; choose one of the eleven buildings. **Check / Preview** shows current legality, material savings, payable cost and duration. **Confirm** submits the action. On occupied tiles, use Upgrade, Enable, Disable, Demolish or Cancel Project. Details and the grid legend expand below the controls.
- **Waste & Energy:** inspect street waste separately from collected waste, recovery and treatment totals, materials available and spent, electricity, and environmental stocks. **Change Reduce Waste** explains the policy’s costs and trade-offs.
- **Market:** switch between available services, requests and quotes, and your contracts. Select a row before accepting, quoting, withdrawing, renewing or cancelling. Publishing and requesting use guided service/rate/price forms. Previews enforce server eligibility; no input requires JSON.
- **Rankings:** compare public city populations, Foundations, Satisfaction and Environment; inspect migration timing and event warnings.
- **Help / Feed:** read the objective and system explanations, review recent receipts and events, or retry an uncertain pending action using its original action ID.

Tab changes focus, Enter activates controls, Escape closes a dialog, and F1 opens help. Mouse controls are supported. Sections scroll at 80×24; building details, forms and market rows retain their selection during incoming updates.

A preview reserves nothing. **Sent** and **Queued** are pending; only **Executed successfully** establishes execution. A construction execution starts a project; its remaining time and later completion come from server state. Destructive actions require preview and confirmation. The server can still reject an action if conditions change after preview.

Waste quantities are units; waste rates are units/minute. Electricity supply and offered rates are E; electricity usage is billed per E-minute. Contract electricity delivery totals are E-seconds. Counters labeled cumulative are match totals, not rates. Satisfaction contributions are weighted points. No separate sustainability victory score is awarded.

## Reconnect and server lifecycle

Choose **Reconnect** after connection loss, or reopen the same private session:

~~~sh
uv run --locked python client/terminal/client.py --tui --url ws://127.0.0.1:9080 --rejoin --session-file /tmp/equilibrium-city1.session.json
~~~

Credentials use mode 0600 and are omitted from the UI and normal output. Keep them private. A waiting-lobby disconnect releases the seat; join again with a new session-file path. During drafting, the default reconnection grace is 30 seconds, configurable by the server. At expiry, the draft is cancelled and its credentials invalidated. During gameplay the city continues operating without becoming a bot.

The client does not silently replay actions as new commands. Rejoining recovers authoritative receipts. If delivery remains uncertain, **Retry Pending** resends one pending action with the same identifier. Server-process destruction cannot be recovered. A finished match shows the authoritative final ranking and circular-economy totals; another match requires a server restart.

## LAN and remote endpoints

For a controlled LAN playtest, bind the server to its LAN interface or all interfaces:

~~~sh
godot --headless --path . --log-file /tmp/equilibrium-lan-server.log --script server/main.gd -- --bind 0.0.0.0 --port 9080 --seed 42
~~~

On another machine, replace SERVER_LAN_IP with the host’s actual LAN address:

~~~sh
uv run --locked python client/terminal/client.py --tui --url ws://SERVER_LAN_IP:9080 --session-file /tmp/equilibrium-lan.session.json
~~~

Permit the chosen port through the host firewall. The URL field also accepts wss:// endpoints; TLS termination is an external server responsibility. LAN, WSS and independent-network operation require separate acceptance testing. Loopback tests do not establish those capabilities end to end.

## Scripted client

Omit --tui to preserve the command-line/JSON mode:

~~~sh
uv run --locked python client/terminal/client.py --url ws://127.0.0.1:9080 --create --session-file /tmp/equilibrium-script.session.json --json
~~~

Commands remain ready, unready, start, foundation ID, action KIND JSON, and quit. Example input after drafting:

~~~text
action set_reduce_waste {"enabled":true}
action construct {"building_type":"recycling_center","tile":6}
~~~

Scripted actions use ordinary authoritative validation. The quit command disconnects without stopping a running match.

## Focused checks

Each invocation is separate and bounded; none runs a full match:

~~~sh
godot --headless --path . --log-file /tmp/equilibrium-online-tests.log --script tests/integration/online_foundation.gd
godot --headless --path . --log-file /tmp/equilibrium-presentation-tests.log --script tests/integration/presentation.gd -- /tmp/equilibrium-ui-fixture.json
uv sync --locked --group test
uv run --locked --group test python -m unittest discover -s tests/integration -p test_terminal_tui.py -v
uv run --locked --group test python tests/integration/network_tui.py
uv run --locked --group test python tests/integration/network_tui.py --quotes
uv run --locked --group test python tests/integration/network_authority.py --terminal
uv run --locked python tools/check_repository.py
~~~

The Godot presentation probe exports a three-tick authoritative fixture and a separate final-boundary fixture for the Python UI tests. The latter skips earlier ticks and executes one actual finalization transition; it is not a completed live match. The network TUI checks launch a real server and operate actual UI controls. The quote case uses the real quote-selection window. See [server checks](../../server/README.md#checks) for independent-client occupancy and marketplace fixtures.

## Four-person acceptance

Use four separate session files and complete these steps together:

1. Create/join one private room, ready, start and choose unique Foundations.
2. Inspect population/rank and city warnings. Construct a building through its preview; observe admission, execution, project progress and completion.
3. Inspect building details and upgrade once its Development prerequisite is met. Try disabling/re-enabling a facility and confirm a deliberate cancellation or demolition.
4. Compare street and collected waste, recycling, materials recovery, biogas and electricity. Change Reduce Waste and inspect its funded state.
5. Publish an eligible spare service or request quotes. Have another player accept during the legal window. Inspect delivered quantities, payments, expiry, renewal and cancellation from both participant views.
6. Observe migration and compare public rankings. Disconnect one client and rejoin its existing city.
7. Complete the twenty-minute match and inspect final ranking and cumulative circular-economy totals.

A practical early trade uses Circular Foundation’s spare recyclable treatment and Industrial Legacy’s treatment need, subject to current capacity and policy. No deal is guaranteed profitable. Fewer humans follow the same procedure; bots fill empty seats.

The complete human match, first-time-player comprehension, real-network reliability and Android interaction quality remain manual acceptance work. Automated fixtures do not establish those outcomes.

## Python and session recovery

Python **3.13.x only** is supported for the terminal client and Python tests.
The standalone Godot 4.5.1 server does not require Python. Dependency pins are
unchanged and verified under Python 3.13 on Linux/WSL; native Windows execution
has not been verified in this environment.

Native Windows PowerShell, from the repository root:

```powershell
py -3.13 --version
uv sync --locked --group test
uv lock --check
uv run --locked python client\terminal\client.py --tui
```

Linux/WSL launch after the setup above:

```bash
uv run --locked python client/terminal/client.py --tui
```

Each TUI defaults to a unique persistent session file in `equilibrium` under
`$XDG_STATE_HOME` (or `~/.local/state`) on Linux, and Local AppData on Windows.
Keep the displayed path for later reconnects; explicit `--session-file` paths
still work. Session writes use a same-directory temporary file, `os.fchmod(0600)`,
flush/fsync and atomic replacement. New application directories use mode 0700.
Windows ACLs determine access; file mode 0600 alone is not a Unix-equivalent
privacy guarantee. Avoid shared or untrusted directories for explicit paths.

A save failure retains the current credential in memory and displays a persistent
Retry Saving Session button. Keep the application open until saving succeeds.
Reconnect in that instance uses the current in-memory credential. A crash between
server token rotation and durable local saving can still lose recovery access:
the protocol invalidates the old token immediately. It does not keep old tokens
valid or promise crash-safe authentication recovery. A server restart also
invalidates sessions. No credentials are printed.

Connected means the server has assigned an authenticated seat. Cached views are
marked stale after disconnect; the city keeps simulating. Pending actions keep
their original IDs and can only be retried in the same authenticated match.
A queued receipt is not an executed receipt. Help (F1) returns with Escape without
changing the current screen or selection. Previews are checks, never reservations.
