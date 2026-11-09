"""A policy in plain words, for the person reviewing a drafted chatbot. Computed from the compiled policy in code, so the
summary says exactly what the chatbot will do, never what a model thinks it might."""

from __future__ import annotations

from typing import Any

from yachat.policy.compiler import CompiledPolicy

LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "hi": "Hindi", "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ru": "Russian", "tr": "Turkish",
}  # fmt: skip
TRIGGERS = {
    "user_request": "the customer asks for a person",
    "max_reasks": "it still can't get an answer after a few tries",
    "disengaged": "the customer stops replying",
    "tool_unavailable": "a connected system is down",
}


def _words(name: str) -> str:
    return name.replace("_", " ").strip()


def _step(node: str, settings: dict[str, Any]) -> str:
    if node == "greet_consent":
        return "Greets the customer and asks if it may help."
    if node == "form_fill":
        fields = [_words(str(f.get("name", ""))) + (" (optional)" if f.get("optional") else "") for f in settings.get("fields") or []]
        return "Asks for: " + ", ".join(fields) + ". It asks again when an answer is unclear, a limited number of times."
    if node == "survey":
        qs = [str(q.get("prompt", "")) for q in settings.get("questions") or []]
        listed = "; ".join(f"“{q}”" for q in qs[:6]) + ("; …" if len(qs) > 6 else "")
        return f"Runs a survey of {len(qs)} question{'s' if len(qs) != 1 else ''}: {listed}"
    if node == "conversation_survey":
        obs = settings.get("objectives") or []
        listed = "; ".join(str(o.get("learn", "")).rstrip(".") + (" (as a 0-10 score)" if o.get("measure") == "nps" else "") for o in obs[:6])
        return f"Has a natural conversation to learn {len(obs)} thing{'s' if len(obs) != 1 else ''}: {listed}. It skips what they already said."
    if node == "knowledge":
        return "Answers questions from your content, and says so when your content doesn't cover one."
    if node == "analytics":
        return "Answers questions about your data, with the figures checked."
    if node == "job_search":
        return "Searches open jobs and helps the person apply."
    if node == "handoff":
        return "Passes the chat to a person."
    if node == "exit":
        return "Ends the conversation."
    return f"Runs the “{_words(node)}” step."


def plain_summary(policy: CompiledPolicy) -> list[str]:
    out = [f"{n}. {_step(s.node, s.settings)}" for n, s in enumerate(policy.steps, 1)]
    if not policy.steps:
        out.append("Holds an open conversation, without fixed steps.")
    step_tools = {t for s in policy.steps for t in s.tools}
    extras = [t for t in policy.allowed_tools if t not in step_tools]
    if "knowledge_search" in extras:
        out.append("Can answer side questions from your content at any point.")
    if others := [_words(t) for t in extras if t != "knowledge_search"]:
        out.append("Can use: " + ", ".join(others) + ".")
    out.append("Speaks: " + ", ".join(LANGUAGES.get(code, code) for code in policy.languages) + ".")
    if policy.handoff:
        out.append("Passes the chat to you when " + " or when ".join(TRIGGERS.get(w, _words(w)) for w in policy.handoff.when) + ".")
    else:
        out.append("Never passes the chat to a person.")
    if policy.memory:
        items = [_words(m.name) + (" (only with permission)" if m.sensitive else "") for m in policy.memory]
        out.append("Remembers between chats: " + ", ".join(items) + ".")
    for r in [*policy.rules, *(r for s in policy.steps for r in s.rules)]:
        when = r.when.rstrip(".")
        if when[:1].isupper() and when[1:2].islower():  # "The user ..." mid-sentence; "VIP ..." and "I ..." stay
            when = when[0].lower() + when[1:]
        out.append(f"When {when}: {r.then.rstrip('.')}.")
    if policy.small_talk != "off":
        out.append("Chats briefly when the customer makes small talk, then gets back to the task.")
    return out
