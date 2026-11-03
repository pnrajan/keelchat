export type TurnResponse = {
  turn_id: string;
  outbound: { text: string; language: string; items: Record<string, unknown>[]; end_of_conversation: boolean; exit_reason: string | null };
  rendered: { text: string; payload: Record<string, unknown> }[];
  trace: { stage: string; ms: number; skipped: boolean; note: string }[];
  errors: string[];
  llm_calls: number;
  tool_calls: number;
  duplicate: boolean;
  route: string | null;
};

export type ValidationError = { path: string; message: string };

export class ApiError extends Error {
  constructor(message: string, readonly issues: ValidationError[] = [], readonly status = 0) {
    super(message);
  }
}

const KEY = "yachat.apiKey";

// When the server sets YACHAT_API_KEY, every route needs it as a bearer token. The console asks once on a 401
// and keeps it in this browser only.
// /console/#key: enter a key by hand (the server admin) instead of signing in; read at load, before the app rewrites the hash
const KEY_BY_HAND = typeof window !== "undefined" && window.location.hash === "#key";

async function send(method: string, path: string, body?: unknown): Promise<Response> {
  const key = localStorage.getItem(KEY);
  const form = body instanceof FormData;  // multipart: the browser sets the content type and boundary
  const headers: Record<string, string> = form ? {} : { "Content-Type": "application/json" };
  if (key) headers.Authorization = `Bearer ${key}`;
  return fetch(path, { method, headers, body: form ? body : body ? JSON.stringify(body) : undefined });
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  let r = await send(method, path, body);
  if (r.status === 401) {
    // a server with sign-up: (re)sign in there, which issues a fresh console key; otherwise ask for a key
    const signup = await fetch("/v1/signup/config").then((x) => x.json(), () => ({ enabled: false }));
    if (signup.enabled && !KEY_BY_HAND) {
      localStorage.removeItem(KEY);
      window.location.href = "/signup/#signin";
      return new Promise<T>(() => {});
    }
    const key = window.prompt("This server requires an API key (YACHAT_API_KEY):");
    if (key) {
      localStorage.setItem(KEY, key.trim());
      r = await send(method, path, body);
    }
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = (data as { detail?: unknown }).detail;
    if (Array.isArray(detail)) {
      const issues = detail.map((d: ValidationError & { loc?: unknown[]; msg?: string }) =>
        d.path !== undefined ? { path: d.path, message: d.message } : { path: (d.loc ?? []).join("."), message: d.msg ?? "" });
      throw new ApiError(issues.map((i) => `${i.path}: ${i.message}`).join("\n"), issues, r.status);
    }
    throw new ApiError(String(detail ?? r.statusText), [], r.status);
  }
  return data as T;
}

export type Built = { agent: string; hash: string; mode: string; allowed_tools: string[]; datasets: string[]; models: Record<string, string> };
export type PolicyVersion = { tenant: string; agent: string; version: number; hash: string; note: string; created_at: string; active: boolean; yaml?: string; created?: boolean };
export type AgentSummary = { agent: string; active_version: number | null; latest_version: number; updated_at: string };
export type Dataset = { name: string; kind: "structured" | "unstructured" };
export type ReadinessItem = { id: string; area: string; status: "ok" | "warning" | "missing"; title: string; detail: string; blocking: boolean; fix: string | null };
export type Readiness = { agent: string | null; items: ReadinessItem[]; score: number; ready: boolean };
export type BotType = { id: string; name: string; description: string; yaml: string; builtin: boolean };

export type HandoffStatus = "open" | "claimed" | "resolved";
export type Handoff = {
  id: string; agent: string; session_id: string; user_id: string; channel: string; reason: string; status: HandoffStatus;
  assignee: string | null; note: string; fields: Record<string, string>; transcript: { role: "user" | "agent"; text: string; at: string }[];
  external_id: string | null; created_at: string; updated_at: string;
};
export type AuditEvent = {
  id: string; at: string; agent: string; session_id: string | null; turn_id: string | null; kind: string; decision: string; reason: string;
  step: string | null; field: string | null; ok: boolean | null; ref: string | null; actor: string; text: string;
};
export type AuditSummary = {
  sessions: number; turns: number; outcomes: Record<string, number>; completion_rate: number; handoff_rate: number;
  handoff_reasons: Record<string, number>; handoff_actions: Record<string, number>; rejected_answers: Record<string, number>;
  guardrails: Record<string, number>; tool_failures: Record<string, number>; truncated: boolean;
};

