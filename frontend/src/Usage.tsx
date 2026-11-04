import ApiKeys from "./ApiKeys";
import { useEffect, useState } from "react";
import { api, BillingRec, CheaperModels, DelightedResult, UsageLine, UsageReport, WeeklyRec } from "./api";

const usd = (x: number | null) => (x == null ? "–" : x >= 1 ? `$${x.toFixed(2)}` : x >= 0.01 ? `$${x.toFixed(3)}` : `$${x.toFixed(5)}`);
const tok = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
const short = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const PURPOSE: Record<string, string> = {
  intent_detect: "Reading the message", render: "Writing the reply", reply_verify: "Checking the reply", expect: "Checking an answer",
  affirm: "Reading a yes / no", memory_extract: "Remembering details", shorthand: "Expanding shorthand", knowledge_answer: "Answering from documents",
  contextualize: "Preparing a document search", verify_grounded: "Checking a document answer", plan: "Planning", planner_answer: "Planner answer",
  summarize: "Summarizing", reasoning_prepare: "Planning checks", reasoning_verify: "Verifying the result", analytics_sql: "Writing SQL",
  analytics_code: "Writing analysis code", analytics_interpret: "Explaining results", analytics_resolve: "Resolving the question",
  analytics_needs_code: "Choosing SQL or code", policy_draft: "Drafting a policy", sim_persona: "Simulated user",
};

function Table({ title, rows, label }: { title: string; rows: UsageLine[]; label?: (k: string) => string }) {
  const saved = rows.some((r) => r.saved_tokens > 0); // only behind a Headroom proxy
  return (
    <section className="panel">
      <h3>{title}</h3>
      {rows.length === 0 ? <p className="muted small">No model calls yet.</p> : (
        <div className="table-wrap">
          <table className="log">
            <thead><tr><th></th><th className="r">Calls</th><th className="r">Input</th><th className="r">Cached</th>{saved && <th className="r" title="Input tokens Headroom removed before the call">Saved</th>}<th className="r">Output</th><th className="r">Median time</th><th className="r">Cost</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.key}><td><b>{label ? label(r.key) : r.key}</b></td><td className="r num">{r.calls}</td><td className="r num">{tok(r.input_tokens)}</td>
                <td className="r num">{tok(r.cached_tokens)}</td>{saved && <td className="r num">{tok(r.saved_tokens)}</td>}<td className="r num">{tok(r.output_tokens)}</td>
                <td className="r num">{r.p50_ms == null ? "–" : `${(r.p50_ms / 1000).toFixed(1)} s`}</td>
                <td className="r num">{usd(r.cost_usd)}{r.unpriced_calls > 0 && <span className="muted small" title="Calls to a model with no known price"> +{r.unpriced_calls} unpriced</span>}</td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DailyCost({ days }: { days: UsageLine[] }) {
  const [tip, setTip] = useState<{ x: number; y: number; d: UsageLine } | null>(null);
  const W = 1000, H = 160, L = 56, B = 22, T = 8;
  const max = Math.max(0.0001, ...days.map((d) => d.cost_usd ?? 0));
  const bw = (W - L) / days.length;
  const y = (v: number) => H - B - (v / max) * (H - B - T);
  const every = Math.ceil(days.length / 8);
  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Model cost per day">
        {[0, max / 2, max].map((v) => <g key={v}><line x1={L} x2={W} y1={y(v)} y2={y(v)} className="grid" /><text x={L - 6} y={y(v) + 4} className="axis" textAnchor="end">{usd(v)}</text></g>)}
        {days.map((d, i) => {
          const x = L + i * bw + bw * 0.15, w = Math.max(2, bw * 0.7), v = d.cost_usd ?? 0;
          return (
            <g key={d.key} onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY, d })} onMouseLeave={() => setTip(null)}>
              <rect x={L + i * bw} y={T} width={bw} height={H - B - T} fill="transparent" />
              {v > 0 && <rect x={x} y={y(v)} width={w} height={y(0) - y(v)} className="s-hand" rx={1.5} />}
              {i % every === 0 && <text x={x + w / 2} y={H - 6} className="axis" textAnchor="middle">{short(d.key)}</text>}
            </g>
          );
        })}
      </svg>
      {tip && <div className="tip" style={{ left: tip.x + 12, top: tip.y + 12 }}><div className="tip-title">{short(tip.d.key)}</div>
        <div>{usd(tip.d.cost_usd)} · {tip.d.calls} calls</div><div>{tok(tip.d.input_tokens)} in · {tok(tip.d.output_tokens)} out</div></div>}
    </>
  );
}

