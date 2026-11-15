"""Platform-driver protocols + bundles.

The framework treats every platform as 4 small drivers behind protocols
defined in `base.py`:

    InputDriver  · Renderer  · ProcessLifecycle  · TimeController

A "bundle factory" in each platform subpackage constructs all four for
that platform. The `Env` class in `coding4game_gui.benchmark.windows.driver_env` holds one
instance of each and exposes one interface to the game and interactive runners.

Available bundles:
  - windows-xinput   (current Windows + vgamepad implementation)
  - windows-kbm      (Windows keyboard and mouse implementation)

Pick a platform with `make_env("windows-xinput", ...)` from
`coding4game_gui.benchmark.windows.drivers.registry`.
"""
