import { useEffect, useMemo, useState } from "react";
import { api, AgentSummary, Dashboard as Data } from "./api";
import KeyMetrics from "./KeyMetrics";

const pct = (x: number) => `${Math.round(x * 100)}%`;
const secs = (ms: number | null) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
const mins = (m: number | null) => (m == null ? "–" : m < 60 ? `${Math.round(m)} min` : `${(m / 60).toFixed(1)} h`);
const short = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const SERIES = [
  { key: "completed", label: "Completed", cls: "s-done" },
  { key: "handed_off", label: "Handed to a person", cls: "s-hand" },
  { key: "dropped", label: "Dropped", cls: "s-drop" },
] as const;

type Tip = { x: number; y: number; lines: string[] } | null;

function niceMax(v: number) {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / p) * p;
}

function Outcomes({ days, onTip }: { days: Data["days"]; onTip: (t: Tip) => void }) {
  const W = 1000, H = 220, L = 32, B = 22, T = 8;
  const max = niceMax(Math.max(1, ...days.map((d) => d.completed + d.handed_off + d.dropped)));
  const bw = (W - L) / days.length;
  const y = (v: number) => H - B - (v / max) * (H - B - T);
  const every = Math.ceil(days.length / 8);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Conversations per day by outcome">
      {[0, max / 2, max].map((v) => (
        <g key={v}><line x1={L} x2={W} y1={y(v)} y2={y(v)} className="grid" /><text x={L - 6} y={y(v) + 4} className="axis" textAnchor="end">{v}</text></g>
      ))}
      {days.map((d, i) => {
        let acc = 0;
        const x = L + i * bw + bw * 0.15, w = Math.max(2, bw * 0.7);
        return (
          <g key={d.day} onMouseLeave={() => onTip(null)}
            onMouseMove={(e) => onTip({ x: e.clientX, y: e.clientY, lines: [short(d.day), `${d.conversations} conversations`, `${d.completed} completed · ${d.handed_off} handed off · ${d.dropped} dropped`] })}>
            <rect x={L + i * bw} y={T} width={bw} height={H - B - T} fill="transparent" />
            {SERIES.map((s) => {
              const v = d[s.key];
              if (!v) return null;
              const top = y(acc + v), h = y(acc) - top;
              acc += v;
              return <rect key={s.key} x={x} y={top} width={w} height={Math.max(0, h - 1.5)} className={s.cls} rx={1.5} />;
            })}
            {i % every === 0 && <text x={x + w / 2} y={H - 6} className="axis" textAnchor="middle">{short(d.day)}</text>}
          </g>
        );
      })}
    </svg>
  );
}

function Latency({ days, onTip }: { days: Data["days"]; onTip: (t: Tip) => void }) {
  const W = 1000, H = 180, L = 44, B = 22, T = 8;
  const max = niceMax(Math.max(1, ...days.map((d) => d.p95_ms ?? 0)) / 1000) * 1000;
  const step = (W - L) / Math.max(1, days.length - 1);
  const x = (i: number) => L + i * step;
  const y = (v: number) => H - B - (v / max) * (H - B - T);
  const line = (k: "p50_ms" | "p95_ms") => days.map((d, i) => (d[k] == null ? null : `${x(i)},${y(d[k]!)}`)).filter(Boolean).join(" ");
  const every = Math.ceil(days.length / 8);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Reply time per day, median and 95th percentile">
      {[0, max / 2, max].map((v) => (
        <g key={v}><line x1={L} x2={W} y1={y(v)} y2={y(v)} className="grid" /><text x={L - 6} y={y(v) + 4} className="axis" textAnchor="end">{secs(v)}</text></g>
      ))}
      <polyline points={line("p95_ms")} className="ln-p95" />
      <polyline points={line("p50_ms")} className="ln-p50" />
      {days.map((d, i) => (
        <g key={d.day} onMouseLeave={() => onTip(null)}
          onMouseMove={(e) => onTip({ x: e.clientX, y: e.clientY, lines: [short(d.day), `median ${secs(d.p50_ms)}`, `95th pct ${secs(d.p95_ms)}`, `${d.turns} turns · ${d.error_turns} with errors`] })}>
          <rect x={x(i) - step / 2} y={T} width={step} height={H - B - T} fill="transparent" />
          {d.p95_ms != null && <circle cx={x(i)} cy={y(d.p95_ms)} r={2.5} className="pt-p95" />}
          {d.p50_ms != null && <circle cx={x(i)} cy={y(d.p50_ms)} r={2.5} className="pt-p50" />}
          {i % every === 0 && <text x={x(i)} y={H - 6} className="axis" textAnchor="middle">{short(d.day)}</text>}
        </g>
      ))}
    </svg>
  );
}

