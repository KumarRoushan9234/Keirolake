import { ArrowLeft, ChatCircleDots, Sparkle } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { api, KB } from "../../api";
import { clearTask, runTask, useTask } from "../../background";
import { Card, ErrorNote, useKept } from "../../ui";
import { INTERVALS } from "./types";

type Field = { name: string; type: string; description: string };
type Step = { type: string; [k: string]: any };
type Spec = { name: string; goal: string; steps: Step[]; fields: Field[]; outputs: { table: boolean; knowledge_base: boolean }; interval_minutes: number };
type Plan = { spec: Spec; estimate: { total_calls: number; keiro_calls: Record<string, number>; note: string }; model: string; summary?: string };
type Question = { id: string; question: string; options: string[] };

const EXAMPLES = [
  "Every electric scooter sold in India this year with price, range and top speed",
  "Each week, the newest posts from qdrant.tech/blog with title, date and a one-line summary",
  "Indian states with population, capital and official language",
];

const stepText = (s: Step) =>
  s.type === "search" ? `Search “${s.query}”`
  : s.type === "extract" ? `Read ${s.urls.length} page${s.urls.length === 1 ? "" : "s"}`
  : s.type === "sitemap" ? `Newest ${s.limit} pages of ${s.site}${s.path_prefix || ""}`
  : `Research: ${s.question}`;

