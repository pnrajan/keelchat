"""Named, replaceable parts of knowledge search: embedders, chunkers, indexing methods and vector stores.

Each kind has a decorator (`@embedder`, `@chunker`, `@indexer`, `@vector_store`) that registers a factory under a
name, and a setting that picks one by that name or by import path (`package.module:Name`). Packages listed in
KEELCHAT_CAPABILITY_PACKAGES, or declaring a `keelchat.capabilities` entry point, register theirs on import.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

#: modules with optional built-in parts (they import their libraries only when a part is built)
BUILTIN_MODULES = ("keelchat.knowledge.chonkie_parts", "keelchat.knowledge.stores")
_builtins_loaded = False
#: True while BUILTIN_MODULES import: their registrations never replace a part a user registered under that name
_loading_builtins = False


def load_builtins() -> None:
    global _builtins_loaded, _loading_builtins
    if not _builtins_loaded:
        _builtins_loaded = True
        _loading_builtins = True
        try:
            for name in BUILTIN_MODULES:
                importlib.import_module(name)
        finally:
            _loading_builtins = False


class Plugins[T]:
    """Factories for one kind of part, by name. `base` is the class every factory must return an instance of."""

    def __init__(self, kind: str, setting: str, base: type[Any], call: Callable[..., Any] | None = None):
        """`call(factory, *args)` builds a part when a kind passes its arguments in its own way (default: as given)."""
        self.kind, self.setting, self.base = kind, setting, base  # an ABC: checked with isinstance
        self.call = call or (lambda factory, *args, **kwargs: factory(*args, **kwargs))
        self.factories: dict[str, Callable[..., T]] = {}

    def register(self, name: str) -> Callable[[Any], Any]:
        """Decorate a subclass of the base (called with the factory's arguments) or a function returning one."""

        def add(obj: Any) -> Any:
            if isinstance(obj, type) and not issubclass(obj, self.base):
                article = "an" if self.base.__name__[0] in "AEIOU" else "a"
                raise TypeError(f"@{self.kind}({name!r}) needs {article} {self.base.__name__} subclass, got {obj.__name__}")
            if _loading_builtins and name in self.factories:
                return obj  # a user's part of the same name, registered first, stays in use
            self.factories[name] = obj
            return obj

        return add

    def names(self) -> list[str]:
        load_builtins()
        return sorted(self.factories)

    def has(self, choice: str) -> bool:
        """Whether `choice` names a registered part (an import path is taken on trust until built)."""
        return ":" in choice or self._find(choice) is not None

    def _find(self, name: str) -> Callable[..., T] | None:
        load_builtins()
        if name not in self.factories:
            from keelchat.registry.core import REGISTRY

            REGISTRY.discover()  # capability packages and entry points register theirs on import
        return self.factories.get(name)

    def make(self, choice: str, *args: Any, **kwargs: Any) -> T:
        """Build the part named `choice` (a registered name, or `package.module:Name`) with these arguments."""
        if ":" in choice:
            mod, _, attr = choice.partition(":")
            factory: Callable[..., T] = getattr(importlib.import_module(mod), attr)
        elif (found := self._find(choice)) is not None:
            factory = found
        else:
            raise ValueError(f"unknown {self.setting} {choice!r}; known: {', '.join(self.names())}")
        made: T = self.call(factory, *args, **kwargs)
        if not isinstance(made, self.base):
            article = "an" if self.base.__name__[0] in "AEIOU" else "a"
            raise TypeError(f"{self.setting} {choice!r} made a {type(made).__name__}, not {article} {self.base.__name__}")
        try:
            made.plugin_name = choice  # what it was chosen as, for reports (registry, logs)
        except AttributeError:  # slotted or frozen: reported by class name instead
            pass
        return made


def plugin_name(part: object) -> str:
    """The name a part was chosen by (`semantic`, `pkg.mod:Name`), else its class name."""
    return str(getattr(part, "plugin_name", "") or type(part).__name__)
