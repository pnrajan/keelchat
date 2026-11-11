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
    from keelchat.knowledge.chunker import Chunker as Chunker
    from keelchat.knowledge.chunker import chunker as chunker
    from keelchat.knowledge.embedder import Embedder as Embedder
    from keelchat.knowledge.embedder import embedder as embedder
    from keelchat.knowledge.models import Chunk as Chunk
    from keelchat.knowledge.models import Document as Document
    from keelchat.knowledge.models import Hit as Hit
    from keelchat.knowledge.strategies import IndexContext as IndexContext
    from keelchat.knowledge.strategies import Indexer as Indexer
    from keelchat.knowledge.strategies import indexer as indexer
    from keelchat.knowledge.vectorstore import VectorIndex as VectorIndex
    from keelchat.knowledge.vectorstore import VectorStoreConfig as VectorStoreConfig
    from keelchat.knowledge.vectorstore import vector_store as vector_store
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
    "Embedder": "keelchat.knowledge.embedder",
    "embedder": "keelchat.knowledge.embedder",
    "Chunker": "keelchat.knowledge.chunker",
    "chunker": "keelchat.knowledge.chunker",
    "Indexer": "keelchat.knowledge.strategies",
    "indexer": "keelchat.knowledge.strategies",
    "IndexContext": "keelchat.knowledge.strategies",
    "VectorIndex": "keelchat.knowledge.vectorstore",
    "vector_store": "keelchat.knowledge.vectorstore",
    "VectorStoreConfig": "keelchat.knowledge.vectorstore",
    "Chunk": "keelchat.knowledge.models",
    "Document": "keelchat.knowledge.models",
    "Hit": "keelchat.knowledge.models",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'keelchat' has no attribute {name!r}")
    return getattr(import_module(_EXPORTS[name]), name)
