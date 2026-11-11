import { useCallback, useEffect, useState } from "react";
import { AggregatesRec, AgentSummary, api, CampaignRec, ChannelInfo, EmbedRec, FindingsRec, InviteRec, KlaviyoRec, RecommendationRec, RecommendationsRec } from "./api";

const pct = (x: number) => `${Math.round(x * 100)}%`;
const STATUS_ORDER = ["pending", "sending", "sent", "opened", "started", "completed", "dropped", "failed", "unsubscribed"];

export function Copy({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="compact" type="button" onClick={() => {
      navigator.clipboard.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }, () => undefined);
    }}>{done ? "Copied" : label}</button>
  );
}

// Where a published agent can go, with what to paste or set for each.
export function Channels({ e, onChange, initial = "website", fixed = false }: { e: EmbedRec; onChange: () => void; initial?: string; fixed?: boolean }) {
  const [tab, setTab] = useState(initial);
  useEffect(() => setTab(initial), [initial]);
  const [wa, setWa] = useState({ phone_number_id: "", access_token: "", app_secret: "" });
  const [waResult, setWaResult] = useState<{ webhook_url: string; verify_token: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ch: ChannelInfo | undefined = e.channels.find((c) => c.channel === tab);
  async function connect(ev: React.FormEvent) {
    ev.preventDefault(); setErr(null);
    try { const r = await api.connectWhatsApp(e.id, wa); setWaResult(r); setWa({ phone_number_id: "", access_token: "", app_secret: "" }); onChange(); }
    catch (x) { setErr((x as Error).message); }
  }
  return (
    <div className="channels">
      {!fixed && <div className="channel-tabs" role="tablist">{e.channels.map((c) => (
        <button key={c.channel} type="button" role="tab" aria-selected={tab === c.channel} className={tab === c.channel ? "on" : ""} onClick={() => setTab(c.channel)}>
          {c.label}{c.channel === "whatsapp" && e.whatsapp_connected ? " ✓" : ""}</button>
      ))}</div>}
      {ch && (
        <div className="channel-body">
          <code className="snippet">{ch.value}</code>
          <div className="reach-actions"><Copy text={ch.value} />{ch.channel === "app" && <a className="compact-link" href={ch.value} target="_blank" rel="noreferrer">Open</a>}</div>
          <ol className="steps-list">{ch.steps.map((s) => <li key={s}>{s}</li>)}</ol>
          {ch.channel === "whatsapp" && (
            <form className="reach-form" onSubmit={connect}>
              <label>Phone number id<input id={`wa-id-${e.id}`} value={wa.phone_number_id} onChange={(x) => setWa({ ...wa, phone_number_id: x.target.value })} inputMode="numeric" /></label>
              <label>Access token<input id={`wa-token-${e.id}`} type="password" value={wa.access_token} onChange={(x) => setWa({ ...wa, access_token: x.target.value })} autoComplete="off" /></label>
              <label>App secret<input id={`wa-secret-${e.id}`} type="password" value={wa.app_secret} onChange={(x) => setWa({ ...wa, app_secret: x.target.value })} autoComplete="off" /></label>
              <button className="primary" type="submit" disabled={!wa.phone_number_id || !wa.access_token || !wa.app_secret}>{e.whatsapp_connected ? "Reconnect" : "Connect"}</button>
            </form>
          )}
          {ch.channel === "whatsapp" && waResult && (
            <p className="small">Connected. In Meta's dashboard set the callback URL to <code>{waResult.webhook_url}</code> and the verify token to <code>{waResult.verify_token}</code> <Copy text={waResult.verify_token} label="Copy token" /></p>
          )}
          {err && <div className="callout err">{err}</div>}
        </div>
      )}
    </div>
  );
}

// A chatbot that answers from your content: files and web pages in, a published agent out.
function CreateFromContent({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [urls, setUrls] = useState("");
  const [origins, setOrigins] = useState("");
  const [files, setFiles] = useState<FileList | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  async function create(ev: React.FormEvent) {
    ev.preventDefault(); setBusy(true); setNote(null);
    const form = new FormData();
    form.append("name", name); form.append("urls", urls); form.append("allowed_origins", origins);
    Array.from(files || []).forEach((f) => form.append("files", f));
    try {
      const r = await api.createAssistant(form);
      setNote(`Created "${name}" from ${r.documents.length} source${r.documents.length === 1 ? "" : "s"}. Pick a channel below to put it live.`);
      setName(""); setUrls(""); setFiles(null); onCreated();
    } catch (x) { setNote((x as Error).message); } finally { setBusy(false); }
  }
  return (
    <section className="panel">
      <h3>Create a chatbot from your content</h3>
      <p className="muted small">Upload files (PDF, text, Markdown, CSV, HTML) and/or add web pages. keelchat reads them, builds an assistant that answers only from them, and publishes it for your website, Shopify store, WhatsApp number and more.</p>
      <form className="create-form" onSubmit={create}>
        <label>Name<input id="as-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Bloom Bakery" maxLength={60} required /></label>
        <label>Files<input id="as-files" type="file" multiple accept=".pdf,.txt,.md,.markdown,.csv,.html,.htm" onChange={(e) => setFiles(e.target.files)} /></label>
        <label>Web pages, one per line<textarea id="as-urls" rows={3} value={urls} onChange={(e) => setUrls(e.target.value)} placeholder="https://www.example.com/faq" /></label>
        <label>Websites that may show it<input id="as-origins" value={origins} onChange={(e) => setOrigins(e.target.value)} placeholder="https://your-store.myshopify.com" /></label>
        <button className="primary" type="submit" disabled={busy || name.trim().length < 2 || (!urls.trim() && !files?.length)}>{busy ? "Reading your content…" : "Create chatbot"}</button>
      </form>
      {note && <p className="muted small" role="status">{note}</p>}
    </section>
  );
}

// Put an agent on a website (widget), as an installable app, and on phone numbers.
function Publish({ agents, refresh }: { agents: AgentSummary[]; refresh: number }) {
  const [list, setList] = useState<EmbedRec[]>([]);
  const [agent, setAgent] = useState("");
  const [title, setTitle] = useState("");
  const [origins, setOrigins] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => { api.embeds().then(setList, (e: Error) => setErr(e.message)); }, []);
  useEffect(load, [load, refresh]);
  useEffect(() => { if (!agent && agents[0]) setAgent(agents[0].agent); }, [agents, agent]);

  async function publish(e: React.FormEvent) {
    e.preventDefault(); setErr(null);
    try {
      const allowed = origins.split(/[\s,]+/).map((o) => o.trim()).filter(Boolean);
      await api.publish(agent, { name: title || agent, title, allowed_origins: allowed });
      setTitle(""); setOrigins(""); load();
    } catch (x) { setErr((x as Error).message); }
  }

  return (
    <section className="panel">
      <h3>Website and app</h3>
      <p className="muted small">Publish an agent: you get one line to paste into any website (WordPress: a Custom HTML block) and a link people can open or install as an app. Only the sites you list can show the widget.</p>
      <form className="reach-form" onSubmit={publish}>
        <label>Agent<select id="pub-agent" value={agent} onChange={(e) => setAgent(e.target.value)}>{agents.map((a) => <option key={a.agent}>{a.agent}</option>)}</select></label>
        <label>Title people see<input id="pub-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Acme Support" maxLength={60} /></label>
        <label>Websites allowed<input id="pub-origins" value={origins} onChange={(e) => setOrigins(e.target.value)} placeholder="https://www.example.com" /></label>
        <button className="primary" type="submit" disabled={!agent}>Publish</button>
      </form>
      {err && <div className="callout err">{err}</div>}
      {list.length === 0 ? <p className="muted small">Nothing published yet.</p> : (
        <ul className="reach-list">{list.map((e) => (
          <li key={e.id} className={e.enabled ? "" : "off"}>
            <div className="row-between"><b>{e.title || e.agent}</b><span className="muted small">{e.agent} · {e.allowed_origins.join(", ") || "app link only"}</span></div>
            <Channels e={e} onChange={load} />
            <div className="reach-actions">
              <button className="compact" type="button" onClick={() => api.updateEmbed(e.id, { enabled: !e.enabled }).then(load)}>{e.enabled ? "Turn off" : "Turn on"}</button>
            </div>
          </li>
        ))}</ul>
      )}
    </section>
  );
}

// What a survey found: counted in code, themes backed by quotes.
function Findings({ agents }: { agents: AgentSummary[] }) {
  const [agent, setAgent] = useState("");
  const [days, setDays] = useState(30);
  const [deckBusy, setDeckBusy] = useState(false);
  const [data, setData] = useState<FindingsRec | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (!agent && agents[0]) setAgent((agents.find((a) => /survey|nps|feedback|screen/i.test(a.agent)) || agents[0]).agent); }, [agents, agent]);
  useEffect(() => {
    if (!agent) return;
    setData(null); setErr(null);
    api.findings(agent, days).then(setData, (e: Error) => setErr(e.message));
  }, [agent, days]);

  return (
    <section className="panel">
      <div className="row-between">
        <h3>Findings</h3>
        <div className="field-row">
          <select className="compact" aria-label="Agent" value={agent} onChange={(e) => setAgent(e.target.value)}>{agents.map((a) => <option key={a.agent}>{a.agent}</option>)}</select>
          <select className="compact" aria-label="Time range" value={days} onChange={(e) => setDays(Number(e.target.value))}>{[7, 30, 90, 365].map((d) => <option key={d} value={d}>Last {d} days</option>)}</select>
          <button className="compact" type="button" disabled={!data} onClick={() => api.downloadResponses(agent).catch((e: Error) => setErr(e.message))}>Download CSV</button>
          <button className="compact" type="button" disabled={!data || deckBusy} title="A slide deck of these findings (paid workspaces)"
            onClick={() => { setDeckBusy(true); api.downloadDeck(agent, days).catch((e: Error) => setErr(e.message)).finally(() => setDeckBusy(false)); }}>
            {deckBusy ? "Building deck…" : "Slide deck"}</button>
        </div>
      </div>
      {err && <p className="muted small">{err.includes("no survey step") ? "This agent has no survey step." : err}</p>}
      {!data && !err && <p className="muted small">Loading…</p>}
      {data && (
        <>
          <p className="muted small">{data.respondents} {data.respondents === 1 ? "person" : "people"} answered, {data.completed} finished. Numbers are counted over the people who reached each question; quotes are people's own words.</p>
          <FixFirst agent={agent} days={days} />
          <Aggregates agent={agent} days={days} />
          <div className="findings-grid">{data.questions.map((q) => (
            <article key={q.key} className="finding">
              <div className="row-between"><b>{q.prompt}</b><span className="muted small">{q.n_asked} answered</span></div>
              {q.nps != null && <p className="big-num">NPS {q.nps > 0 ? "+" : ""}{q.nps}<span className="muted small"> · average {q.mean}</span></p>}
              {q.nps == null && q.mean != null && <p className="big-num">{q.mean}<span className="muted small"> average · median {q.median}</span></p>}
              {q.kind !== "scale" && q.counts.length > 0 && (
                <ul className="bars">{q.counts.map((c) => (
                  <li key={c.key}><span>{c.label}</span><span className="bar"><i style={{ width: pct(c.share) }} /></span><span className="num">{c.n} · {pct(c.share)}</span></li>
                ))}</ul>
              )}
              {q.themes.map((t) => (
                <details key={t.theme} className="theme"><summary><b>{t.theme}</b> <span className="muted small">{t.n} {t.n === 1 ? "person" : "people"}</span></summary>
                  <p className="small">{t.summary}</p>
                  {t.quotes.map((x) => <blockquote key={x.response_id}>“{x.text}”</blockquote>)}
                </details>
              ))}
              {q.themes_error && <p className="muted small">{q.themes_error}</p>}
            </article>
          ))}</div>
        </>
      )}
    </section>
  );
}

