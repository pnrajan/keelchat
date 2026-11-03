"""Discovery: every module under yachat/capabilities, every package in YACHAT_CAPABILITY_PACKAGES, every
`yachat.capabilities` entry point, then the tools of every MCP server in YACHAT_MCP_SERVERS."""

from __future__ import annotations

import importlib
import os
import pkgutil
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from yachat.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from yachat.registry.core import Registry

log = get_logger("registry")


def _import_tree(name: str) -> list[str]:
    """Import a package and every module under it, so their decorators register."""
    pkg = importlib.import_module(name)
    loaded = [name]
    for mod in pkgutil.walk_packages(getattr(pkg, "__path__", []), name + "."):
        importlib.import_module(mod.name)
        loaded.append(mod.name)
    return loaded


def discover_builtin() -> list[str]:
    return _import_tree("yachat.capabilities")  # a broken built-in module is a bug: let it raise


def discover_packages(names: list[str]) -> list[str]:
    """Import each package (and its submodules) so its decorators register."""
    loaded = []
    for name in names:
        try:
            _import_tree(name)
            loaded.append(name)
        except Exception as e:  # a broken package must not break discovery
            log.error("capability package failed", package=name, error=f"{e.__class__.__name__}: {e}")
    return loaded


def packages_from_env() -> list[str]:
    return [p.strip() for p in os.getenv("YACHAT_CAPABILITY_PACKAGES", "").split(",") if p.strip()]


def discover_entry_points(group: str) -> list[str]:
    loaded = []
    for ep in entry_points(group=group):
        try:
            obj = ep.load()
            if callable(obj) and not isinstance(obj, type):
                obj()  # a function entry point registers on call
            loaded.append(ep.name)
        except Exception as e:  # a broken contributed package must not break discovery
            log.error("entry point failed", entry_point=ep.name, error=f"{e.__class__.__name__}: {e}")
    return loaded


def discover_all(registry: Registry) -> None:
    builtin = discover_builtin()
    packages = discover_packages(packages_from_env())
    eps = discover_entry_points(registry.ENTRY_POINT_GROUP)
    from yachat.registry.mcp import register_mcp_servers

    mcp_tools = register_mcp_servers(registry)
    log.info(
        "registry discovered",
        modules=len(builtin),
        packages=packages,
        entry_points=eps,
        mcp_tools=mcp_tools,
        tools=registry.tool_names(),
        nodes=registry.node_names(),
    )
