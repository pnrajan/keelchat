"""The plain guide (docs/guide): every YAML example in it builds, and the broken ones fail with the error it quotes."""

from __future__ import annotations

import subprocess
import sys

import pytest

from keelchat.policy.compiler import build
from tests.conftest import ROOT

SNIPPETS = ROOT / "docs" / "guide" / "snippets"
#: examples that show a mistake, and the error the guide says you will see
BROKEN = {"03b_booking_broken.yaml": "needs a top-level `handoff:`", "07b_tool_unknown.yaml": "unregistered tool 'order_status'"}


@pytest.mark.parametrize("name", sorted(p.name for p in SNIPPETS.glob("*.yaml")))
def test_each_guide_example_builds_or_fails_as_the_guide_says(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from keelchat.registry.core import REGISTRY

    text = (SNIPPETS / name).read_text()
    if name in BROKEN:
        # as the guide runs it: `keelchat build`, in a fresh process (no tool from another example registered)
        cli = [sys.executable, "-c", "import sys; from keelchat.cli import main; sys.exit(main())"]
        run = subprocess.run([*cli, "build", str(SNIPPETS / name)], capture_output=True, text=True)
        assert run.returncode != 0 and BROKEN[name] in run.stdout + run.stderr, run.stdout + run.stderr
        assert BROKEN[name].replace("_", "\\_") in (ROOT / "docs" / "guide" / "building-agents.tex").read_text()  # quoted as is
        return
    if "order_status" in text:  # the guide's own tool, from snippets/my_tools
        monkeypatch.syspath_prepend(str(SNIPPETS))
        if "my_tools.orders" not in sys.modules:
            import my_tools.orders  # noqa: F401  # registers order_status
    REGISTRY.discover()
    assert build(text).agent


async def test_the_guides_vector_store_passes_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from keelchat.knowledge.vectorstore import VectorStoreConfig
    from keelchat.testing import check_vector_store

    spec = importlib.util.spec_from_file_location("guide_vector_store", SNIPPETS / "vector_store_demo.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    store_cls = next(v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == mod.__name__ and hasattr(v, "upsert"))
    await check_vector_store(store_cls(VectorStoreConfig(None, "chunks_guide", 4)))
