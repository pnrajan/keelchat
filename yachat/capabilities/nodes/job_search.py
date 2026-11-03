from __future__ import annotations

from typing import Any, ClassVar

from yachat.capabilities.nodes.common import affirm, affirm_failed, answers_for, tool_failed
from yachat.registry.core import EmptyConfig, Node, NodeResult, node
from yachat.runtime.context import TurnContext

ASK_QUERY = ("role", "What role and location are you looking for?", "A job title and a city or ZIP")
ASK_LOCATION = ("location", "Where do you want to work (city or ZIP)?", "A city or ZIP")
ASK_PICK = ("job_pick", "Which job interests you? Reply with its number, or 'none'.", "A number from the list, or 'none'")
CONFIRM_LOCATION = "location__confirm"


def _split(query: str) -> tuple[str, str]:
    low = query.lower()
    for sep in (" in ", " en ", " near ", " cerca de "):
        if sep in low:
            i = low.index(sep)
            return query[:i].strip(), query[i + len(sep) :].strip()
    return query.strip(), ""


@node("job_search", tools=["job_search"], produces=["selected_job"])
class JobSearch(Node[EmptyConfig]):
    """Searches jobs by role and location and lets the candidate pick one. Job text is shown as posted (translate=False)."""

    fields: ClassVar[tuple[str, ...]] = ("role", "location")

    async def _search(self, ctx: TurnContext) -> NodeResult:
        role, location = str(ctx.state.fields.get("role") or ""), str(ctx.state.fields.get("location") or "")
        res = await ctx.services.gateway.call("job_search", {"role": role, "location": location})
        ctx.tool_calls += 1
        if not res.ok:
            if tool_failed(ctx, "job_search", res.error_kind):
                return NodeResult(
                    "Job search is still unavailable. Please come back later.",
                    goal="Apologise that job search is down and invite them back later.",
                    exit_reason="tool_unavailable",
                )
            return NodeResult(
                "Job search is unavailable right now. Please try again in a moment.\n" + ASK_QUERY[1],
                goal="Say the search failed for now, and ask for the role and location again.",
                ask=ASK_QUERY,
            )
        jobs: list[dict[str, Any]] = list(res.data or [])[:10]
        ctx.state.data["job_results"] = jobs
        if not jobs:
            return NodeResult(
                "I couldn't find matching jobs. Try another role or location.\n" + ASK_QUERY[1],
                goal="Say nothing matched, and ask for another role or location.",
                ask=ASK_QUERY,
            )
        where = f" in {location}" if location else ""
        return NodeResult(
            f"Here is what I found{where}:\n" + ASK_PICK[1],
            goal=f"Introduce the jobs found{where} (they are listed after your reply), and ask which one interests them.",
            items=jobs,
            verbatim=jobs,
            ask=ASK_PICK,
        )

    def _take_query(self, ctx: TurnContext, value: str) -> None:
        role, location = _split(value)
        ctx.state.fields["role"] = role or value
        if location:
            ctx.state.fields["location"] = location
            ctx.validated_answers["location"] = location

    async def act(self, ctx: TurnContext, config: EmptyConfig) -> NodeResult:
        st = ctx.state
        answers = answers_for(ctx, "job_search")
        q = st.open_question
        own = {k: v for k, v in answers.items() if k in (*self.fields, "job_pick", CONFIRM_LOCATION)}
        if answers and q and not own:  # the message answered other steps' fields (kept as prefilled); re-ask ours
            return NodeResult(q.ask, goal="Note what they told you, and ask the question.", ask=(q.field, q.ask, q.expect))
        if own and q and q.field == CONFIRM_LOCATION:
            value = own.get(CONFIRM_LOCATION) or own.get("location") or next(iter(own.values()))
            pre = st.data.pop("prefill", {})
            meant = "yes" if "location" in own and own["location"] == pre.get("value") else await affirm(ctx, value, q.ask)
            if meant == "no" and "location" not in own:
                return NodeResult(ASK_LOCATION[1], ask=ASK_LOCATION)
            if meant == "unclear" and "location" not in own and affirm_failed(ctx):  # could not read the reply: ask again
                st.data["prefill"] = pre
                return NodeResult(f"Sorry, I didn't catch that. {q.ask}", goal="Say you didn't catch that, and ask again.", ask=(q.field, q.ask, q.expect))
            st.fields["location"] = own["location"] if "location" in own and meant != "yes" else pre.get("value")
            return await self._search(ctx)
        if own and q and q.field == "location":
            st.fields["location"] = own.get("location") or next(iter(own.values()))
            return await self._search(ctx)
        if own and q and q.field == "role":
            self._take_query(ctx, own.get("role") or next(iter(own.values())))
            if "location" in own:
                st.fields["location"] = own["location"]
            return await self._ask_location_or_search(ctx)
        if own and q and q.field == "job_pick":
            if "job_pick" not in own:  # a fresh role/location at the pick stage is a new search, not a pick
                st.fields.update({k: own[k] for k in self.fields if k in own})
                return await self._search(ctx)
            return await self._pick(ctx, own["job_pick"])
        # entering (or re-entering) the step
        st.outputs.pop("selected_job", None)
        if st.data.pop("edited_field", None) == "location" and st.fields.get("role"):
            return await self._search(ctx)
        if st.fields.get("role"):
            return await self._ask_location_or_search(ctx)
        if "role" in st.prefilled:
            self._take_query(ctx, str(st.prefilled.pop("role")))
            return await self._ask_location_or_search(ctx)
        return NodeResult(ASK_QUERY[1], goal="Ask what role and where they want to work.", ask=ASK_QUERY)

    async def _ask_location_or_search(self, ctx: TurnContext) -> NodeResult:
        st = ctx.state
        if st.fields.get("location"):
            return await self._search(ctx)
        pre = st.prefilled.pop("location", None)
        if pre is None:
            rec = next((m for m in ctx.memory if m.item == "location" and m.weight() >= ctx.policy.defaults.stale_memory_threshold), None)
            if rec is not None:
                spec = ctx.policy.memory_item("location")
                if spec is not None and not spec.confirm_on_reuse:
                    st.fields["location"] = rec.value
                    return await self._search(ctx)
                pre = rec.value
        if pre is not None:
            st.data["prefill"] = {"field": "location", "value": pre}
            ask = f"You mentioned {pre}. Search for jobs there? (yes/no)"
            return NodeResult(ask, goal=f"Check whether to search for jobs in {pre}, which they mentioned.", ask=(CONFIRM_LOCATION, ask, "yes or no"))
        return await self._search(ctx)  # no location known: search broadly rather than ask another question

    async def _pick(self, ctx: TurnContext, value: str) -> NodeResult:
        st = ctx.state
        jobs: list[dict[str, Any]] = st.data.get("job_results", [])
        a = value.lower().strip(" .")
        if a in ("none", "no", "neither", "skip", "ninguno"):
            n = st.reasks.get("job_pick", 0) + 1
            st.reasks["job_pick"] = n
            if n >= ctx.policy.defaults.reasks:
                return NodeResult(
                    "No problem, we can pick a job another time.", goal="Say that's fine and they can pick a job another time.", exit_reason="max_reasks"
                )
            st.fields.pop("role", None)
            return NodeResult(
                "No problem. Tell me another role or location to search.", goal="Say that's fine, and ask for another role or location.", ask=ASK_QUERY
            )
        digits = "".join(ch for ch in a if ch.isdigit())
        idx = int(digits) - 1 if digits else -1
        if 0 <= idx < len(jobs):
            return NodeResult(
                f"Great choice: {jobs[idx].get('title')}.",
                goal=f"Confirm their pick, {jobs[idx].get('title')}, warmly in a few words.",
                outputs={"selected_job": jobs[idx]},
                complete=True,
            )
        if not digits and len(a.split()) >= 2:  # a new search instead of a pick
            self._take_query(ctx, value)
            return await self._ask_location_or_search(ctx)
        n = st.reasks.get("job_pick", 0) + 1
        st.reasks["job_pick"] = n
        if n >= ctx.policy.defaults.reasks:
            return NodeResult("I couldn't tell which job you meant.", goal="Say you couldn't tell which job they meant.", exit_reason="max_reasks")
        return NodeResult("Please reply with the job's number or 'none'.", goal="Ask them to pick a job by its number, or say none.", ask=ASK_PICK)
