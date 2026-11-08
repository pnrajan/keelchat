"""The public demo's list of what a drafted bot needs connecting (examples/demo/needs.py)."""

from __future__ import annotations

from examples.demo.needs import from_policy
from tests.conftest import ROOT
from yachat.policy.compiler import build


def test_the_list_names_what_the_yaml_uses_and_how_to_connect_it() -> None:
    needs = {n.name: n for n in from_policy(build((ROOT / "examples" / "support_assistant.yaml").read_text()))}
    # the support example searches documents, hands over to people and remembers a contact preference
    assert {"A model key", "Your documents", "Somewhere for handed-over chats", "A database for what it remembers", "Where people talk to it"} <= set(needs)
    assert "preferred_contact" in needs["A database for what it remembers"].why
    assert all(n.how and n.docs.startswith("https://") and n.options and n.source == "yaml" for n in needs.values())


def test_a_bot_without_documents_or_handoff_is_not_asked_for_them() -> None:
    needs = {n.name for n in from_policy(build("agent: greeter\ncharter: 'Say hello. Out of scope: everything else.'\nflow: [greet_consent]\n"))}
    assert "Your documents" not in needs and "Somewhere for handed-over chats" not in needs
    assert "A model key" in needs


def test_the_landing_page_yaml_builds() -> None:
    """site/index.html shows a bot; it must be one the build accepts."""
    import html
    import re

    page = (ROOT / "site" / "index.html").read_text()
    shown = re.search(r'<pre><span class="k">agent:.*?</pre>', page, re.S)
    assert shown is not None
    assert build(html.unescape(re.sub(r"<[^>]+>", "", shown.group(0)))).agent == "support-assistant"
