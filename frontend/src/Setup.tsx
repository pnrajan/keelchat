// Configure an agent without YAML, and see what it still needs before launch. Every change edits the policy text
// (comments kept), so the editor, the live build check and this panel always agree.
import { useEffect, useState } from "react";
import { api, Readiness } from "./api";
import { Field, moveStep, readConfig, removeStep, setKey, setStep } from "./policyDoc";
import { flowSteps, type Root } from "./policySchema";

type Props = { policy: string; refresh: number; onChange: (yaml: string) => void; schema: Root | null; onSave: () => void; onAddStep: (name: string) => void };

const AREAS: Record<string, string> = { policy: "Policy", connections: "Connections", safety: "Safety", launch: "Launch" };
const MARK = { ok: "✓", warning: "!", missing: "✕" } as const;

function enumOf(schema: Root | null, ...path: string[]): string[] {
  let s: unknown = schema;
  for (const k of path) s = (s as Record<string, unknown> | undefined)?.[k];
  return ((s as { enum?: string[] } | undefined)?.enum ?? []).map(String);
}

export default function Setup({ policy, refresh, onChange, schema, onSave, onAddStep }: Props) {
  const cfg = readConfig(policy);
  const [report, setReport] = useState<Readiness | null>(null);
  const [focus, setFocus] = useState<string | null>(null);

  useEffect(() => {  // the checklist follows the text, half a second after the last edit
    const t = window.setTimeout(() => { api.readiness(policy).then(setReport, () => setReport(null)); }, 500);
    return () => window.clearTimeout(t);
  }, [policy, refresh]);

  useEffect(() => { if (focus) document.getElementById(focus)?.focus(); }, [focus]);

  if (!cfg) return <div className="callout err">The YAML doesn't parse, so the settings can't be shown. Fix the line marked in the editor.</div>;

  const tools = enumOf(schema, "properties", "tools", "items");
  const ins = enumOf(schema, "properties", "guardrails", "properties", "input", "items");
  const outs = enumOf(schema, "properties", "guardrails", "properties", "output", "items");
  const toggle = (have: string[], v: string) => (have.includes(v) ? have.filter((x) => x !== v) : [...have, v]);
  const defaults = ((schema as Record<string, unknown> | null)?.["x-defaults"] ?? {}) as Record<string, string[]>;
  const baseIn = defaults.base_input_guardrails ?? [], baseOut = defaults.base_output_guardrails ?? [];
  // what runs: the policy's list plus the always-on base, or the platform defaults when the policy sets none
  const input = cfg.input ?? defaults.input_guardrails ?? [], output = cfg.output ?? defaults.output_guardrails ?? [];
  const guard = (stage: "input" | "output", list: string[], base: string[], g: string) => (
    <label key={g} className="check" title={base.includes(g) ? "Always on" : undefined}>
      <input type="checkbox" checked={base.includes(g) || list.includes(g)} disabled={base.includes(g)}
        onChange={() => onChange(setKey(policy, ["guardrails", stage], toggle(list.filter((x) => !base.includes(x)), g)))} />
      {g.replace(/_/g, " ")}
    </label>
  );

  function fix(action: string) {
    if (action === "save") onSave();
    else if (action === "charter") setFocus("setup-charter");
    else if (action === "guardrails") setFocus("setup-guardrails");
    else if (action.startsWith("add_step:")) onAddStep(action.slice(9));
    else if (action === "add_step") setFocus("setup-add-step");
  }
  const fixLabel: Record<string, string> = { save: "Save version", charter: "Edit charter", guardrails: "Choose checks", add_step: "Add a step" };

  const setFields = (i: number, fields: Field[]) =>
    onChange(setStep(policy, i, "fields", fields.map((f) => ({ name: f.name, ask: f.ask, expect: f.expect, ...(f.optional ? { optional: true } : {}) }))));

  return (
    <div className="setup">
      <section className="readiness" aria-live="polite">
        <div className="row-between">
          <h3>Ready to launch?</h3>
          {report && <span className={`badge ${report.ready ? "ok" : "partial"}`}>{report.ready ? "ready" : `${report.score}%`}</span>}
        </div>
        {!report ? <p className="muted small">Checking…</p> : Object.keys(AREAS).map((area) => {
          const items = report.items.filter((i) => i.area === area);
          return items.length ? (
            <div key={area} className="area">
              <h4>{AREAS[area]}</h4>
              <ul>{items.map((i) => (
                <li key={i.id} className={i.status}>
                  <span className="mark" aria-label={i.status}>{MARK[i.status]}</span>
                  <div><p>{i.title}{i.blocking && i.status === "missing" && <span className="tag">blocks launch</span>}</p>{i.detail && <p className="muted small">{i.detail}</p>}</div>
                  {i.fix && i.status !== "ok" && (fixLabel[i.fix] || i.fix.startsWith("add_step:")) && (
                    <button className="compact" onClick={() => fix(i.fix!)}>{fixLabel[i.fix] ?? `Add ${i.fix.slice(9).replace(/_/g, " ")}`}</button>
                  )}
                </li>
              ))}</ul>
            </div>
          ) : null;
        })}
      </section>

      <section className="config">
        <h3>Settings</h3>
        <label>Charter<span className="muted small">What it helps with, its tone, and what is out of scope.</span>
          <textarea id="setup-charter" rows={4} value={cfg.charter} onChange={(e) => onChange(setKey(policy, ["charter"], e.target.value))} />
        </label>
        <label>Languages<span className="muted small">Comma-separated codes, e.g. en, es.</span>
          <input id="setup-languages" value={cfg.languages.join(", ")}
            onChange={(e) => onChange(setKey(policy, ["languages"], e.target.value.split(",").map((s) => s.trim()).filter(Boolean)))} />
        </label>

        <fieldset id="setup-guardrails" tabIndex={-1}>
          <legend>Safety checks</legend>
          <div className="checks-row"><span className="muted small">On what users send</span>
            {ins.map((g) => guard("input", input, baseIn, g))}
          </div>
          <div className="checks-row"><span className="muted small">On what it replies</span>
            {outs.map((g) => guard("output", output, baseOut, g))}
          </div>
          <p className="muted small">{cfg.input === null && cfg.output === null ? "Using the platform defaults. " : ""}Greyed-out checks always run.</p>
        </fieldset>

        <fieldset>
          <legend>Extra tools</legend>
          <div className="checks-row">
            {tools.map((t) => <label key={t} className="check"><input type="checkbox" checked={cfg.tools.includes(t)} onChange={() => onChange(setKey(policy, ["tools"], toggle(cfg.tools, t)))} />{t.replace(/_/g, " ")}</label>)}
          </div>
          <p className="muted small">Steps bring their own tools; these are for side questions at any point.</p>
        </fieldset>

        <fieldset>
          <legend>Limits</legend>
          <div className="field-row">
            <label>Re-asks per question<input id="setup-reasks" type="number" min={1} max={10} value={cfg.reasks ?? ""} placeholder="3"
              onChange={(e) => onChange(setKey(policy, ["limits", "reasks"], e.target.value ? Number(e.target.value) : undefined))} /></label>
            <label>Off-topic turns<input id="setup-digressions" type="number" min={1} max={10} value={cfg.digressions ?? ""} placeholder="3"
              onChange={(e) => onChange(setKey(policy, ["limits", "digressions"], e.target.value ? Number(e.target.value) : undefined))} /></label>
          </div>
        </fieldset>

        <fieldset>
          <legend>Steps</legend>
          {cfg.steps.length === 0 && <p className="muted small">No steps: the agent holds an open conversation.</p>}
          <ol className="step-list">{cfg.steps.map((s, i) => (
            <li key={`${i}-${s.name}`}>
              <div className="row-between">
                <strong>{i + 1}. {s.name.replace(/_/g, " ")}</strong>
                <span className="step-actions">
                  <button className="compact ghost" aria-label={`Move ${s.name} up`} disabled={i === 0} onClick={() => onChange(moveStep(policy, i, -1))}>↑</button>
                  <button className="compact ghost" aria-label={`Move ${s.name} down`} disabled={i === cfg.steps.length - 1} onClick={() => onChange(moveStep(policy, i, 1))}>↓</button>
                  <button className="compact ghost" aria-label={`Remove ${s.name}`} onClick={() => onChange(removeStep(policy, i))}>Remove</button>
                </span>
              </div>
              <label className="check"><input type="checkbox" checked={s.verify} onChange={() => onChange(setStep(policy, i, "verify", s.verify ? false : undefined))} />
                Check replies before sending</label>
              {s.fields && (
                <div className="form-fields">
                  {s.fields.map((f, j) => (
                    <div key={j} className="form-field">
                      <input aria-label="Field name" value={f.name} placeholder="field_name"
                        onChange={(e) => setFields(i, s.fields!.map((x, k) => (k === j ? { ...x, name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_") } : x)))} />
                      <input aria-label="What to ask" value={f.ask} placeholder="What to ask (the model phrases it)" onChange={(e) => setFields(i, s.fields!.map((x, k) => (k === j ? { ...x, ask: e.target.value } : x)))} />
                      <input aria-label="What counts as an answer" value={f.expect} placeholder="What counts as a valid answer" onChange={(e) => setFields(i, s.fields!.map((x, k) => (k === j ? { ...x, expect: e.target.value } : x)))} />
                      <div className="row-between">
                        <label className="check"><input type="checkbox" checked={f.optional} onChange={() => setFields(i, s.fields!.map((x, k) => (k === j ? { ...x, optional: !x.optional } : x)))} />optional</label>
                        <button className="compact ghost" onClick={() => setFields(i, s.fields!.filter((_, k) => k !== j))}>Remove field</button>
                      </div>
                    </div>
                  ))}
                  <button className="compact" onClick={() => setFields(i, [...s.fields!, { name: `field_${s.fields!.length + 1}`, ask: "", expect: "", optional: false }])}>+ Add field</button>
                </div>
              )}
            </li>
          ))}</ol>
          <select id="setup-add-step" className="compact" value="" disabled={!schema} onChange={(e) => e.target.value && onAddStep(e.target.value)} aria-label="Add a step">
            <option value="">+ Add step</option>
            {schema && flowSteps(schema).map((s) => <option key={s.name} value={s.name} title={s.description}>{s.name.replace(/_/g, " ")}: {s.description}</option>)}
          </select>
        </fieldset>
        {cfg.memory > 0 && <p className="muted small">Remembers {cfg.memory} item{cfg.memory === 1 ? "" : "s"} per user (edit under <code>memory</code> in the YAML).</p>}
      </section>
    </div>
  );
}
