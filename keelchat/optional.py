"""Optional dependencies: imported where they are used, and missing ones say which extra installs them."""

from __future__ import annotations

import importlib
from types import ModuleType


def optional(module: str, extra: str, *, needed_for: str = "") -> ModuleType:
    """Import `module`; when it is not installed, raise a RuntimeError naming `pip install "keelchat[extra]"`."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        what = f"{needed_for} needs" if needed_for else f"{module} is not installed:"
        raise RuntimeError(f'{what} `pip install "keelchat[{extra}]"`') from e
