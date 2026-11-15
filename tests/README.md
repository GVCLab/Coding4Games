# Software checks

Run the suite from the source checkout with the development dependencies:

    python -B -m pytest -q -rs tests

| Scope | Tests |
|---|---|
| Registered scenarios, startup, controls, save handling, and scoring | tests/game/ |
| Development, independent tests, Direct, budgets, and run evidence | tests/benchmark/ |
| WSL2 environments, dependency reconstruction, and recovery | tests/wsl2/ |
| Operator menu, menu recording, configuration, and source release | tests/tools/ |
| Interactive runners and standalone client | tests/interactive/, tests/runtime_client/ |

The catalog check resolves startup settings and builds the registered scorer
for each of the 15 scenarios. Operator checks preview development and
independent testing for each scenario. Shared runner tests exercise selection,
archive identity, and model-free testing with simulated runtime dependencies.

Run recorder tests on a Windows desktop with Tcl/Tk while real games are
closed. Tests backed by local checkpoints, recordings, OCR, or compiled
helpers report missing prerequisites through pytest skip reasons. Real CLI,
game, and isolation acceptance follow the
[evidence contract](../docs/contracts/EVIDENCE_AND_ACCEPTANCE.md).