export type ModelOption = { model: string; name: string; input_per_mtok: number; output_per_mtok: number; context: number; cost_usd: number; saving: number | null };
export type CheaperModels = { days: number; prices_at: string | null; jobs: { job: string; label: string; setting: string; model: string; input_tokens: number; output_tokens: number; cost_usd: number | null; options: ModelOption[] }[] };
export type ChannelInfo = { channel: string; label: string; value: string; steps: string[]; connected?: boolean };
export type InsightsRec = {
  agents: string[];
  total: { conversations: number; turns: number; completion_rate: number; handoff_rate: number; intents: Record<string, number> };
  themes: { theme: string; summary: string; n: number; quotes: { text: string }[] }[];
  text: null | { messages: number; sentiment_model: string; sentiment: { positive: number; negative: number }; keywords: { term: string; conversations: number }[];
    topics: { label: string; conversations: number; examples: string[] }[] };
  plan: null | { question: string; agents: string[]; days: number; mood: string; about: string };
};
export type ApiKeyRec = { id: string; tenant: string; name: string; scopes: string[]; created_at: string; expires_at: string | null; last_used_at: string | null; revoked_at: string | null };
export type RefineRound = {
  round: number; score: number; finished: number; answered: Record<string, number>; smooth: number; problems: string[]; yaml: string;
  conversations: { persona: string; finished: boolean; exit_reason: string | null; questions: number; answered: string[]; transcript: { user: string; agent: string }[] }[];
};
export type RefinementRec = { rounds: RefineRound[]; best: number; target: number };
/** A chatbot drafted for review: its policy, the same in plain words, and any build errors. */
export type AssistantDraft = {
  agent: string; dataset: string; documents: string[]; yaml: string; summary: string[];
  errors: { path: string; message: string }[];
};

/** A Shopify store connected to this workspace. */
export type ShopRec = {
  shop: string; name: string; domain: string; embed_id: string; scopes: string; theme_editor_url?: string;
  survey_campaign: string; survey_delay_hours: number; survey_cooldown_days: number;
};

export type EmbedRec = { id: string; agent: string; name: string; title: string; greeting: string; allowed_origins: string[]; daily_limit: number; enabled: boolean; snippet: string; app_url: string; whatsapp_connected: boolean; channels: ChannelInfo[] };
export type CampaignRec = { id: string; agent: string; name: string; channel: "email" | "sms" | "phone" | "link"; message: string; subject: string; reminder_hours: number; expires_days: number; verify_identity: boolean; score_question: string; active: boolean; invites: Record<string, number> };
export type InviteRec = { id: string; name: string; email: string; phone: string; status: string; sent_at: string | null; opened_at: string | null; finished_at: string | null; verified_at: string | null; error: string; link?: string };
export type RecommendationRec = { action: string; theme: string; summary: string; question: string; people: number; share: number; detractors: number; nps: number | null; mean: number | null; quotes: { response_id: string; text: string }[] };
export type RecommendationsRec = { overall_nps: number | null; fix: RecommendationRec[]; keep: RecommendationRec[]; error: string; days: number };
export type WeeklyRec = { on: boolean; to: string; last_sent: string };
export type DelightedResult = { imported: number; other_survey_types: number; problems: string[]; campaign_id: string | null; contacts: number };
export type BillingRec = { enabled: boolean; plan: "free" | "pro" | "business" | null; status: string; plans: Record<string, { limit_turns: number }> };
export type KlaviyoRec = { connected: boolean; connected_at?: string; synced_until?: string; last_sync?: string; last_error?: string };
/** A survey's score over time and split by segment, counted on the server. */
export type AggregatesRec = {
  question: string | null; prompt: string; by: "week" | "month"; respondents: number; completed: number; days: number; truncated: boolean;
  trend: { start: string; respondents: number; nps: number | null; mean: number | null }[];
  segments: Record<string, { value: string; respondents: number; nps: number | null; mean: number | null; small: boolean }[]>;
};
export type FindingsRec = { agent: string; respondents: number; completed: number; days: number; questions: { key: string; prompt: string; kind: string; n_asked: number; counts: { key: string; label: string; n: number; share: number }[]; mean: number | null; median: number | null; nps: number | null; themes: { theme: string; summary: string; n: number; quotes: { response_id: string; text: string }[] }[]; themes_error: string }[] };
export type KeyMetric = { name: string; label: string; unit: "count" | "rate" | "ms" | "usd" | "minutes" | "number"; description: string; sql: string; value: number | null };
export type WorkspaceMetrics = { days: number; truncated: boolean; metrics: KeyMetric[]; tables: { name: string; rows: number; description: string; columns: string[] }[] };
export type MetricsAnswer = { question: string; sql: string; columns: string[]; rows: unknown[][]; truncated: boolean; window_truncated: boolean; attempts: number };

