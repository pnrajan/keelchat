"""What a drafted bot needs connecting before real people use it, with suggestions for each.

Two sources: what the YAML itself uses (a model, documents, a handoff destination, memory, a channel), read in code
from the compiled policy; and the outside systems the description implies but the YAML can't reach yet (a calendar, a
CRM, an order system), suggested by the model with real products to pick from and how keelchat connects to them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from keelchat.policy.compiler import CompiledPolicy
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig, untrusted

Source = Literal["yaml", "suggested"]
Connect = Literal["config", "tool", "mcp", "webhook", "built-in"]

DOCS = "https://github.com/pnrajan/keelchat/blob/main/docs"
HOW: dict[str, tuple[str, str]] = {  # how each kind of connection is made, and where it's explained
    "config": ("Set it in .env", f"{DOCS}/REFERENCE.md"),
    "tool": ("Write a Python tool: one decorated function that calls its API", f"{DOCS}/ADDING_A_CAPABILITY.md"),
    "mcp": ("Point keelchat at its MCP server; its tools appear as <alias>.<tool>", f"{DOCS}/ADDING_A_CAPABILITY.md"),
    "webhook": ("Send handoffs to it with the handoff_webhook tool (HANDOFF_URL)", f"{DOCS}/FEATURES.md"),
    "built-in": ("Nothing to install: it ships with keelchat", f"{DOCS}/FEATURES.md"),
}


class Need(BaseModel):
    name: str
    why: str
    options: list[str] = Field(default_factory=list)
    connect: Connect
    source: Source = "yaml"
    how: str = ""
    docs: str = ""

    def explained(self) -> Need:
        how, docs = HOW[self.connect]
        return self.model_copy(update={"how": self.how or how, "docs": self.docs or docs})


def from_policy(policy: CompiledPolicy) -> list[Need]:
    """What the YAML uses that lives outside keelchat. Computed in code."""
    tools = set(policy.allowed_tools)
    out = [
        Need(
            name="A model key",
            why="Every reply is phrased by a model; keelchat's code decides what happens.",
            options=["Anthropic", "OpenAI", "Google Gemini", "any provider litellm reaches", "Ollama for a local model"],
            connect="config",
            how="Set KEELCHAT_LLM and the provider's key in .env",
        )
    ]
    if "knowledge_search" in tools:
        out.append(
            Need(
                name="Your documents",
                why="It answers questions from them, and says so when they don't cover a question.",
                options=["Upload PDFs, Word or Markdown in the console", "an S3 bucket of files"],
                connect="built-in",
                how="Run the knowledge service (scripts/knowledge.sh) and set KNOWLEDGE_URL",
            )
        )
    if "analytics_query" in tools:
        out.append(
            Need(
                name="Your data",
                why="It answers questions with SQL over your tables.",
                options=["CSV or Parquet files", "Postgres", "DuckDB"],
                connect="config",
                how="Run the analytics service (scripts/analytics.sh) and set ANALYTICS_URL",
            )
        )
    if policy.handoff is not None:
        out.append(
            Need(
                name="Somewhere for handed-over chats",
                why="When it hands over, a person needs to see the conversation and reply.",
                options=["the built-in handoff inbox", "Zendesk", "Freshdesk", "Intercom", "any helpdesk with a webhook"],
                connect="webhook",
            )
        )
    if policy.memory:
        out.append(
            Need(
                name="A database for what it remembers",
                why=f"It keeps {', '.join(m.name for m in policy.memory)} between conversations, with consent.",
                options=["Postgres", "in memory while you build"],
                connect="config",
                how="Set DATABASE_URL (scripts/db.sh starts one with Docker)",
            )
        )
    out.append(
        Need(
            name="Where people talk to it",
            why="The bot is channel-neutral; each channel adds its own limits and rules.",
            options=["a website widget (built in)", "WhatsApp Cloud API", "Twilio SMS or voice", "Slack"],
            connect="built-in",
            how="Publish it from the console's Channels tab",
        )
    )
    return [n.explained() for n in out]


SYSTEM = """You list the outside systems a chatbot needs to do its job for real, beyond what its policy already uses.
You get the owner's description, and the bot's policy (YAML) when there is one. Never list what keelchat already covers:
a model, documents or a knowledge base, passing the chat to a person (it has a handoff inbox and helpdesk webhooks),
storage, or a chat channel (web, WhatsApp, SMS, Slack, phone). List only other systems the description implies, such as
a booking calendar, an order or CRM system, a payments provider or an email sender.

For each: a short name ("A booking calendar"), why the bot needs it in one sentence tied to the description, two to
four well-known real products the owner might already use, and how to connect it: "mcp" when the product is commonly
reached through an MCP server, otherwise "tool" (a small Python function that calls its API). Return an empty list
when nothing else is needed. At most four."""

SCHEMA = {
    "type": "object",
    "properties": {
        "needs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "connect": {"type": "string", "enum": ["tool", "mcp"]},
                },
                "required": ["name", "why", "options", "connect"],
            },
        }
    },
    "required": ["needs"],
}
LIMIT = 4
#: what keelchat covers already; a suggestion named for one of these repeats the list read from the YAML
COVERED = ("document", "knowledge", "faq", "person", "human", "handoff", "helpdesk", "database", "storage", "model", "channel", "chat widget")


async def suggested(llm: LLMClient, models: ModelConfig, description: str, yaml: str) -> list[Need]:
    """Outside systems the description implies, as the model suggests them; checked and capped in code."""
    content = untrusted("description", description) + "\n\n" + untrusted("policy", yaml)
    req = LLMRequest("demo_needs", models.generate, SYSTEM, [{"role": "user", "content": content}], max_tokens=1200, timeout_s=60)
    raw = await llm.structured(req, SCHEMA)
    out: list[Need] = []
    for item in raw.get("needs") or []:
        if not isinstance(item, dict) or item.get("connect") not in ("tool", "mcp"):
            continue
        name, why = str(item.get("name", "")).strip(), str(item.get("why", "")).strip()
        options = [str(o).strip()[:60] for o in item.get("options") or [] if str(o).strip()][:4]
        if name and why and not any(w in name.lower() for w in COVERED):
            out.append(Need(name=name[:80], why=why[:240], options=options, connect=item["connect"], source="suggested").explained())
    return out[:LIMIT]
