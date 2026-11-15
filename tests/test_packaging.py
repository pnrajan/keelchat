"""What `pip install keelchat` gives you: the default provider works, and a missing optional dependency names its extra."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from keelchat.optional import optional

ROOT = Path(__file__).resolve().parent.parent


def test_the_default_provider_is_in_the_core_install():
    """OpenRouter (through litellm) is the documented default; a bare install must not die on `import litellm`."""
    core = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.startswith("litellm") for d in core), core


def test_a_missing_optional_dependency_names_its_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # as if not installed
    with pytest.raises(RuntimeError, match=r'an s3:// dataset needs `pip install "keelchat\[api\]"`'):
        optional("boto3", "api", needed_for="an s3:// dataset")
    with pytest.raises(RuntimeError, match=r'boto3 is not installed: `pip install "keelchat\[api\]"`'):
        optional("boto3", "api")
    assert optional("json", "api").dumps({"ok": 1}) == '{"ok": 1}'  # an installed module is returned
