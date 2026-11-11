from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyError:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class PolicyValidationError(Exception):
    def __init__(self, errors: list[PolicyError]):
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


def loc_to_path(loc: tuple[int | str, ...], prefix: str = "") -> str:
    out = prefix
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "<root>"
