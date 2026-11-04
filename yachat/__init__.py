"""yachat: chatbots written in YAML and extended in Python.

What an extension needs, in one import:

    from yachat import tool, node, Node, NodeResult, CapabilityConfig

Names load on first use, so `import yachat` stays cheap and registers nothing by itself.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # explicit re-exports, so type checkers see them (mypy --strict included)
    from yachat.capabilities.nodes.common import answers_for as answers_for
    from yachat.capabilities.tools import ToolError as ToolError
    from yachat.channels.base import Channel as Channel
    from yachat.registry.contract import check_capability as check_capability
    from yachat.registry.core import CapabilityConfig as CapabilityConfig
    from yachat.registry.core import GuardContext as GuardContext
    from yachat.registry.core import Guardrail as Guardrail
    from yachat.registry.core import GuardResult as GuardResult
    from yachat.registry.core import Node as Node
    from yachat.registry.core import NodeResult as NodeResult
    from yachat.registry.core import PreProcessor as PreProcessor
    from yachat.registry.core import action as action
    from yachat.registry.core import guardrail as guardrail
    from yachat.registry.core import node as node
    from yachat.registry.core import preprocessor as preprocessor
    from yachat.registry.core import tool as tool
    from yachat.runtime.context import TurnContext as TurnContext

_EXPORTS = {
    "tool": "yachat.registry.core",
    "action": "yachat.registry.core",
    "node": "yachat.registry.core",
    "guardrail": "yachat.registry.core",
    "preprocessor": "yachat.registry.core",
    "CapabilityConfig": "yachat.registry.core",
    "Node": "yachat.registry.core",
    "NodeResult": "yachat.registry.core",
    "Guardrail": "yachat.registry.core",
    "GuardResult": "yachat.registry.core",
    "GuardContext": "yachat.registry.core",
    "PreProcessor": "yachat.registry.core",
    "ToolError": "yachat.capabilities.tools",
    "answers_for": "yachat.capabilities.nodes.common",
    "TurnContext": "yachat.runtime.context",
    "Channel": "yachat.channels.base",
    "check_capability": "yachat.registry.contract",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'yachat' has no attribute {name!r}")
    return getattr(import_module(_EXPORTS[name]), name)