const SEGMENT_NAMES: Record<string, string> = { campaign: "Campaign", channel: "Channel", language: "Language", product: "Product", buyer: "First-time or repeat buyer" };
const fmt = (v: number | null, nps: boolean) => (v == null ? "–" : nps ? `${v > 0 ? "+" : ""}${v}` : String(v));

/** The score per period as a line, on a fixed scale (NPS -100..100, or the question's 0..10), with gaps for empty
 * periods, a zero line for NPS, the latest value labelled, and each point's value and count on hover. */
function TrendChart({ a, nps }: { a: AggregatesRec; nps: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 200, L = 40, R = 56, T = 14, B = 28;
  const lo = nps ? -100 : 0, hi = nps ? 100 : 10;
  const val = (p: AggregatesRec["trend"][number]) => (nps ? p.nps : p.mean);
  const x = (i: number) => L + (a.trend.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (a.trend.length - 1));
  const y = (v: number) => T + ((hi - v) * (H - T - B)) / (hi - lo);
  const runs: string[] = [];
  let cur = "";
  a.trend.forEach((p, i) => { const v = val(p); if (v == null) { if (cur) runs.push(cur); cur = ""; } else cur += `${cur ? "L" : "M"}${x(i)},${y(v)}`; });
  if (cur) runs.push(cur);
  const lastI = a.trend.map(val).map((v, i) => (v == null ? -1 : i)).filter((i) => i >= 0).pop();
  const ticks = nps ? [-100, -50, 0, 50, 100] : [0, 5, 10];
  const label = (s: string) => new Date(s + "T00:00:00").toLocaleDateString(undefined, a.by === "week" ? { month: "short", day: "numeric" } : { month: "short", year: "2-digit" });
  const every = Math.max(1, Math.ceil(a.trend.length / 8));
  return (
    <figure className="trend">
      <div className="trend-scroll">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" onMouseLeave={() => setHover(null)}
          aria-label={`${nps ? "NPS" : "Average score"} per ${a.by}: ${a.trend.map((p) => `${label(p.start)} ${fmt(val(p), nps)}`).join(", ")}`}>
          {ticks.map((t) => (
            <g key={t}><line x1={L} x2={W - R} y1={y(t)} y2={y(t)} className={t === 0 && nps ? "zero" : "grid"} />
              <text x={L - 8} y={y(t) + 4} textAnchor="end" className="tick">{t > 0 && nps ? `+${t}` : t}</text></g>
          ))}
          {runs.map((d) => <path key={d} d={d} className="line" />)}
          {a.trend.map((p, i) => {
            const v = val(p);
            return (
              <g key={p.start}>
                {i % every === 0 && <text x={x(i)} y={H - 8} textAnchor="middle" className="tick">{label(p.start)}</text>}
                <rect x={x(i) - (W - L - R) / Math.max(2, a.trend.length) / 2} y={T} width={(W - L - R) / Math.max(2, a.trend.length)} height={H - T - B}
                  fill="transparent" onMouseEnter={() => setHover(i)} />
                {v != null && <circle cx={x(i)} cy={y(v)} r={hover === i ? 5 : 4} className="dot" pointerEvents="none" />}
              </g>
            );
          })}
          {lastI != null && <text x={x(lastI)} y={y(val(a.trend[lastI]) as number) - 10} textAnchor={x(lastI) > W - R - 20 ? "end" : "middle"} className="end">{fmt(val(a.trend[lastI]), nps)}</text>}
          {hover != null && (
            <g pointerEvents="none">
              <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} className="cross" />
              <text x={Math.min(Math.max(x(hover), L + 60), W - R - 60)} y={T + 12} textAnchor="middle" className="tip">
                {label(a.trend[hover].start)} · {fmt(val(a.trend[hover]), nps)} · {a.trend[hover].respondents} {a.trend[hover].respondents === 1 ? "person" : "people"}
              </text>
            </g>
          )}
        </svg>
      </div>
      <figcaption className="muted small">{nps ? "NPS" : "Average score"} per {a.by}; a gap means nobody answered that {a.by}.</figcaption>
    </figure>
  );
}

