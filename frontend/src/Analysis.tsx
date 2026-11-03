// The full AnalysisResult of the latest analytics turn: input, data, assumptions, findings, summary, recommendations.
type Col = { name: string; type: string };
type Finding = { id: string; statement: string; values: Record<string, unknown>; evidence: string };
type Rec = { action: string; rationale: string; based_on: string[]; confidence: string };
export type Verification = {
  plan: { method: string; rationale: string; assumptions: string[]; expected: string };
  checks: { id: string; claim: string; code: string }[];
  results: { id: string; passed: boolean; detail: string }[];
  issues: { stage: string; problem: string }[];
  rounds: number;
  passed: boolean;
};
export type AnalysisResult = {
  status: string;
  error_kind?: string | null;
  input: { question: string; resolved_question: string; dataset: string; dataset_version: string };
  data: {
    tables: string[]; sql: string; columns: Col[]; rows: unknown[][]; row_count: number; truncated: boolean;
    code?: string | null; code_output?: Record<string, unknown> | null; sandbox?: string | null;
  };
  assumptions: { text: string; source: "system" | "analyst" }[];
  outcome: { method: string; findings: Finding[]; statistics: Record<string, unknown>; limitations: string[]; attempts: Record<string, number> };
  summary: string;
  recommendations: Rec[];
  verification?: Verification | null;
};

const SHOWN_ROWS = 20;
const STATUS_TEXT: Record<string, string> = {
  ok: "Every figure checked against the data",
  partial: "Partly answered: see limitations",
  failed: "Could not answer",
};

export function latestAnalysis(items: Record<string, unknown>[] | undefined): AnalysisResult | null {
  const it = (items ?? []).find((i) => i.type === "analysis_result");
  return it ? (it.result as AnalysisResult) : null;
}

/** The verification of the latest turn: an analysis's, or a verified answer's own item (knowledge). */
export function latestVerification(items: Record<string, unknown>[] | undefined): Verification | null {
  const it = (items ?? []).find((i) => i.type === "verification");
  return it ? (it.verification as Verification) : latestAnalysis(items)?.verification ?? null;
}

function VerificationView({ v }: { v: Verification }) {
  const results = new Map(v.results.map((r) => [r.id, r]));
  return (
    <section className="verification">
      <h3>Verification <span className="muted small">{plural(v.rounds, "round")}</span></h3>
      <div className={`status-banner ${v.passed ? "ok" : "partial"}`}>
        <span className={`badge ${v.passed ? "ok" : "partial"}`}>{v.passed ? "verified" : "not verified"}</span>
        <span>{v.passed ? "Every check passed and the verifier found no problems" : "Shown with its problems; not presented as verified"}</span>
      </div>
      <p><strong>Plan:</strong> {v.plan.method}</p>
      <p className="muted small">{v.plan.rationale}</p>
      <ul className="checks">{v.checks.map((c) => {
        const r = results.get(c.id);
        return (
          <li key={c.id}>
            <span className={`tag ${r?.passed ? "pass" : "fail"}`}>{r?.passed ? "pass" : "fail"}</span>
            <div><p>{c.claim}{c.code && <span className="muted small"> (run in the sandbox)</span>}</p>
              {r?.detail && !r.passed && <p className="muted small">{r.detail}</p>}
              {c.code && <pre className="code">{c.code}</pre>}</div>
          </li>);
      })}</ul>
      {v.issues.length > 0 && (
        <><h4>Problems found</h4><ul className="muted small plain">{v.issues.map((i, n) => <li key={n}><span className="tag">{i.stage}</span> {i.problem}</li>)}</ul></>
      )}
    </section>
  );
}

const label = (k: string) => k.replace(/_/g, " ");
export const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return String(v);
}

