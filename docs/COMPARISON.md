# How keelchat compares

Checked against each project's own documentation; corrections welcome in an issue.

| | **keelchat** | **Rasa Pro (CALM)** | **Parlant** | **LangGraph** | **Botpress** |
|---|---|---|---|---|---|
| **What it is** | Runtime for goal-oriented bots | Enterprise conversational AI platform | Framework for customer-facing agents | Library for agent workflows as graphs | Hosted bot builder |
| **You write** | One YAML policy per bot; Python for tools and steps | YAML flows plus Python actions | Python: guidelines (condition → action) and journeys | Python graph code: nodes, edges, state | Visual flows in a studio |
| **Who controls the conversation** | Code steps validate answers and decide; the model phrases replies and reads intent | Flows in code; the model reads the user's intent | The model, steered by the guidelines matched each turn | Your graph code; you build any control you need | Visual flows plus model nodes |
| **Limits and safety** | Built in and counted in code: re-asks, digressions, planner budget, guardrails, one tool gateway | Built in | Built in: guideline matching, canned replies | You build it | Platform features |
| **Channels** | Web widget, WhatsApp, SMS, Slack, phone, plus your own | Many connectors | Bring your own | Bring your own | Many, hosted |
| **Licence** | Apache-2.0 | Commercial; free developer edition with a monthly conversation cap. The older Rasa Open Source is Apache-2.0 and in maintenance | Apache-2.0 | MIT | Hosted product; SDK and CLI are MIT; self-hosted v12 retired |
| **Maturity** | Early: 0.2.0, alpha | Mature, widely deployed | Active, 3.x | Mature, large ecosystem | Mature, hosted |

**Pick keelchat** when bots must finish a defined job reliably, behaviour should live in a reviewable file rather than in
prompts or code, and you want many bots on one self-hosted runtime with per-tenant cost, audit and handoff.

**Pick something else**
- **Rasa Pro** for a proven enterprise platform with vendor support.
- **Parlant** if your agent is mostly open-ended and steered by many contextual rules rather than a sequence of steps.
- **LangGraph** for full programmatic control of a multi-agent workflow, or when there's no end-user conversation.
- **Botpress** for a hosted visual builder with no infrastructure to run.