export type Dashboard = {
  since: string; until: string; summary: AuditSummary; p50_ms: number | null; p95_ms: number | null; llm_calls_per_turn: number; error_rate: number;
  claim_p50_min: number | null; resolve_p50_min: number | null; open_handoffs: number;
  days: { day: string; conversations: number; completed: number; handed_off: number; dropped: number; turns: number; p50_ms: number | null; p95_ms: number | null; error_turns: number }[];
  agents: { agent: string; conversations: number; completion_rate: number; handoff_rate: number; drop_rate: number; turns_per_conversation: number; p95_ms: number | null; llm_calls_per_turn: number; top_rejected_field: string | null }[];
};
export type UsageLine = { key: string; calls: number; input_tokens: number; output_tokens: number; cached_tokens: number; cost_usd: number | null; unpriced_calls: number; p50_ms: number | null };
export type UsageReport = {
  total: UsageLine; by_model: UsageLine[]; by_purpose: UsageLine[]; by_agent: UsageLine[]; by_day: UsageLine[]; conversations: number; cost_per_conversation: number | null;
  models: { generate: string; classify: string; verify: string };
  openrouter_prices_at: string | null;
  prices: Record<string, { source: "config" | "openrouter" | "litellm" | "unknown"; input_per_mtok: number | null; output_per_mtok: number | null }>;
};
export type SimConversation = { name: string; messages?: string[]; persona?: string; max_turns?: number; channel?: string };
export type SimResult = {
  exit_reasons: Record<string, number>; reask_rate: number; policy_hash: string; summary: AuditSummary;
  conversations: {
    name: string; turns: number; exit_reason: string | null; completed: boolean; reasks: number; max_digressions: number; nudges: number;
    llm_calls: number; tool_calls: number; persona: string; transcript: { user: string; agent: string }[];
    decisions: { kind: string; decision: string; reason: string; step: string | null; field: string | null; ok: boolean | null; ref: string | null }[];
  }[];
};

const query = (q: Record<string, string | undefined>) => {
  const p = new URLSearchParams(Object.entries(q).filter((e): e is [string, string] => !!e[1]));
  return p.toString() ? `?${p}` : "";
};

// the console works on the tenant its key belongs to (GET /v1/whoami); "default" for the admin key or an open server
let T = "default";
export const tenant = () => T;
const agentPath = (agent: string) => `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}`;