// The tenant's outcome dashboard: how conversations end, how fast replies are, and how people handle handoffs.
export default function Dashboard({ workspace, onOpenHandoffs }: { workspace: string | null; onOpenHandoffs: () => void }) {
  const [days, setDays] = useState(30);
  const [agent, setAgent] = useState("");
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tip, setTip] = useState<Tip>(null);

  useEffect(() => { api.agents().then(setAgents, () => undefined); }, []);
  useEffect(() => {
    setData(null);
    api.dashboard(days, agent || undefined).then((d) => { setData(d); setErr(null); }, (e: Error) => setErr(e.message));
  }, [days, agent]);
  const s = data?.summary;
  const empty = useMemo(() => !!data && data.summary.sessions === 0, [data]);

  return (
    <main className="dash">
      <div className="dash-head">
        <div>
          <h1>Outcomes</h1>
          <p className="muted small">Workspace <code>{workspace ?? "default"}</code>: every number comes from this workspace's audit log.</p>
        </div>
        <div className="field-row">
          <select className="compact" aria-label="Agent" value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">All agents</option>
            {agents.map((a) => <option key={a.agent} value={a.agent}>{a.agent}</option>)}
          </select>
          <select className="compact" aria-label="Time range" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[7, 14, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}
          </select>
        </div>
      </div>
      {err && <div className="callout err">{err}</div>}
      {!data && !err && <div className="empty">Loading…</div>}
      {data && s && (
        <>
          <div className="kpis">
            <div><span className="muted small">Conversations</span><span className="num big">{s.sessions}</span></div>
            <div><span className="muted small">Completed</span><span className="num big">{pct(s.completion_rate)}</span></div>
            <div><span className="muted small">Handed to a person</span><span className="num big">{pct(s.handoff_rate)}</span></div>
            <div><span className="muted small">Median reply time</span><span className="num big">{secs(data.p50_ms)}</span><span className="muted small">95th pct {secs(data.p95_ms)}</span></div>
            <button className="kpi-link" onClick={onOpenHandoffs}>
              <span className="muted small">Open handoffs</span><span className={`num big${data.open_handoffs ? " warn" : ""}`}>{data.open_handoffs}</span>
              <span className="muted small">claimed in {mins(data.claim_p50_min)} · resolved in {mins(data.resolve_p50_min)} (median)</span>
            </button>
          </div>
          {empty ? <div className="empty">No conversations in this range yet. Talk to an agent or run a simulation from the console; real conversations show up here.</div> : (
            <>
              <section className="panel">
                <div className="row-between"><h3>Conversations per day, by how they ended</h3>
                  <div className="legend">{SERIES.map((x) => <span key={x.key}><i className={x.cls} />{x.label}</span>)}</div></div>
                <Outcomes days={data.days} onTip={setTip} />
              </section>
              <section className="panel">
                <div className="row-between"><h3>Reply time per turn</h3>
                  <div className="legend"><span><i className="ln-p50" />Median</span><span><i className="ln-p95" />95th percentile</span></div></div>
                <Latency days={data.days} onTip={setTip} />
                <p className="muted small">{data.llm_calls_per_turn} model calls per turn · {pct(data.error_rate)} of turns had an error</p>
              </section>
              <KeyMetrics days={days} agent={agent} />
              <section className="panel">
                <h3>By agent</h3>
                <div className="table-wrap">
                  <table className="log">
                    <thead><tr><th>Agent</th><th className="r">Conversations</th><th className="r">Completed</th><th className="r">Handed off</th><th className="r">Dropped</th><th className="r">Turns each</th><th className="r">95th pct reply</th><th>Most rejected question</th></tr></thead>
                    <tbody>{data.agents.map((a) => (
                      <tr key={a.agent}><td><b>{a.agent}</b></td><td className="r num">{a.conversations}</td><td className="r num">{pct(a.completion_rate)}</td><td className="r num">{pct(a.handoff_rate)}</td>
                        <td className="r num">{pct(a.drop_rate)}</td><td className="r num">{a.turns_per_conversation}</td><td className="r num">{secs(a.p95_ms)}</td><td>{a.top_rejected_field?.replace(/_/g, " ") ?? "–"}</td></tr>
                    ))}</tbody>
                  </table>
                </div>
              </section>
              <div className="panels-2">
                {([["How conversations ended", s.outcomes], ["Why they went to a person", s.handoff_reasons], ["Rejected answers by question", s.rejected_answers], ["Guardrail blocks", s.guardrails], ["Tool failures", s.tool_failures]] as const).map(([t, m]) => (
                  <section key={t} className="panel"><h3>{t}</h3>
                    {Object.keys(m).length === 0 ? <p className="muted small">None.</p> : (
                      <table className="log"><tbody>{Object.entries(m).sort((a, b) => b[1] - a[1]).map(([k, n]) => <tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="r num">{n}</td></tr>)}</tbody></table>
                    )}
                  </section>
                ))}
              </div>
            </>
          )}
        </>
      )}
      {tip && <div className="tip" style={{ left: tip.x + 12, top: tip.y + 12 }}>{tip.lines.map((l, i) => <div key={i} className={i === 0 ? "tip-title" : ""}>{l}</div>)}</div>}
    </main>
  );
}
