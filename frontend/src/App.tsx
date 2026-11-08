import { Component, lazy, ReactNode, Suspense, useCallback, useEffect, useRef, useState } from "react";
import Analysis, { AnalysisResult, latestAnalysis, latestVerification, plural } from "./Analysis";
import { AgentSummary, api, ApiError, BotType, Built, Dataset, TurnResponse, ValidationError } from "./api";
import { ANALYST_POLICY, EXAMPLE_POLICY } from "./example";
import { appendStep, flowSteps, stepSnippet, type Root } from "./policySchema";
import Audit from "./Audit";
import Dashboard from "./Dashboard";
import Usage from "./Usage";
import Handoffs from "./Handoffs";
import Setup from "./Setup";
import Simulate from "./Simulate";
import Rain from "./Rain";
import Reach from "./Reach";
import Install from "./Install";
import Versions from "./Versions";

const PolicyEditor = lazy(() => import("./PolicyEditor"));  // CodeMirror is most of the bundle: load it on its own

type Msg = { role: "user" | "agent"; text: string; meta?: string[]; analysis?: AnalysisResult | null; error?: boolean; handoff?: string };
type Tab = "setup" | "analysis" | "trace" | "state" | "rendered" | "metrics" | "versions" | "simulate" | "handoffs" | "audit";
const TABS: Tab[] = ["setup", "analysis", "trace", "state", "rendered", "metrics", "versions", "simulate", "handoffs", "audit"];

const TEMPLATES = [
  { id: "hiring", label: "Hiring assistant", yaml: EXAMPLE_POLICY },
  { id: "analyst", label: "Sales analyst", yaml: ANALYST_POLICY },
];

const SUGGESTIONS = {
  hiring: ["hello", "yes", "backend engineer in austin", "1", "Ana Ruiz, 12 Elm St Austin", "do you offer dental benefits?", "which pays more and is closer to 78704?", "Actually, I meant Dallas", "submit"],
  analyst: ["Which region brought in the most revenue?", "now only the Pro product", "Is EU monthly revenue declining significantly?", "Which regions are behind their Q2 target?"],
};

// One panel that cannot render a response shows an error there, instead of blanking the whole console.
class Panel extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: Error) { return { error: e.message }; }
  render() {
    return this.state.error ? <div className="callout err">This panel could not show the response ({this.state.error}). The raw data is in the Rendered tab.</div> : this.props.children;
  }
}

const GONE = "This session is no longer on the server (sessions kept in memory are lost when the API restarts). Start a new session.";
const describe = (e: unknown) => (e instanceof ApiError && e.status === 404 ? GONE : (e as Error).message);