export const api = {
  whoami: async () => { const w = await req<{ tenant: string; admin: boolean }>("GET", "/v1/whoami"); T = w.tenant; return w; },
  health: () => req<{ status: string }>("GET", "/health"),
  uploadAgent: (yaml: string) => req<Built>("POST", "/v1/agents", { yaml }),
  validate: (yaml: string) => req<{ errors: ValidationError[]; agent: string | null }>("POST", "/v1/agents/validate", { yaml }),
  draft: (description: string) => req<{ yaml: string; errors: ValidationError[]; attempts: number }>("POST", "/v1/agents/draft", { description }),
  saveVersion: (yaml: string, note: string) => req<PolicyVersion>("POST", `/v1/tenants/${T}/agents`, { yaml, note }),
  agents: () => req<AgentSummary[]>("GET", `/v1/tenants/${T}/agents`),
  versions: (agent: string) => req<PolicyVersion[]>("GET", `${agentPath(agent)}/versions`),
  version: (agent: string, n: number) => req<PolicyVersion>("GET", `${agentPath(agent)}/versions/${n}`),
  activate: (agent: string, n: number) => req<PolicyVersion>("POST", `${agentPath(agent)}/versions/${n}/activate`),
  datasets: () => req<Dataset[]>("GET", `/v1/tenants/${T}/datasets`),
  readiness: (yaml: string) => req<Readiness>("POST", `/v1/tenants/${T}/agents/readiness`, { yaml }),
  templates: () => req<BotType[]>("GET", `/v1/tenants/${T}/templates`),
  fromTemplate: (template: string, agent: string) => req<PolicyVersion>("POST", `/v1/tenants/${T}/agents/from-template`, { template, agent }),
  copyAgent: (from: string, agent: string, version?: number) => req<PolicyVersion>("POST", `${agentPath(from)}/copy`, version ? { agent, version } : { agent }),
  saveTemplate: (id: string, name: string, description: string, agent: string) =>
    req<BotType>("POST", `/v1/tenants/${T}/templates`, { id, name, description, agent }),
  schema: () => req<Record<string, unknown>>("GET", "/v1/schema"),
  /** `policy_hash`: the build to talk to (the console tests what it just built, not the saved active version). */
  startSession: (agent: string, user_id: string, channel: string, policy_hash?: string) =>
    req<{ id: string; agent: string; policy_hash: string; channel: string; language: string }>("POST", "/v1/sessions", { agent, user_id, channel, ...(policy_hash ? { policy_hash } : {}) }),
  turn: (sessionId: string, text: string, channel_message_id: string, action?: string) =>
    req<TurnResponse>("POST", `/v1/sessions/${sessionId}/turns`, action ? { text, channel_message_id, action } : { text, channel_message_id }),
  state: (sessionId: string) => req<Record<string, unknown>>("GET", `/v1/sessions/${sessionId}/state`),
  deleteMemory: (userId: string) => req<{ deleted: number }>("DELETE", `/v1/users/${userId}/memory`),
  metrics: () => req<Record<string, unknown>>("GET", "/v1/metrics"),
  handoffs: (status?: HandoffStatus) => req<Handoff[]>("GET", `/v1/tenants/${T}/handoffs${query({ status })}`),
  updateHandoff: (id: string, change: { status?: HandoffStatus; assignee?: string; note?: string }) =>
    req<Handoff>("POST", `/v1/tenants/${T}/handoffs/${encodeURIComponent(id)}`, change),
  audit: (q: { agent?: string; session_id?: string; kind?: string }) => req<AuditEvent[]>("GET", `/v1/tenants/${T}/audit${query(q)}`),
  simulate: (agent: string, conversations: SimConversation[]) =>
    req<SimResult>("POST", `${agentPath(agent)}/simulate`, { conversations }),
  embeds: () => req<EmbedRec[]>("GET", `/v1/tenants/${T}/embeds`),
  keys: () => req<ApiKeyRec[]>("GET", `/v1/tenants/${T}/keys`),
  createKey: (name: string, scopes: string[], expires_in_days: number | null) =>
    req<ApiKeyRec & { key: string }>("POST", `/v1/tenants/${T}/keys`, { name, scopes, expires_in_days }),
  revokeKey: (id: string) => req<unknown>("DELETE", `/v1/tenants/${T}/keys/${encodeURIComponent(id)}`),
  /** A chatbot from files and web pages: multipart, so it goes through send() rather than req(). */
  createAssistant: async (form: FormData) => {
    const r = await send("POST", `/v1/tenants/${T}/assistants`, form);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Failed (${r.status})`);
    return data as EmbedRec & { agent: string; documents: string[] };
  },
  draftAssistant: async (form: FormData) => {
    form.set("draft_only", "true");
    const r = await send("POST", `/v1/tenants/${T}/assistants`, form);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Failed (${r.status})`);
    return data as AssistantDraft;
  },
  /** Test a survey with pretend customers and improve it; polls until done (each round takes a few minutes). */
  refineSurvey: async (yaml: string, rounds = 2, onTick?: () => void): Promise<RefinementRec> => {
    const job = await req<{ id: string }>("POST", `/v1/tenants/${T}/agents/refine`, { yaml, rounds });
    for (;;) {
      await new Promise((r) => setTimeout(r, 4000));
      onTick?.();
      const j = await req<{ status: string; error: string; result: RefinementRec | null }>("GET", `/v1/tenants/${T}/agents/refine/${job.id}`);
      if (j.status === "done" && j.result) return j.result;
      if (j.status !== "running") throw new Error(j.error || "The test couldn't finish.");
    }
  },
  publishAssistant: (body: { name: string; yaml: string; greeting?: string; allowed_origins: string[] }) =>
    req<EmbedRec & { agent: string }>("POST", `/v1/tenants/${T}/assistants/publish`, body),
  checkInstall: (id: string, url: string) =>
    req<{ installed: boolean; allowed: boolean; url: string; message: string }>("POST", `/v1/tenants/${T}/embeds/${encodeURIComponent(id)}/check`, { url }),
  connectWhatsApp: (id: string, body: { phone_number_id: string; access_token: string; app_secret: string }) =>
    req<EmbedRec & { webhook_url: string; verify_token: string }>("POST", `/v1/tenants/${T}/embeds/${encodeURIComponent(id)}/whatsapp`, body),
  publish: (agent: string, body: { name: string; title?: string; greeting?: string; allowed_origins: string[] }) =>
    req<EmbedRec>("POST", `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}/embeds`, body),
  updateEmbed: (id: string, body: Partial<Pick<EmbedRec, "enabled" | "allowed_origins" | "title" | "greeting">>) => req<EmbedRec>("POST", `/v1/tenants/${T}/embeds/${encodeURIComponent(id)}`, body),
  recommendations: (agent: string, days: number) =>
    req<RecommendationsRec>("GET", `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}/findings/recommendations${query({ days: String(days) })}`),
  aggregates: (agent: string, days: number, by: "week" | "month") =>
    req<AggregatesRec>("GET", `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}/findings/aggregates${query({ days: String(days), by })}`),
  findings: (agent: string, days: number) => req<FindingsRec>("GET", `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}/findings${query({ days: String(days) })}`),
  /** Download the responses CSV with the console's key (a plain link would not carry it). */
  downloadResponses: async (agent: string) => {
    const r = await send("GET", `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}/responses.csv`);
    if (!r.ok) throw new Error(`Export failed (${r.status})`);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a"); a.href = url; a.download = `${agent}-responses.csv`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  /** Download the findings as a slide deck (paid workspaces); the server's message explains a refusal. */
  downloadDeck: async (agent: string, days: number) => {
    const r = await send("GET", `/v1/tenants/${T}/agents/${encodeURIComponent(agent)}/findings/deck.pptx${query({ days: String(days) })}`);
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))) as { detail?: string };
      throw new Error(detail.detail ?? `Deck failed (${r.status})`);
    }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a"); a.href = url; a.download = `${agent}-findings.pptx`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  cheaperModels: (days: number) => req<CheaperModels>("GET", `/v1/tenants/${T}/usage/alternatives${query({ days: String(days) })}`),
  refreshPrices: () => req<{ models: number; openrouter_prices_at: string }>("POST", `/v1/tenants/${T}/usage/prices/refresh`),
  usage: (days: number) => req<UsageReport>("GET", `/v1/tenants/${T}/usage${query({ days: String(days) })}`),
  workspaceMetrics: (days: number, agent?: string) => req<WorkspaceMetrics>("GET", `/v1/tenants/${T}/metrics${query({ days: String(days), agent })}`),
  askMetrics: (question: string, days: number, agent?: string) =>
    req<MetricsAnswer>("POST", `/v1/tenants/${T}/metrics/ask`, { question, days, ...(agent ? { agent } : {}) }),
  dashboard: (days: number, agent?: string) => req<Dashboard>("GET", `/v1/tenants/${T}/dashboard${query({ days: String(days), agent })}`),
  auditSummary: (agent?: string) => req<AuditSummary>("GET", `/v1/tenants/${T}/audit/summary${query({ agent })}`),
};
