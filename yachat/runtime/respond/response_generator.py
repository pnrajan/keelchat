"""Response Generator: the outbound message. translate=False content verbatim and labeled; internal data fences never
reach the user. Language and tone are the Rewriter's (respond/rewriter.py)."""

from __future__ import annotations

import re

from yachat.runtime.context import OutboundMessage, TurnContext
from yachat.state.models import Turn

#: the fences prompts wrap untrusted data in (runtime.llm.untrusted); a model that copies one into a reply leaks it
#: fences that carry data: removed with their content (to the end when unclosed)
_BLOCKS = "tool_output|data|evidence|answer|reply|conversation|description"
#: any other tool-call markup a model may imitate: the tags are removed, the text between them kept
FENCE = re.compile(rf"<({_BLOCKS})\b[^>]*>.*?(</\1>|\Z)|</?(?:{_BLOCKS}|tool_\w+|function_\w+)\b[^>]*>", re.S)


def strip_fences(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", FENCE.sub("", text)).strip()


AS_POSTED = {"en": "as posted", "es": "tal como se publicó", "fr": "tel que publié", "de": "wie veröffentlicht", "pt": "como publicado"}


def verbatim_block(items: list[dict[str, object]], language: str) -> str:
    label = AS_POSTED.get(language, AS_POSTED["en"])
    lines = []
    for i, it in enumerate(items, 1):
        title = it.get("title") or it.get("name") or ""
        if it.get("type") == "choice":  # a survey option: its letter, the label as written
            lines.append(f"({it.get('option')}) {title}")
            continue
        loc = f" — {it['location']}" if it.get("location") else ""
        pay = f" — {it['pay']}" if it.get("pay") else ""
        lines.append(f"{i}. {title}{loc}{pay} ({label})")
    return "\n".join(lines)


class ResponseGenerator:
    name = "response_generator"

    async def run(self, ctx: TurnContext) -> TurnContext:
        r = ctx.node_result
        text = strip_fences(r.text if r else "")
        if r and r.verbatim:
            text = f"{text}\n{verbatim_block(r.verbatim, ctx.language)}"
        ctx.outbound = OutboundMessage(
            text=text,
            language=ctx.language,
            items=list(r.items) if r else [],
            end_of_conversation=ctx.state.finished,
            exit_reason=ctx.exit_reason,
        )
        q = ctx.state.open_question
        ctx.state.turns.append(
            Turn(
                id=ctx.turn_id,
                role="user",
                text=ctx.text,
                original_text=ctx.inbound.text if ctx.inbound.text != ctx.text else None,
                language=ctx.language,
                step=ctx.route,
                field=q.field if q else None,
                flagged=ctx.input_flagged,
            )
        )
        ctx.state.turns.append(Turn(role="agent", text=text, language=ctx.language, step=ctx.route, field=q.field if q else None))
        return ctx
