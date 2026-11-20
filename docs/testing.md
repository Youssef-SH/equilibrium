# Testing

Use Godot 4.5.1 from the repository root.

## Complete engine suite

```sh
godot --headless --path . --log-file /tmp/equilibrium-tests.log --script tests/run_all.gd
```

The entry point runs the production regression suites, Foundation-opening checks and five replay scenarios in separate forward/reversed command-arrival processes. Replay passes require byte-identical complete canonical state after 1,200 ticks. Script errors, parse errors, explicit failure markers and nonzero child-process exits fail the suite.

Coverage includes initialization, command ordering, fixed-point arithmetic, construction and upgrades, funding, staffing, power, waste mass accounting, recycling, biogas, environment, Development, events, migration, marketplace reservations and settlement, refunds, serialization and conservation invariants.

## Static repository check

```sh
uv run --locked python tools/check_repository.py
```

The checker verifies retained Godot resource paths, production dependency boundaries, `.gd.uid` pairing, Markdown links and the canonical B1 catalogue/structural shape. It uses only the Python standard library.

## Focused Godot suites

Individual suites can be run through the same Godot script entry point, for example:

```sh
godot --headless --path . --script tests/regression/resources.gd
godot --headless --path . --script tests/integration/market.gd
godot --headless --path . --script tests/integration/local_protocol.gd
```

`tests/support/` contains focused fixtures and scenario builders for production systems.

## Online multiplayer

The [server checks](../server/README.md#checks) cover protocol/schema agreement, authorization, scheduling, recursive player-view privacy, draft disconnect recovery, a complete marketplace settlement, real socket fixtures for all four human/bot occupancy combinations, and the terminal CLI. The [interactive client checks](../client/terminal/README.md#focused-checks) additionally exercise authenticated read-only previews, formatting, form stability and actual TUI controls over loopback sockets. Fixture-based final-results rendering is distinct from completing a live match. The complete injected-clock application test is a separate, longer check. Full-duration completion over continuously connected sockets is not accelerated through the public protocol.

## Clean import

For publication checks, copy retained source into a clean directory without `.godot/`, local environments or outputs, then run a Godot headless import and the complete suite. Retain `.gd.uid` sidecars with their scripts.

Install and verify the exact locked runtime and test dependencies with `uv sync --locked --group test` and `uv lock --check`.
