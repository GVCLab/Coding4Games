"""Platform registry + `make_env(...)` dispatch.

Adding a new platform = (a) write a bundle factory matching the signature
below, (b) register it here. Env uses the shared driver interfaces.

Bundle factory signature:

    def my_bundle(
        process_name: str, *,
        env_fps: int,
        n_players: int,
        screenshot_backend: str | None = None,
        use_speedhack: bool = True,
    ) -> Bundle    # see drivers/base.py: (InputDriver, Renderer, ProcessLifecycle, TimeController)

Each factory accepts screenshot_backend and use_speedhack.
"""

from __future__ import annotations

import sys
from typing import Any
from typing import Callable
from typing import Optional

from coding4game_gui.benchmark.windows.drivers.base import Bundle


BundleFactory = Callable[..., Bundle]


def _windows_xinput_factory() -> BundleFactory:
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.bundle import windows_xinput_bundle
    return windows_xinput_bundle


def _windows_kbm_factory() -> BundleFactory:
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.bundle import windows_kbm_bundle
    return windows_kbm_bundle


# Lazy registry: each entry is a 0-arg callable returning the bundle factory.
# Import native Windows dependencies only when their driver is selected.
_LAZY_REGISTRY: dict[str, Callable[[], BundleFactory]] = {
    "windows-xinput": _windows_xinput_factory,
    "windows-kbm":    _windows_kbm_factory,
}


def list_platforms() -> list[str]:
    return sorted(_LAZY_REGISTRY.keys())


def resolve_platform(name: str) -> str:
    """Expand 'auto' to a concrete platform; validate other names."""
    if name == "auto":
        if sys.platform == "win32":
            return "windows-xinput"
        raise RuntimeError(
            f"--platform auto: no default for sys.platform={sys.platform!r}. "
            f"Pass --platform explicitly. Available: {list_platforms()}"
        )
    if name not in _LAZY_REGISTRY:
        raise ValueError(
            f"unknown platform {name!r}. Available: {list_platforms()}"
        )
    return name


def get_bundle_factory(name: str) -> BundleFactory:
    name = resolve_platform(name)
    return _LAZY_REGISTRY[name]()


def make_env(
    platform: str,
    process_name: str,
    *,
    env_fps: int = 10,
    n_players: int = 1,
    screenshot_backend: Optional[str] = None,
    use_speedhack: bool = True,
    **extra: Any,
):
    """Build an Env for the given platform.

    Forward driver-specific keyword arguments to the selected bundle factory.
    """
    from coding4game_gui.benchmark.windows.driver_env import Env

    factory = get_bundle_factory(platform)
    input_, renderer, lifecycle, timectl = factory(
        process_name,
        env_fps=env_fps,
        n_players=n_players,
        screenshot_backend=screenshot_backend,
        use_speedhack=use_speedhack,
        **extra,
    )
    return Env(
        input_=input_, renderer=renderer,
        lifecycle=lifecycle, timectl=timectl,
        env_fps=env_fps,
    )


__all__ = [
    "BundleFactory",
    "list_platforms",
    "resolve_platform",
    "get_bundle_factory",
    "make_env",
]