export default function App() {
  const [policy, setPolicy] = useState(EXAMPLE_POLICY);
  const [botTypes, setBotTypes] = useState<BotType[]>([]);
  const shown = botTypes.find((t) => t.yaml === policy);
  const template = shown ? `type:${shown.id}` : botTypes.length ? "" : TEMPLATES.find((t) => t.yaml === policy)?.id ?? "";
  const [built, setBuilt] = useState<(Built & { yaml: string }) | null>(null);
  const [issues, setIssues] = useState<ValidationError[]>([]);
  // the live check of the editor text: undefined while checking
  const [live, setLive] = useState<{ issues: ValidationError[] | undefined; agent: string | null }>({ issues: undefined, agent: null });
  const [schema, setSchema] = useState<Root | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [versionsKey, setVersionsKey] = useState(0);
  const [savedTick, setSavedTick] = useState(0);  // bumped after a save or an activation: the checklist re-checks  // bumped after a save or an activation
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [describing, setDescribing] = useState(false);
  const [description, setDescription] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [newOpen, setNewOpen] = useState(false);
  const [newType, setNewType] = useState("blank");
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [userId, setUserId] = useState("tester");
  const [channel, setChannel] = useState("web");
  const [session, setSession] = useState<{ id: string; policy_hash: string } | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [building, setBuilding] = useState(false);
  const [last, setLast] = useState<TurnResponse | null>(null);
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const [tab, setTab] = useState<Tab>("setup");
  const [metrics, setMetrics] = useState<Record<string, unknown> | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);
  const [openHandoffs, setOpenHandoffs] = useState(0);
  const [focusHandoff, setFocusHandoff] = useState<string | null>(null);
  const [view, setView] = useState<"console" | "dashboard" | "settings" | "reach" | "install">(() =>
    location.hash === "#dashboard" ? "dashboard" : location.hash === "#usage" ? "settings" : location.hash === "#reach" ? "reach" : location.hash.startsWith("#install") ? "install" : "console");
  useEffect(() => {
    const hash = { dashboard: "#dashboard", settings: "#usage", reach: "#reach", install: "#install", console: "" }[view];
    try { history.replaceState(null, "", hash || location.pathname); } catch { /* hash is a convenience */ }
  }, [view]);
  const countHandoffs = useCallback(() => { api.handoffs("open").then((h) => setOpenHandoffs(h.length), () => undefined); }, []);
  const chatRef = useRef<HTMLDivElement>(null);

  useEffect(() => { chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior: "smooth" }); }, [msgs, busy]);
  useEffect(() => {
    const check = () => api.health().then(() => { setOnline(true); countHandoffs(); }, () => setOnline(false));
    check();
    const t = window.setInterval(check, 15000);
    return () => window.clearInterval(t);
  }, [countHandoffs]);

  const refreshAgents = useCallback(() => { api.agents().then(setAgents, () => undefined); }, []);
  const [workspace, setWorkspace] = useState<string | null>(null);
  useEffect(() => {  // the key's tenant first, then what the editor offers: the schema, its datasets and saved agents
    api.whoami().then((w) => setWorkspace(w.tenant), () => undefined).finally(() => {
      api.schema().then((s) => setSchema(s as Root), () => undefined);
      api.datasets().then(setDatasets, () => undefined);
      refreshAgents();
      refreshTypes();
    });
  }, [refreshAgents]);  // eslint-disable-line react-hooks/exhaustive-deps

  function refreshTypes() { api.templates().then(setBotTypes, () => undefined); }

  async function opened(v: { agent: string; version: number }, text: string) {
    // a new saved agent: show its policy, make it the one to try, refresh the pickers
    const yaml = (await api.version(v.agent, v.version)).yaml ?? "";
    load(yaml);
    setBuilt({ ...(await api.uploadAgent(yaml)), yaml });
    setSession(null); setMsgs([]); setLast(null); setState(null);
    setVersionsKey((k) => k + 1); refreshAgents();
    setNotice({ kind: "ok", text });
  }

  async function createAgent() {
    setNotice(null); setIssues([]); setCreating(true);
    try {
      const v = await api.fromTemplate(newType, newName.trim());
      setNewOpen(false); setNewName("");
      await opened(v, `Created ${v.agent} v1 from the ${botTypes.find((t) => t.id === newType)?.name ?? newType} type. Edit it and save new versions.`);
    } catch (e) {
      if (e instanceof ApiError && e.issues.length) setIssues(e.issues);
      else setNotice({ kind: "err", text: (e as Error).message });
    } finally { setCreating(false); }
  }
  const onIssues = useCallback((i: ValidationError[] | undefined, agent: string | null) => setLive((cur) => ({ issues: i, agent: agent ?? cur.agent })), []);

  function load(text: string) {
    setPolicy(text); setIssues([]);
  }

  async function openAgent(name: string) {
    const a = agents.find((x) => x.agent === name);
    if (!a) return;
    try {
      load((await api.version(name, a.active_version ?? a.latest_version)).yaml ?? "");
    } catch (e) { setNotice({ kind: "err", text: (e as Error).message }); }
  }

  async function saveVersion() {
    setIssues([]); setNotice(null); setSaving(true);
    try {
      const v = await api.saveVersion(policy, note.trim());
      setNote(""); setVersionsKey((k) => k + 1); refreshAgents(); setSavedTick((t) => t + 1);
      setBuilt({ ...(await api.uploadAgent(policy)), yaml: policy });  // the saved build is the one to try
      setSession(null); setMsgs([]); setLast(null); setState(null);
      setNotice({ kind: "ok", text: v.created ? `Saved ${v.agent} v${v.version}; new sessions use it.` : `No changes since v${v.version}; nothing new saved.` });
    } catch (e) {
      if (e instanceof ApiError && e.issues.length) setIssues(e.issues);
      else setNotice({ kind: "err", text: (e as Error).message });
    } finally { setSaving(false); }
  }

  async function draft() {
    setNotice(null); setDrafting(true);
    try {
      const d = await api.draft(description.trim());
      load(d.yaml); setDescribing(false);
      setNotice(d.errors.length
        ? { kind: "err", text: `Drafted, but ${plural(d.errors.length, "problem")} ${d.errors.length === 1 ? "remains" : "remain"} after ${plural(d.attempts, "attempt")}; they are marked in the editor.` }
        : { kind: "ok", text: `Drafted a policy that builds (${plural(d.attempts, "attempt")}). Review it, then build or save.` });
    } catch (e) { setNotice({ kind: "err", text: (e as Error).message }); } finally { setDrafting(false); }
  }

  function addStep(name: string) {
    if (schema && name) load(appendStep(policy, stepSnippet(schema, name, datasets)));
  }

  const isAnalyst = (built?.datasets.length ?? 0) > 0;
  // sample replies for the two bundled demos; other agents get none (they would not fit)
  const suggestions = !built ? [] : isAnalyst ? SUGGESTIONS.analyst : built.agent === "hiring-agent" ? SUGGESTIONS.hiring : [];

  async function build() {
    setIssues([]); setNotice(null); setBuilding(true);
    try {
      const r = await api.uploadAgent(policy);
      setBuilt({ ...r, yaml: policy }); setSession(null); setMsgs([]); setLast(null); setState(null);
    } catch (e) {
      setBuilt(null);
      if (e instanceof ApiError && e.issues.length) setIssues(e.issues);
      else setNotice({ kind: "err", text: (e as Error).message });
    } finally { setBuilding(false); }
  }

  async function start() {
    if (!built) return;
    setNotice(null);
    try {
      const s = await api.startSession(built.agent, userId, channel, built.hash);
      setSession(s); setMsgs([]); setLast(null); setState(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {  // the API restarted and lost its in-memory policies
        setBuilt(null);
        setNotice({ kind: "err", text: "The API no longer has this policy (it restarted). Press Build & try again." });
      } else setNotice({ kind: "err", text: (e as Error).message });
    }
  }

  async function send(text: string, action?: string) {
    if (!session || busy || (!text.trim() && !action)) return;
    setBusy(true);
    setMsgs((m) => [...m, { role: "user", text: action ? `[${action}]` : text }]);
    setInput("");
    try {
      const r = await api.turn(session.id, text, crypto.randomUUID(), action);
      const meta = [r.route ?? "-", `${r.llm_calls} model`, `${r.tool_calls} tool`, r.outbound.exit_reason ? `exit: ${r.outbound.exit_reason}` : "", r.rules?.length ? `rules: ${r.rules.join(", ")}` : "", r.errors.length ? `${r.errors.length} errors` : ""].filter(Boolean);
      const analysis = latestAnalysis(r.outbound.items);
      const handoff = r.outbound.items.find((x) => x.type === "handoff")?.ticket_id as string | undefined;
      setMsgs((m) => [...m, { role: "agent", text: r.outbound.text, meta, analysis, handoff }]);
      if (handoff) countHandoffs();
      setLast(r);
      if (analysis || latestVerification(r.outbound.items)) setTab("analysis");
    } catch (e) {
      setMsgs((m) => [...m, { role: "agent", text: describe(e), error: true }]);
      setBusy(false);
      return;
    }
    try {  // the reply is in; a failed inspector refresh is not a failed turn
      setState(await api.state(session.id));
      if (tab === "metrics") setMetrics(await api.metrics());
    } catch (e) {
      setNotice({ kind: "err", text: describe(e) });
    } finally { setBusy(false); }
  }

  async function forget() {
    const r = await api.deleteMemory(userId);
    setNotice({ kind: "ok", text: `Deleted ${r.deleted} memory record${r.deleted === 1 ? "" : "s"} for ${userId}.` });
  }

  const analysis = latestAnalysis(last?.outbound.items);
  const verification = latestVerification(last?.outbound.items);
  const totalMs = last ? Math.max(1, ...last.trace.filter((t) => !t.skipped).map((t) => t.ms)) : 1;

  return (
    <>
    <Rain />
    <div className="shell">
      <header className="topbar">
        <div className="brand"><span className="mark" aria-hidden>y</span><span>yachat<span className="caret" aria-hidden /></span> <span className="muted">/ test console</span></div>
        <div className="topbar-right">
          {workspace && <span className="pill" title="The tenant your key belongs to">workspace <code>{workspace}</code></span>}
          <button className={`pill link${view === "console" ? " on" : ""}`} onClick={() => setView("console")} title="Write a policy and talk to it">Editor · test chat</button>
          <button className={`pill link${view === "install" ? " on" : ""}`} onClick={() => setView("install")} title="Web, SMS, Slack, WhatsApp, voice">Channels</button>
          <button className={`pill link${view === "reach" ? " on" : ""}`} onClick={() => setView("reach")} title="Publish agents and see survey results">Publish · results</button>
          <button className={`pill link${view === "dashboard" ? " on" : ""}`} onClick={() => setView("dashboard")} title="Outcomes and the audit log">Outcomes</button>
          <button className={`pill link${view === "settings" ? " on" : ""}`} onClick={() => setView("settings")} title="API keys, models, usage and cost">Settings</button>
          <button className={`pill link${openHandoffs > 0 ? " attention" : ""}`} onClick={() => { setFocusHandoff(null); setTab("handoffs"); setView("console"); }}
            title="Conversations handed to a person">Handoffs ({openHandoffs})</button>
          <a className="pill link" href="/docs" target="_blank" rel="noreferrer">API docs</a>
          {built && <span className="pill" title="Sessions pin this exact compiled policy">policy <code>{built.hash}</code></span>}
          <span className={`pill status ${online === false ? "down" : online ? "up" : ""}`}>
            <span className="dot" /> {online === null ? "connecting" : online ? "API online" : "API offline"}
          </span>
        </div>
      </header>

      {view === "dashboard" && <Dashboard workspace={workspace} onOpenHandoffs={() => { setFocusHandoff(null); setTab("handoffs"); setView("console"); }} />}
      {view === "settings" && <Usage workspace={workspace} />}
      {view === "reach" && <Reach workspace={workspace} />}
      {view === "install" && <Install onNew={() => setView("console")} />}
      <main className="grid" hidden={view !== "console"}>
        <section className="card">
          <div className="card-head">
            <h2>Policy</h2>
            <select className="compact" aria-label="Open a template or a saved agent" value={template}
              onChange={(e) => {
                const v = e.target.value;
                const t = TEMPLATES.find((x) => x.id === v) ?? botTypes.find((x) => `type:${x.id}` === v);
                if (t) load(t.yaml); else if (v.startsWith("saved:")) void openAgent(v.slice(6));
              }}>
              <option value="" disabled>{live.agent ? `Editing ${live.agent}` : "Custom policy"}</option>
              <optgroup label="Bot types (not saved)">
                {botTypes.length
                  ? botTypes.map((t) => <option key={t.id} value={`type:${t.id}`}>{t.name}</option>)
                  : TEMPLATES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
              </optgroup>
              {agents.length > 0 && <optgroup label="Saved agents">{agents.map((a) => (
                <option key={a.agent} value={`saved:${a.agent}`}>{a.agent} (v{a.active_version ?? a.latest_version})</option>))}</optgroup>}
            </select>
          </div>
          <div className="editor-tools">
            <select className="compact" aria-label="Add a step" value="" disabled={!schema} onChange={(e) => addStep(e.target.value)}>
              <option value="">+ Add step</option>
              {schema && flowSteps(schema).map((s) => <option key={s.name} value={s.name} title={s.description}>{s.name}</option>)}
            </select>
            <button className="ghost compact" onClick={() => { setNewOpen((o) => !o); setDescribing(false); }} aria-expanded={newOpen}>New agent…</button>
            <button className="ghost compact" onClick={() => { setDescribing((d) => !d); setNewOpen(false); }} aria-expanded={describing}>Describe a bot…</button>
            <span className={`live ${live.issues === undefined ? "" : live.issues.length ? "bad" : "good"}`} aria-live="polite">
              {live.issues === undefined ? "checking…" : live.issues.length ? plural(live.issues.length, "problem") : "builds"}
            </span>
          </div>
          {newOpen && (
            <div className="describe">
              <div className="types" role="radiogroup" aria-label="Bot type">
                {botTypes.map((t) => (
                  <label key={t.id} className={`type${newType === t.id ? " on" : ""}`}>
                    <input type="radio" name="bot-type" value={t.id} checked={newType === t.id} onChange={() => setNewType(t.id)} />
                    <b>{t.name}{!t.builtin && <span className="tag">yours</span>}</b>
                    <span className="muted small">{t.description}</span>
                  </label>
                ))}
              </div>
              <div className="field-row">
                <input id="new-agent-name" className="grow" value={newName} onChange={(e) => setNewName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))}
                  placeholder="agent name, e.g. returns-desk" aria-label="New agent name" maxLength={63} />
                <button className="primary" onClick={createAgent} disabled={creating || !newName.trim()}>{creating ? "Creating…" : "Create"}</button>
              </div>
              <span className="muted small">Saves the new agent as v1 and opens it in the editor.</span>
            </div>
          )}
          {describing && (
            <div className="describe">
              <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} aria-label="Describe the bot"
                placeholder="e.g. A support bot for a shoe store: ask for consent, then collect the order number and what went wrong." />
              <div className="field-row">
                <button className="primary" onClick={draft} disabled={drafting || description.trim().length < 10}>{drafting ? "Drafting…" : "Draft policy"}</button>
                <span className="muted small">Replaces the editor text. The draft is checked by the build; nothing is saved.</span>
              </div>
            </div>
          )}
          <Suspense fallback={<div className="editor"><pre className="code">{policy}</pre></div>}>
            <PolicyEditor value={policy} onChange={setPolicy} schema={schema} datasets={datasets} onIssues={onIssues} />
          </Suspense>
          <div className="field-row">
            <button className="primary" onClick={build} disabled={building} title="Build and try it; not saved">{building ? "Building…" : "Build & try"}</button>
            <input className="grow" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed (optional)" aria-label="Version note" maxLength={500} />
            <button onClick={saveVersion} disabled={saving || !!live.issues?.length} title={live.issues?.length ? "Fix the problems first: only valid builds are saved" : "Save as the next version and make it active"}>
              {saving ? "Saving…" : "Save version"}
            </button>
          </div>

          {issues.length > 0 && (
            <div className="callout err" role="alert">
              <strong>{issues.length} problem{issues.length === 1 ? "" : "s"} in the policy</strong>
              <ul>{issues.map((i, n) => <li key={n}><code>{i.path}</code> {i.message}</li>)}</ul>
            </div>
          )}
          {built && (
            <div className="callout ok">
              <div className="row-between"><strong>{built.agent}</strong><span className="tag">{built.mode}</span></div>
              <dl className="facts">
                <dt>Tools</dt><dd>{built.allowed_tools.join(", ") || "none"}</dd>
                {built.datasets.length > 0 && <><dt>Datasets</dt><dd>{built.datasets.join(", ")}</dd></>}
              </dl>
              {built.yaml !== policy && <p className="muted small">The editor has changed since this build; build again to try the changes.</p>}
            </div>
          )}

          <div className="session-box">
            <div className="field-row">
              <label>User<input value={userId} onChange={(e) => setUserId(e.target.value)} /></label>
              <label>Channel
                <select value={channel} onChange={(e) => setChannel(e.target.value)}>
                  {["web", "slack", "sms", "whatsapp"].map((c) => <option key={c}>{c}</option>)}
                </select>
              </label>
            </div>
            <div className="field-row">
              <button className="primary" onClick={start} disabled={!built}>New session</button>
              <button className="ghost" onClick={forget}>Forget user</button>
            </div>
            {session && <p className="muted small">Session <code>{session.id}</code></p>}
            {notice && <div className={`callout ${notice.kind}`}>{notice.text}</div>}
          </div>
        </section>

        <section className="card chat-card">
          <div className="card-head">
            <h2>Conversation</h2>
            {session && <span className="muted small">{built?.agent} · {channel}</span>}
          </div>
          <div className="chat" ref={chatRef}>
            {msgs.length === 0 && (
              <ol className="steps" aria-label="Getting started">
                <li className={built ? "done" : "now"}><b>Build a policy</b><span>Load a template, describe a bot, or add steps; then build it to try, or save a version.</span></li>
                <li className={session ? "done" : built ? "now" : ""}><b>Start a session</b><span>Sessions pin the compiled policy you just built.</span>
                  {built && !session && <button className="primary" onClick={start}>New session</button>}</li>
                <li className={session ? "now" : ""}><b>Talk to the agent</b><span>Type a message or pick a suggestion below.</span></li>
              </ol>
            )}
            {msgs.map((m, i) => (
              <div key={i} className={`msg ${m.role}${m.error ? " error" : ""}`}>
                <div className="bubble">{m.text}</div>
                {m.analysis && (
                  <button className="analysis-chip" onClick={() => setTab("analysis")}>
                    <span className={`badge ${m.analysis.status}`}>{m.analysis.status}</span>
                    {plural(m.analysis.outcome.findings.length, "finding")} · {plural(m.analysis.data.row_count, "row")} · view analysis →
                  </button>
                )}
                {m.handoff && (
                  <button className="analysis-chip handoff-chip" onClick={() => { setFocusHandoff(m.handoff ?? null); setTab("handoffs"); }}>
                    <span className="badge partial">handed off</span> waiting for a person · <code>{m.handoff}</code> · open in inbox →
                  </button>
                )}
                {m.meta && <div className="meta">{m.meta.map((x) => <span key={x}>{x}</span>)}</div>}
              </div>
            ))}
            {busy && <div className="msg agent"><div className="bubble typing" aria-label="Agent is working"><span /><span /><span /></div></div>}
          </div>
          {suggestions.length > 0 && session && (
            <div className="suggestions">
              {suggestions.map((t) => <button key={t} className="chip" disabled={busy} onClick={() => void send(t)}>{t}</button>)}
              {!isAnalyst && <button className="chip accent" disabled={busy} onClick={() => void send("", "submit_application")} title="A candidate-only action, as a channel button would send it">Submit button</button>}
            </div>
          )}
          <form className="composer" onSubmit={(e) => { e.preventDefault(); void send(input); }}>
            <input value={input} onChange={(e) => setInput(e.target.value)} placeholder={session ? "Message the agent…" : "Start a session first"} disabled={!session || busy} aria-label="Message" />
            <button type="submit" className="primary" disabled={!session || busy || !input.trim()}>Send</button>
          </form>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Inspector</h2>
          </div>
          <div className="tabs" role="tablist">
            {TABS.map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""}
                onClick={async () => { setTab(t); if (t === "metrics") setMetrics(await api.metrics()); }}>
                {t[0].toUpperCase() + t.slice(1)}{t === "handoffs" && openHandoffs > 0 ? ` (${openHandoffs})` : ""}
              </button>
            ))}
          </div>
          <div className="inspector">
            {tab === "setup" && <Panel><Setup policy={policy} refresh={savedTick} onChange={setPolicy} schema={schema} onSave={() => void saveVersion()} onAddStep={addStep} /></Panel>}
            {tab === "analysis" && <Panel key={last?.turn_id}><Analysis result={analysis} verification={verification} /></Panel>}
            {tab === "trace" && (last ? (
              <div className="trace">
                {last.trace.map((t, i) => (
                  <div key={i} className={`trace-row${t.skipped ? " skipped" : ""}`}>
                    <div className="row-between"><span>{t.stage}{t.note ? <em> · {t.note}</em> : null}</span><span>{t.skipped ? "skipped" : `${t.ms.toFixed(0)} ms`}</span></div>
                    {!t.skipped && <div className="bar"><div style={{ width: `${Math.max(2, (t.ms / totalMs) * 100)}%` }} /></div>}
                  </div>
                ))}
                {last.errors.length > 0 && <div className="callout err"><pre>{last.errors.join("\n")}</pre></div>}
              </div>
            ) : <div className="empty">Send a message to see each stage and its time.</div>)}
            {tab === "state" && (state ? <pre className="code">{JSON.stringify(state, null, 2)}</pre> : <div className="empty">No session state yet.</div>)}
            {tab === "rendered" && (last ? <pre className="code">{JSON.stringify({ outbound: last.outbound, rendered: last.rendered }, null, 2)}</pre> : <div className="empty">Nothing rendered yet.</div>)}
            {tab === "metrics" && (metrics ? <pre className="code">{JSON.stringify(metrics, null, 2)}</pre> : <div className="empty">Loading…</div>)}
            {tab === "simulate" && <Panel><Simulate agent={built?.agent ?? null} /></Panel>}
            {tab === "handoffs" && <Panel><Handoffs focus={focusHandoff} onChange={countHandoffs} /></Panel>}
            {tab === "audit" && <Panel><Audit key={session?.id ?? "none"} agent={built?.agent ?? live.agent} session={session?.id ?? null} /></Panel>}
            {tab === "versions" && (
              <Panel><Versions key={`${live.agent}:${versionsKey}`} agent={live.agent} current={policy} onOpen={load}
                onActivated={(v) => { refreshAgents(); setSavedTick((t) => t + 1); setNotice({ kind: "ok", text: `${v.agent} v${v.version} is active; new sessions use it.` }); }}
                onCopied={(v) => opened(v, `Copied to ${v.agent} v1.`)}
                onTemplate={(t) => { refreshTypes(); setNotice({ kind: "ok", text: `Saved "${t.name}" as a bot type; pick it under New agent.` }); }} /></Panel>
            )}
          </div>
        </section>
      </main>
    </div>
    </>
  );
}
