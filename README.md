# EQUILIBRIUM

### Deterministic Circular-Economy Strategy Game

EQUILIBRIUM is a monorepo for a four-city online-first strategy game. Its authoritative Godot simulation, WebSocket server and network terminal client support private four-city matches with one to four humans and bots in vacant seats.

Over a fixed 1,200-second match, cities compete for a conserved regional population by developing housing, employment and services while managing electricity, waste, pollution, municipal finances and inter-city contracts.

Population moves at deterministic migration intervals according to Satisfaction, housing capacity and regional movement limits. Satisfaction derives from employment, housing, public services, electricity reliability and Environment. Construction, circular processing, energy supply and marketplace commitments therefore affect the same population-based victory objective through explicit economic and environmental consequences.

## Features

- Fixed one-second ticks and seeded deterministic command ordering.
- Transactional command validation and atomic tick commits.
- Four distinct Foundations and eleven upgradeable building types.
- Municipal finance, staffing, construction slots and fixed-point accounting.
- Electricity allocation, waste collection, recycling, biogas and landfill systems.
- Environmental stocks, Satisfaction, Development, migration and final ranking.
- Waste-treatment and electricity contracts with reservations, settlement and refunds.
- Canonical serialization, read-only projections and deterministic replay coverage.
- Headless execution and stable local lifecycle/JSON-lines interfaces.

## Architecture

Godot 4.5.1 runs the authoritative simulation without scenes, rendering or wall-clock dependencies.

- `simulation/core/` owns state, fixed-point arithmetic, seeded randomness, command admission, transactional execution and canonical serialization.
- `simulation/systems/` owns construction, finance, economy, power, waste, environment, marketplace, events and migration.
- `simulation/interfaces/` exposes Foundation drafting, local sessions and a versioned JSON-lines adapter.
- `simulation/projections/` contains read-only derived insights.
- `simulation/match.gd` composes the complete production engine.
- `server/` hosts the authoritative multiplayer runtime.
- `shared/` defines the versioned WebSocket JSON protocol.
- `client/terminal/` contains the interactive network TUI and scripted CLI.

See [architecture](docs/architecture.md) and [deterministic execution](docs/deterministic-execution.md).

## Requirements

- Godot 4.5.1
- Python 3.13.x exclusively for the static repository checker and optional network terminal client

The engine needs no Python packages. The repository checker, terminal client and Python tests use the locked uv environment in `pyproject.toml` and `uv.lock`.

## Run headlessly

From the repository root, run a complete match with the default seed:

```sh
godot --headless --path . --script simulation/headless_runner.gd
```

The runner accepts an optional integer seed and tick count from 0 through 1200:

```sh
godot --headless --path . --script simulation/headless_runner.gd -- 42 1200
```

It prints the canonical serialized state. The runner uses an explicit demonstration Foundation assignment; lifecycle drafting is available through the engine interface.

## Interactive multiplayer

Start the [authoritative server](server/README.md#run), install the [terminal dependencies](client/terminal/README.md#install-and-launch), then open the interactive client:

~~~sh
uv run --locked python client/terminal/client.py --tui
~~~

Create or join a private room from the menu. City construction, waste and energy, trading, rankings, reconnection and final results use guided controls. The [terminal guide](client/terminal/README.md) includes separate-client launch commands and the four-person acceptance procedure.

## Engine integration

Integrators can instantiate `simulation/match.gd` directly and use its initialization, command, stepping, snapshot and serialization boundary. `simulation/interfaces/lifecycle.gd` adds deterministic unique-Foundation drafting. `simulation/interfaces/local_protocol.gd` provides a local JSON-lines protocol through `tools/local_match.gd`.

See [engine interface](docs/engine-interface.md) for envelopes, operations and state ownership.

## Configuration

[B1.json](config/balance/B1.json) is the only live numerical balance configuration. The [structural specification](docs/EQUILIBRIUM_SPEC_v1.0.md) defines binding game rules. Do not duplicate either authority in application code or documentation.

## Testing

```sh
godot --headless --path . --log-file /tmp/equilibrium-tests.log --script tests/run_all.gd
uv run --locked python tools/check_repository.py
```

The Godot entry point runs production regression suites, opening checks and five independent-process 1,200-tick replay comparisons. The Python checker performs static resource, dependency-boundary, UID, documentation-link and B1-structure checks. See [testing](docs/testing.md) for focused commands.

## Scope

The existing simulation is the only gameplay authority. See [server operation](server/README.md) and [terminal client](client/terminal/README.md) for multiplayer launch commands. Graphical rendering, Android packaging, public hosting and process-crash persistence remain outside the implementation.
