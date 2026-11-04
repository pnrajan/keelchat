import { useEffect, useState } from "react";
import { api, KeyMetric, MetricsAnswer, WorkspaceMetrics } from "./api";

const SUGGESTED = [
  "Which agent has the highest handoff rate?",
  "Completion rate per day",
  "Which questions are rejected most often?",
  "Model cost per agent and model",
  "Slowest 10 turns and what they did",
];

function fmt(m: KeyMetric): string {
  const v = m.value;
  if (v == null) return "–";
  switch (m.unit) {
    case "rate": return `${(v * 100).toFixed(1)}%`;
    case "ms": return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
    case "usd": return v >= 1 ? `$${v.toFixed(2)}` : v >= 0.01 ? `$${v.toFixed(3)}` : `$${v.toFixed(5)}`;
    case "minutes": return v < 60 ? `${Math.round(v)} min` : `${(v / 60).toFixed(1)} h`;
    case "count": return String(Math.round(v));
    default: return String(Math.round(v * 10) / 10);
  }
}

const cell = (v: unknown) => (v == null ? "–" : typeof v === "number" ? (Number.isInteger(v) ? String(v) : String(Math.round(v * 10000) / 10000)) : String(v));

// Key metrics as SQL over this workspace's audit records, and questions about them in plain English (text to SQL).
export default function KeyMetrics({ days, agent }: { days: number; agent: string }) {
  const [data, setData] = useState<WorkspaceMetrics | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<MetricsAnswer | null>(null);
  const [asking, setAsking] = useState(false);
  const [askErr, setAskErr] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    api.workspaceMetrics(days, agent || undefined).then((d) => { setData(d); setErr(null); }, (e: Error) => setErr(e.message));
  }, [days, agent]);

  async function ask(q: string) {
    if (q.trim().length < 3) return;
    setQuestion(q); setAsking(true); setAskErr(null); setAnswer(null);
    try { setAnswer(await api.askMetrics(q.trim(), days, agent || undefined)); }
    catch (e) { setAskErr((e as Error).message); }
    finally { setAsking(false); }
  }

  return (
    <>
      <section className="panel">
        <div className="row-between"><h3>Key metrics</h3><span className="muted small">each one is a SQL query you can read, copy or build on</span></div>
        {err && <div className="callout err">{err}</div>}
        {!data && !err && <p className="muted small">Loading…</p>}
        {data && (
          <div className="table-wrap">
            <table className="log metrics">
              <thead><tr><th>Metric</th><th className="r">Value</th><th>Meaning</th><th>SQL</th></tr></thead>
              <tbody>{data.metrics.map((m) => (
                <tr key={m.name}>
                  <td><b>{m.label}</b><div className="muted small"><code>{m.name}</code></div></td>
                  <td className="r num big-cell">{fmt(m)}</td>
                  <td className="muted small">{m.description}</td>
                  <td><details><summary>Show</summary><pre className="sql">{m.sql}</pre></details></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <h3>Ask about these metrics</h3>
        <p className="muted small">Ask in plain English. The question becomes one read-only SQL query over this workspace's tables for the selected range and agent; the SQL is shown with the answer.</p>
        <form className="field-row" onSubmit={(e) => { e.preventDefault(); void ask(question); }}>
          <input id="metrics-question" className="grow" aria-label="Question about your metrics" placeholder="e.g. Which agent has the highest handoff rate?"
            value={question} maxLength={500} onChange={(e) => setQuestion(e.target.value)} />
          <button className="primary" type="submit" disabled={asking || question.trim().length < 3}>{asking ? "Querying…" : "Ask"}</button>
        </form>
        <div className="suggestions">{SUGGESTED.map((q) => <button key={q} type="button" className="chip" disabled={asking} onClick={() => void ask(q)}>{q}</button>)}</div>
        {askErr && <div className="callout err">{askErr}</div>}
        {answer && (
          <div className="answer">
            <details open><summary>SQL{answer.attempts > 1 ? ` (fixed after ${answer.attempts - 1} failed ${answer.attempts === 2 ? "try" : "tries"})` : ""}</summary><pre className="sql">{answer.sql}</pre></details>
            {answer.rows.length === 0 ? <p className="muted small">The query ran and returned no rows.</p> : (
              <div className="table-wrap">
                <table className="log">
                  <thead><tr>{answer.columns.map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr></thead>
                  <tbody>{answer.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={typeof v === "number" ? "r num" : ""}>{cell(v)}</td>)}</tr>)}</tbody>
                </table>
              </div>
            )}
            {answer.truncated && <p className="muted small">Showing the first 200 rows.</p>}
          </div>
        )}
        {data && (
          <details><summary>Tables you can ask about</summary>
            <ul className="tables-list">{data.tables.map((t) => (
              <li key={t.name}><code>{t.name}</code> <span className="muted">({t.rows} rows)</span> {t.description}<div className="muted small">{t.columns.join(", ")}</div></li>
            ))}</ul>
          </details>
        )}
      </section>
    </>
  );
}