function Cheaper({ days }: { days: number }) {
  const [data, setData] = useState<CheaperModels | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.cheaperModels(days).then(setData, (e: Error) => setErr(e.message)); }, [days]);
  return (
    <section className="panel">
      <h3>Cheaper models</h3>
      <p className="muted small">For each job, models from OpenRouter (Gemini, GPT, Llama, Qwen, DeepSeek, Mistral and others) that would have cost less on this
        workspace's own token use. Only price is compared: run a simulation from the console before switching. To switch, set the setting shown to the model id.</p>
      {err && <div className="callout err">{err}</div>}
      {!data && !err && <p className="muted small">Loading…</p>}
      {data?.jobs.map((j) => (
        <details key={j.job} open={j.job !== "speech_to_text"}>
          <summary><b>{j.label}</b> · now <code>{j.model || "browser speech recognition"}</code>{j.cost_usd != null && <> · {usd(j.cost_usd)} over {data.days} days</>}</summary>
          {j.options.length === 0 ? <p className="muted small">Nothing cheaper that can do this job{data.prices_at ? "" : " (prices not fetched yet)"}.</p> : (
            <div className="table-wrap">
              <table className="log">
                <thead><tr><th>Model</th><th className="r">Input / 1M</th><th className="r">Output / 1M</th><th className="r">Context</th><th className="r">{j.input_tokens ? "Would have cost" : ""}</th><th className="r">{j.input_tokens ? "Saving" : ""}</th><th>Set</th></tr></thead>
                <tbody>{j.options.map((o) => (
                  <tr key={o.model}><td><b>{o.name}</b><div className="muted small"><code>{o.model}</code></div></td>
                    <td className="r num">{usd(o.input_per_mtok)}</td><td className="r num">{usd(o.output_per_mtok)}</td>
                    <td className="r num">{tok(o.context)}</td><td className="r num">{j.input_tokens ? usd(o.cost_usd) : ""}</td>
                    <td className="r num">{j.input_tokens && o.saving != null ? `${Math.round(o.saving * 100)}%` : ""}</td>
                    <td><code className="small">{j.setting}={o.model}</code></td></tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </details>
      ))}
    </section>
  );
}

// Settings: the models this workspace runs on, where their prices come from, and what the model calls cost.
export default function Usage({ workspace }: { workspace: string | null }) {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<UsageReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => { api.usage(days).then((d) => { setData(d); setErr(null); }, (e: Error) => setErr(e.message)); }, [days, tick]);

  async function refresh() {
    setRefreshing(true); setNote(null);
    try {
      const r = await api.refreshPrices();
      setNote(`Fetched prices for ${r.models} models. New calls use them; past costs keep the price they were made at.`);
      setTick((t) => t + 1);
    } catch (e) { setNote((e as Error).message); } finally { setRefreshing(false); }
  }

  return (
    <main className="dash">
      <div className="dash-head">
        <div><h1>Settings · usage and cost</h1><p className="muted small">Workspace <code>{workspace ?? "default"}</code>: every model call this workspace's agents made, from its audit log.</p></div>
        <select className="compact" aria-label="Time range" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 14, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}
        </select>
      </div>
      {err && <div className="callout err">{err}</div>}
      {!data && !err && <div className="empty">Loading…</div>}
      {data && (
        <>
          <div className="kpis">
            <div><span className="muted small">Model cost</span><span className="num big">{usd(data.total.cost_usd)}</span>
              {data.total.unpriced_calls > 0 && <span className="muted small">{data.total.unpriced_calls} calls without a price</span>}</div>
            <div><span className="muted small">Per conversation</span><span className="num big">{usd(data.cost_per_conversation)}</span><span className="muted small">{data.conversations} conversations</span></div>
            <div><span className="muted small">Model calls</span><span className="num big">{data.total.calls}</span></div>
            <div><span className="muted small">Input tokens</span><span className="num big">{tok(data.total.input_tokens)}</span><span className="muted small">{tok(data.total.cached_tokens)} from cache{data.total.saved_tokens > 0 && <> · {tok(data.total.saved_tokens)} saved by Headroom</>}</span></div>
            <div><span className="muted small">Output tokens</span><span className="num big">{tok(data.total.output_tokens)}</span></div>
          </div>
          <section className="panel"><h3>Cost per day</h3>
            {data.total.cost_usd == null
              ? <p className="muted small">No cost yet: {data.total.calls ? "none of the models used has a known price. Set one under Models and prices below." : "no model calls in this range."}</p>
              : <DailyCost days={data.by_day} />}</section>
          <Table title="By model" rows={data.by_model} />
          <div className="panels-2">
            <Table title="By what the call was for" rows={data.by_purpose} label={(k) => PURPOSE[k] ?? k.replace(/_/g, " ")} />
            <Table title="By agent" rows={data.by_agent} />
          </div>
          <ApiKeys />
          <Cheaper days={days} />
          <section className="panel">
            <div className="row-between">
              <h3>Models and prices</h3>
              <button className="compact" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? "Fetching…" : "Fetch latest prices"}</button>
            </div>
            {note && <p className="muted small" role="status">{note}</p>}
            <dl className="facts">
              <dt>Replies</dt><dd><code>{data.models.generate}</code></dd>
              <dt>Classifying</dt><dd><code>{data.models.classify}</code></dd>
              <dt>Checking answers</dt><dd><code>{data.models.verify}</code></dd>
            </dl>
            <div className="table-wrap">
              <table className="log">
                <thead><tr><th>Model</th><th className="r">Input / 1M tokens</th><th className="r">Output / 1M tokens</th><th>Price from</th></tr></thead>
                <tbody>{Object.entries(data.prices).map(([m, p]) => (
                  <tr key={m}><td><code>{m}</code></td><td className="r num">{usd(p.input_per_mtok)}</td><td className="r num">{usd(p.output_per_mtok)}</td>
                    <td>{p.source === "config" ? "your setting" : p.source === "openrouter" ? "OpenRouter" : p.source === "litellm" ? "litellm price list" : <span className="bad-text">unknown: set it</span>}</td></tr>
                ))}</tbody>
              </table>
            </div>
            <p className="muted small">Models are set on the server (<code>YACHAT_MODEL_GENERATE</code>, <code>YACHAT_MODEL_CLASSIFY</code>, <code>YACHAT_MODEL_VERIFY</code>).
              To set or correct a price, use <code>YACHAT_MODEL_PRICES</code>, for example <code>{'{"my-model": [0.5, 2]}'}</code> (USD per million input and output tokens).
              Otherwise prices come from OpenRouter's public list, refreshed daily{data.openrouter_prices_at ? ` (last fetched ${new Date(data.openrouter_prices_at).toLocaleString()})` : " (not fetched yet)"}, then litellm's.
              OpenRouter's price can differ from your provider's own: set it to bill exactly. Simulations are not included: they run on their own log.</p>
          </section>
        </>
      )}
    </main>
  );
}
