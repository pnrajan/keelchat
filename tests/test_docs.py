"""Every complete policy shown in the docs builds, so the docs can't drift from the policy language."""

from __future__ import annotations

import re

import pytest

from keelchat.policy.compiler import build
from tests.conftest import ROOT

DOCS = sorted([ROOT / "README.md", ROOT / "CONTRIBUTING.md", *(ROOT / "docs").glob("*.md"), *(ROOT / "plugins").rglob("*.md")])
BLOCK = re.compile(r"^```ya?ml\n(.*?)^```", re.M | re.S)


def policies() -> list[tuple[str, str]]:
    out = []
    for doc in DOCS:
        for i, m in enumerate(BLOCK.finditer(doc.read_text())):
            text = m.group(1)
            if re.search(r"^agent:", text, re.M) and re.search(r"^charter:", text, re.M) and "..." not in text and "[...]" not in text:
                out.append((f"{doc.relative_to(ROOT)}#{i}", text))
    return out


@pytest.mark.parametrize("where,text", policies(), ids=[w for w, _ in policies()])
def test_every_policy_in_the_docs_builds(where: str, text: str) -> None:
    from keelchat.registry.core import REGISTRY

    REGISTRY.discover()
    assert build(text).agent, where
