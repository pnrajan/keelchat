from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from keelchat.policy.errors import PolicyError, PolicyValidationError, loc_to_path
from keelchat.policy.models import AgentPolicy


class _Loader(yaml.SafeLoader):
    """Safe YAML where only true/false are booleans (YAML 1.2): a bare Yes, No, On or Off stays a word, so an answer
    option labelled Yes is not read as true."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"] for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def load_yaml(text: str) -> dict[str, Any]:
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506  # a SafeLoader subclass
    except yaml.YAMLError as e:  # unparseable YAML is a policy error with its line, not a server error
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "<root>"
        raise PolicyValidationError([PolicyError(where, f"invalid YAML: {getattr(e, 'problem', None) or e}")]) from None
    if not isinstance(data, dict):
        raise PolicyValidationError([PolicyError("<root>", "policy must be a mapping")])
    return data


def _path(err: Mapping[str, Any]) -> str:
    loc = tuple(err["loc"])
    # flow steps are parsed inside a field validator: surface "flow[i]" paths
    return loc_to_path(loc)


def parse_policy(data: dict[str, Any]) -> AgentPolicy:
    try:
        return AgentPolicy.model_validate(data)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            msg = err["msg"]
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            errors.append(PolicyError(_path(err), msg))
        raise PolicyValidationError(errors) from None


def load_policy(text: str) -> AgentPolicy:
    return parse_policy(load_yaml(text))


def load_policy_file(path: str | Path) -> AgentPolicy:
    return load_policy(Path(path).read_text(encoding="utf-8"))