export default function Analysis({ result, verification }: { result: AnalysisResult | null; verification?: Verification | null }) {
  if (!result && verification) return <div className="analysis"><VerificationView v={verification} /></div>;
  if (!result) return <div className="empty">Ask an analytics or knowledge question to see its analysis and verification here.</div>;
  const { input, data, outcome } = result;
  const numeric = new Set(data.columns.filter((c) => /INT|DOUBLE|DECIMAL|FLOAT|NUMERIC|REAL/i.test(c.type)).map((c) => c.name));
  return (
    <div className="analysis">
      <div className={`status-banner ${result.status}`}>
        <span className={`badge ${result.status}`}>{result.status}</span>
        <span>{STATUS_TEXT[result.status] ?? result.status}{result.error_kind ? ` (${result.error_kind})` : ""}</span>
      </div>
      <div className="provenance">
        <span>dataset <code>{input.dataset}</code> @ <code>{input.dataset_version}</code></span>
        <span>{outcome.method === "sql+python" ? `SQL + Python (${data.sandbox ?? "sandbox"})` : "SQL"}</span>
        <span>attempts {Object.entries(outcome.attempts).map(([k, v]) => `${k} ${v}`).join(" · ") || "-"}</span>
      </div>

      <section>
        <h3>Question</h3>
        <p>{input.question}</p>
        {input.resolved_question !== input.question && <p className="muted small">Interpreted as: {input.resolved_question}</p>}
      </section>

      {result.summary && <section className="summary"><h3>Summary</h3><p>{result.summary}</p></section>}

      {result.recommendations.length > 0 && (
        <section>
          <h3>Recommendations</h3>
          <ol className="recs">{result.recommendations.map((r, i) => (
            <li key={i}>
              <div className="row-between"><strong>{r.action}</strong><span className={`tag conf-${r.confidence}`}>{r.confidence}</span></div>
              <p className="muted small">{r.rationale} <span className="cite">based on {r.based_on.join(", ")}</span></p>
            </li>))}</ol>
        </section>
      )}

      <section>
        <h3>Findings</h3>
        {outcome.findings.length ? (
          <ul className="findings">{outcome.findings.map((f) => (
            <li key={f.id}><span className="fid">{f.id}</span><div>
              <p>{f.statement}</p>
              {Object.keys(f.values).length > 0 && <p className="values">{Object.entries(f.values).map(([k, v]) => <span key={k}>{label(k)} <b>{fmt(v)}</b></span>)}</p>}
              {f.evidence && <p className="muted small">{f.evidence}</p>}
            </div></li>))}</ul>
        ) : <p className="muted">No verified findings.</p>}
      </section>

      {Object.keys(outcome.statistics).length > 0 && (
        <section>
          <h3>Statistics</h3>
          <dl className="stats">{Object.entries(outcome.statistics).map(([k, v]) => <div key={k}><dt>{label(k)}</dt><dd>{fmt(v)}</dd></div>)}</dl>
        </section>
      )}

      {result.verification && <VerificationView v={result.verification} />}

      {result.assumptions.length > 0 && (
        <section>
          <h3>Assumptions</h3>
          <ul className="assumptions">{result.assumptions.map((a, i) => <li key={i}><span className={`tag ${a.source}`}>{a.source}</span>{a.text}</li>)}</ul>
        </section>
      )}
      {outcome.limitations.length > 0 && (
        <section><h3>Limitations</h3><ul className="muted small plain">{outcome.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul></section>
      )}

      <section>
        <h3>Data <span className="muted small">{plural(data.row_count, "row")} · {data.tables.join(", ") || "-"}</span></h3>
        {data.columns.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr>{data.columns.map((c) => <th key={c.name} title={c.type} className={numeric.has(c.name) ? "num" : ""}>{c.name}</th>)}</tr></thead>
              <tbody>{data.rows.slice(0, SHOWN_ROWS).map((r, i) => (
                <tr key={i}>{r.map((v, j) => <td key={j} className={numeric.has(data.columns[j]?.name) ? "num" : ""}>{fmt(v)}</td>)}</tr>
              ))}</tbody>
            </table>
          </div>
        )}
        {(data.truncated || data.rows.length > SHOWN_ROWS) && <p className="muted small">Showing {Math.min(SHOWN_ROWS, data.rows.length)} of {data.row_count.toLocaleString()} rows.</p>}
        <details><summary>SQL</summary><pre className="code">{data.sql}</pre></details>
        {data.code && <details><summary>Python ({data.sandbox})</summary><pre className="code">{data.code}</pre></details>}
        {data.code_output && <details><summary>Code output</summary><pre className="code">{JSON.stringify(data.code_output, null, 2)}</pre></details>}
      </section>
    </div>
  );
}
