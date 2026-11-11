"""keelchat: chatbots written in YAML and extended in Python.

What an extension needs, in one import:

    from keelchat import tool, node, Node, NodeResult, CapabilityConfig

Names load on first use, so `import keelchat` stays cheap and registers nothing by itself.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # explicit re-exports, so type checkers see them (mypy --strict included)
    from keelchat.capabilities.nodes.common import answers_for as answers_for
    from keelchat.capabilities.tools import ToolError as ToolError
    from keelchat.channels.base import Channel as Channel
    from keelchat.registry.contract import check_capability as check_capability
    from keelchat.registry.core import CapabilityConfig as CapabilityConfig
    from keelchat.registry.core import GuardContext as GuardContext
    from keelchat.registry.core import Guardrail as Guardrail
    from keelchat.registry.core import GuardResult as GuardResult
    from keelchat.registry.core import Node as Node
    from keelchat.registry.core import NodeResult as NodeResult
    from keelchat.registry.core import PreProcessor as PreProcessor
    from keelchat.registry.core import action as action
    from keelchat.registry.core import guardrail as guardrail
    from keelchat.registry.core import node as node
    from keelchat.registry.core import preprocessor as preprocessor
    from keelchat.registry.core import tool as tool
    from keelchat.runtime.context import TurnContext as TurnContext

_EXPORTS = {
    "tool": "keelchat.registry.core",
    "action": "keelchat.registry.core",
    "node": "keelchat.registry.core",
    "guardrail": "keelchat.registry.core",
    "preprocessor": "keelchat.registry.core",
    "CapabilityConfig": "keelchat.registry.core",
    "Node": "keelchat.registry.core",
    "NodeResult": "keelchat.registry.core",
    "Guardrail": "keelchat.registry.core",
    "GuardResult": "keelchat.registry.core",
    "GuardContext": "keelchat.registry.core",
    "PreProcessor": "keelchat.registry.core",
    "ToolError": "keelchat.capabilities.tools",
    "answers_for": "keelchat.capabilities.nodes.common",
    "TurnContext": "keelchat.runtime.context",
    "Channel": "keelchat.channels.base",
    "check_capability": "keelchat.registry.contract",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'keelchat' has no attribute {name!r}")
    return getattr(import_module(_EXPORTS[name]), name)