/** One row per segment value: its score as a bar from the middle (NPS) or from zero, the count, and a flag for small
 * groups. */
function SegmentBars({ rows, nps }: { rows: AggregatesRec["segments"][string]; nps: boolean }) {
  return (
    <ul className="seg-bars">{rows.map((r) => {
      const v = (nps ? r.nps : r.mean) ?? 0;
      const share = nps ? Math.abs(v) / 100 / 2 : v / 10;
      const left = nps ? (v < 0 ? 50 - share * 100 : 50) : 0;
      return (
        <li key={r.value} className={r.small ? "small-group" : ""}>
          <span className="seg-name">{r.value}</span>
          <span className={`seg-track${nps ? " mid" : ""}`}><i style={{ left: `${left}%`, width: `${share * 100}%` }} /></span>
          <span className="num">{fmt(nps ? r.nps : r.mean, nps)} · {r.respondents}{r.small ? " · few" : ""}</span>
        </li>
      );
    })}</ul>
  );
}

/** What to fix first (themes unhappy customers raise, most first) and what to keep doing, each with the group's score
 * and their own words. Ranked on the server; only the action line is written by the model. */
function FixFirst({ agent, days }: { agent: string; days: number }) {
  const [r, setR] = useState<RecommendationsRec | null>(null);
  useEffect(() => {
    let live = true;
    setR(null);
    if (agent) api.recommendations(agent, days).then((x) => { if (live) setR(x); }, () => undefined);
    return () => { live = false; };
  }, [agent, days]);
  if (!r || (!r.fix.length && !r.keep.length)) return null;
  const row = (x: RecommendationRec, kind: "fix" | "keep", n: number) => (
    <li key={x.theme} className={`rec ${kind}`}>
      <div className="rec-head"><span className="rec-n mono">{kind === "fix" ? n + 1 : "+"}</span><b>{x.action}</b></div>
      <p className="muted small">{x.theme} · {x.people} {x.people === 1 ? "person" : "people"} ({pct(x.share)})
        {x.nps != null ? ` · their NPS ${x.nps > 0 ? "+" : ""}${x.nps}` : x.mean != null ? ` · their average ${x.mean}` : ""}
        {kind === "fix" ? ` · ${x.detractors} unhappy` : ""}</p>
      {x.quotes.map((q) => <blockquote key={q.response_id}>“{q.text}”</blockquote>)}
    </li>
  );
  return (
    <article className="finding fix-first">
      <div className="row-between"><b>What to fix first</b>{r.overall_nps != null && <span className="muted small">NPS overall {r.overall_nps > 0 ? "+" : ""}{r.overall_nps}</span>}</div>
      {r.fix.length ? <ol className="recs">{r.fix.map((x, n) => row(x, "fix", n))}</ol> : <p className="muted small">No theme is mostly raised by unhappy customers.</p>}
      {r.keep.length > 0 && <><h4>What they love: keep doing</h4><ul className="recs">{r.keep.map((x, n) => row(x, "keep", n))}</ul></>}
      <p className="muted small">Ranked by how many unhappy customers (scores 0–6) raised each theme. {r.error}</p>
    </article>
  );
}

