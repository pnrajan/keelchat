from __future__ import annotations

from keelchat.capabilities.nodes.common import is_no, is_yes
from keelchat.logging import get_logger
from keelchat.registry.core import PreProcessor, preprocessor
from keelchat.runtime.context import TurnContext
from keelchat.runtime.understand.language_rules import CONFIRM, KEPT, NAMES, SWITCHED, evidence, explicit_request, scores, unsupported_reply

log = get_logger("language")


@preprocessor("language", order=20)
class Language(PreProcessor):
    """Language is decided per session: explicit request or two confident turns, confirm first; names and bare replies are never evidence."""

    def _reask(self, ctx: TurnContext) -> str:
        q = ctx.state.open_question
        return f"\n{q.ask}" if q else ""

    async def run(self, ctx: TurnContext) -> TurnContext:
        st, policy, text = ctx.state, ctx.policy, ctx.text
        supported = policy.languages
        if st.pending_language:
            target = st.pending_language
            st.pending_language = None
            if is_yes(text):
                st.language = target
                st.language_evidence = []
                log.info("language switched", language=target)
                ctx.short_circuit_with(SWITCHED.get(target, SWITCHED["en"]) + self._reask(ctx))
                return ctx
            if is_no(text):
                ctx.short_circuit_with(KEPT.get(st.language, KEPT["en"]).format(lang=NAMES.get(st.language, st.language)) + self._reask(ctx))
                return ctx
        if not st.data.get("language_set"):
            st.data["language_set"] = True
            sc = scores(text)
            best = max(sc, key=lambda k: sc[k])
            if sc[best] >= 2 and best in supported:
                st.language = best
            elif sc[best] >= 2:
                st.language = policy.fallback_language
                ctx.short_circuit_with(unsupported_reply(st.language, supported), note=f"unsupported language {best}")
                return ctx
            else:
                st.language = policy.fallback_language
            log.info("session language", language=st.language)
            return ctx
        req = explicit_request(text)
        if req:
            if req not in supported:
                ctx.short_circuit_with(unsupported_reply(st.language, supported), note=f"unsupported language {req}")
            elif req != st.language:
                st.pending_language = req
                ctx.short_circuit_with(CONFIRM.get(req, CONFIRM["en"]))
            return ctx
        q = st.open_question
        ev = evidence(text, st.language, q.field if q else None)
        if ev is None:
            return ctx
        if ev not in supported:
            ctx.short_circuit_with(unsupported_reply(st.language, supported), note=f"unsupported language {ev}")
            return ctx
        st.language_evidence.append(ev)
        needed = policy.defaults.language_confident_turns
        if len(st.language_evidence) >= needed and all(e == ev for e in st.language_evidence[-needed:]):
            st.language_evidence = []
            st.pending_language = ev
            ctx.short_circuit_with(CONFIRM.get(ev, CONFIRM["en"]))
        return ctx