/** Describe a sheet in words: the planner asks what's missing, shows the plan, then the sheet fills itself with KeiroLabs (ZDR). */
export default function Describe({ onDone, onCancel }: { onDone: (sheetId?: string) => void; onCancel: () => void }) {
  const [goal, setGoal] = useKept("describe:goal", "");
  const [questions, setQuestions] = useKept<Question[] | null>("describe:questions", null);
  const [answers, setAnswers] = useKept<Record<string, string>>("describe:answers", {});
  const [plan, setPlan] = useKept<Plan | null>("describe:plan", null);
  const [kbs, setKbs] = useState<KB[]>([]);
  const [kb, setKb] = useState("");
  // Each step runs in the background store: leave the page while it plans and the result is here when you come back.
  type Out = { questions?: Question[]; plan?: Plan; sheet?: string };
  const task = useTask<Out>("describe");
  const busy = task.status === "running";
  const error = task.error ?? "";
  const setError = (_: string) => clearTask("describe");
  useEffect(() => { api<KB[]>("/kbs").then((l) => { setKbs(l); setKb(l[0]?.id ?? ""); }).catch(() => {}); }, []);
  useEffect(() => {
    if (task.status !== "done" || !task.result) return;
    const r = task.result;
    clearTask("describe");
    if (r.sheet) { reset(); setGoal(""); onDone(r.sheet); return; }
    if (r.questions) setQuestions(r.questions);
    if (r.plan) { setPlan(r.plan); setQuestions(null); }
  }, [task.status]);
  const step = (label: string, fn: () => Promise<Out>) => runTask("describe", label, fn, { page: "Tables" });

  const reset = () => { setQuestions(null); setAnswers({}); setPlan(null); };
  const given = () => (questions ?? []).map((q) => ({ question: q.question, answer: answers[q.id] ?? "" })).filter((a) => a.answer.trim());

  function start() {
    reset();
    step(`Plan a table: ${goal.slice(0, 40)}`, async () => {
      const out = await api<{ questions: Question[] }>("/sheets/build/clarify", { method: "POST", json: { goal } });
      if (out.questions.length) return { questions: out.questions };
      return { plan: await api<Plan>("/sheets/build/plan", { method: "POST", json: { goal, answers: [] } }) };
    });
  }

  function makePlan(withAnswers = true) {
    const answersGiven = withAnswers ? given() : [];
    step(`Plan a table: ${goal.slice(0, 40)}`, async () => ({ plan: await api<Plan>("/sheets/build/plan", { method: "POST", json: { goal, answers: answersGiven } }) }));
  }

  function create() {
    if (!plan) return;
    const spec = plan.spec, kbId = plan.spec.outputs.knowledge_base ? kb : null;
    step(`Create ${spec.name}`, async () => ({ sheet: (await api<{ sheet_id: string }>("/sheets/build", { method: "POST", json: { spec, kb_id: kbId, run: true } })).sheet_id }));
  }

  return (
    <div className="space-y-3">
      <button className="btn-ghost -ml-2" onClick={onCancel}><ArrowLeft size={15} /> Sheets</button>
      <Card title={<span className="flex items-center gap-2"><Sparkle size={16} className="text-accent-600" />Describe the sheet</span>}>
        <textarea className="input-field min-h-[96px]" aria-label="What should the sheet contain?" value={goal} onChange={(e) => setGoal(e.target.value)}
          placeholder="What rows, which columns, from where, how often" />
        <div className="mt-2 flex flex-wrap gap-1.5">
          {EXAMPLES.map((e) => <button key={e} className="chip max-w-full" title={e} onClick={() => { setGoal(e); reset(); }}><span className="truncate">{e}</span></button>)}
        </div>
        <button className="btn-primary mt-3" disabled={busy || goal.trim().length < 10} onClick={start}>{busy && !plan && !questions ? "Thinking…" : "Plan it"}</button>
      </Card>
      <ErrorNote error={error} onClose={() => setError("")} />

      {questions && (
        <Card title={<span className="flex items-center gap-2"><ChatCircleDots size={16} className="text-accent-600" />A few questions first</span>}>
          <div className="space-y-3">
            {questions.map((q) => (
              <div key={q.id}>
                <label htmlFor={q.id} className="label">{q.question}</label>
                <div className="mb-1.5 flex flex-wrap gap-1.5">
                  {q.options.map((o) => <button key={o} className={answers[q.id] === o ? "chip-active" : "chip"} onClick={() => setAnswers({ ...answers, [q.id]: o })}>{o}</button>)}
                </div>
                <input id={q.id} className="input-field" value={answers[q.id] ?? ""} placeholder="Or type an answer" onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-2">
            <button className="btn-primary" disabled={busy} onClick={() => makePlan(true)}>{busy ? "Planning…" : "Make the plan"}</button>
            <button className="btn-ghost" disabled={busy} onClick={() => makePlan(false)}>Skip</button>
          </div>
        </Card>
      )}

      {plan && (
        <Card title={plan.spec.name.replace(/_/g, " ")} actions={<span className="mono text-text-muted">{plan.model}</span>}>
          {plan.summary && <p className="mb-3 text-sm text-text-secondary">{plan.summary}</p>}
          <ol className="mb-3 list-decimal space-y-1 pl-5 text-sm">{plan.spec.steps.map((s, i) => <li key={i}>{stepText(s)}</li>)}</ol>
          {plan.spec.fields.length > 0 && (
            <div className="mb-3 overflow-x-auto rounded-md border border-line">
              <table className="table"><thead><tr><th>Column</th><th>Type</th><th>What goes in it</th></tr></thead>
                <tbody>{plan.spec.fields.map((f) => <tr key={f.name}><td className="mono">{f.name}</td><td>{f.type}</td><td className="text-text-secondary">{f.description}</td></tr>)}</tbody></table>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
            <label className="flex items-center gap-2">Refresh
              <select className="input-field py-1" value={plan.spec.interval_minutes}
                onChange={(e) => setPlan({ ...plan, spec: { ...plan.spec, interval_minutes: +e.target.value } })}>
                {INTERVALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" className="accent-accent-400" checked={plan.spec.outputs.knowledge_base}
                onChange={(e) => setPlan({ ...plan, spec: { ...plan.spec, outputs: { ...plan.spec.outputs, knowledge_base: e.target.checked } } })} />
              Also keep the pages in a collection
            </label>
            {plan.spec.outputs.knowledge_base && kbs.length > 0 && (
              <select className="input-field py-1" value={kb} onChange={(e) => setKb(e.target.value)} aria-label="Collection">
                {kbs.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
              </select>
            )}
          </div>
          <p className="mt-2 text-xs text-text-muted">{plan.estimate.total_calls} KeiroLabs calls per run, with zero data retention.</p>
          <div className="mt-3 flex gap-2">
            <button className="btn-primary" disabled={busy || (plan.spec.outputs.knowledge_base && !kb)} onClick={create}>{busy ? "Creating…" : "Create sheet and fill it"}</button>
            <button className="btn-ghost" onClick={reset}>Discard</button>
          </div>
        </Card>
      )}
    </div>
  );
}