function Aggregates({ agent, days }: { agent: string; days: number }) {
  const [by, setBy] = useState<"week" | "month">(days > 90 ? "month" : "week");
  const [a, setA] = useState<AggregatesRec | null>(null);
  useEffect(() => { setBy(days > 90 ? "month" : "week"); }, [days]);
  useEffect(() => {  // the latest request wins: an older reply (another agent or range) never overwrites it
    let live = true;
    setA(null);
    if (agent) api.aggregates(agent, days, by).then((r) => { if (live) setA(r); }, () => { if (live) setA(null); });
    return () => { live = false; };
  }, [agent, days, by]);
  if (!a || !a.question) return null;
  const nps = a.trend.some((p) => p.nps != null) || Object.values(a.segments).some((rows) => rows.some((r) => r.nps != null));
  return (
    <article className="finding aggregates">
      <div className="row-between"><b>{a.prompt}: over time and by group</b>
        <span className="field-row"><span className="muted small">{a.respondents} answered · {a.completed} finished</span>
          <select className="compact" aria-label="Group by" value={by} onChange={(e) => setBy(e.target.value as "week" | "month")}><option value="week">By week</option><option value="month">By month</option></select></span>
      </div>
      {a.trend.length > 0 && <TrendChart a={a} nps={nps} />}
      {a.truncated && <p className="muted small">Only the newest 50,000 answers are counted here.</p>}
      <div className="seg-grid">{Object.entries(a.segments).filter(([, rows]) => rows.length > 1 || Object.keys(a.segments).length <= 3).map(([key, rows]) => (
        <div key={key}><h4>{SEGMENT_NAMES[key] ?? key}</h4><SegmentBars rows={rows} nps={nps} /></div>
      ))}</div>
      <p className="muted small">"few": fewer than 5 people, too few to read much into. Counted from every answer, finished or not.</p>
    </article>
  );
}

export default function Reach({ workspace }: { workspace: string | null }) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => { api.agents().then(setAgents, () => undefined); }, [refresh]);
  return (
    <main className="dash">
      <div className="dash-head"><div><h1>Publish and results</h1>
        <p className="muted small">Workspace <code>{workspace ?? "default"}</code>: publish agents to a site or link, then see what people said. Agents must be saved as a version first.</p></div></div>
      <CreateFromContent onCreated={() => setRefresh((n) => n + 1)} />
      <Publish agents={agents} refresh={refresh} />
      {agents.length > 0 && <Findings agents={agents} />}
    </main>
  );
}
