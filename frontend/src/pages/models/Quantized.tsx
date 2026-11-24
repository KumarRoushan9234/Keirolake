import { ArrowsClockwise, DownloadSimple, Gauge, Stack } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";
import { api, errText, Job } from "../../api";
import { Card, ErrorNote, Progress, useKept, usePoll } from "../../ui";

type Quant = { id: string; bpw: number; note: string };
type Installed = { name: string; size_gb: number; quant: string | null; params: string | null; format: string | null; loaded: boolean };
type Overview = {
  quants: Quant[]; hardware: { vram_gb?: number; ram_gb?: number; gpu?: string };
  installed: Installed[]; runtime: { values: Record<string, string>; options: Record<string, { label: string; choices: string[] }> };
};
type Check = { name: string; available: boolean; size_gb: number | null; estimate?: { weights_gb: number; cache_gb: number; total_gb: number }; fit?: { where: string; label: string }; skipped?: string[] };

/** Download a model at a chosen precision, re-quantize one you have, and tune how Ollama loads them. No model code is ever run. */
export default function Quantized() {
  const [data, reload] = usePoll<Overview>("/models/quant", 8000);
  const [jobs] = usePoll<Job[]>("/jobs?limit=30", 2000);
  const [kind, setKind] = useKept<"ollama" | "hf">("quant:kind", "ollama");
  const [ref, setRef] = useKept("quant:ref", "qwen2.5:7b-instruct");
  const [quant, setQuant] = useKept("quant:q", "Q4_K_M");
  const [ctx, setCtx] = useState(4096);
  const [kv, setKv] = useState("f16");
  const [check, setCheck] = useState<Check | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => { setCheck(null); }, [kind, ref, quant, ctx, kv]);
  const running = useMemo(() => (jobs ?? []).filter((j) => (j.title?.startsWith("Download ") || j.title?.startsWith("Quantize ")) && (j.status === "running" || j.status === "queued")), [jobs]);
  const act = async (fn: () => Promise<unknown>) => { setError(""); setNote(""); setBusy(true); try { await fn(); } catch (e) { setError(errText(e)); } setBusy(false); reload(); };
  const verify = () => act(async () => setCheck(await api<Check>("/models/quant/check", { method: "POST", json: { kind, ref: ref.trim(), quant, ctx, kv } })));
  const download = () => act(async () => { const r = await api<{ name: string }>("/models/quant/pull", { method: "POST", json: { kind, ref: ref.trim(), quant } }); setNote(`Downloading ${r.name}`); });
  const requant = (source: string, q: string) => act(async () => { await api("/models/quant/quantize", { method: "POST", json: { source, quant: q } }); setNote(`Re-quantizing ${source} to ${q}`); });

  const sel = data?.quants.find((q) => q.id === quant);
  return (
    <Card title={<span className="flex items-center gap-2"><Stack size={16} className="text-accent-600" />Quantized models</span>}>
      <p className="mb-3 text-sm text-text-secondary">Fewer bits per weight make a model smaller and faster, with a little less accuracy. Models are read as data (GGUF); nothing from a model repository is executed.</p>
      <div className="grid gap-3 sm:grid-cols-[9rem_1fr_11rem]">
        <div><label htmlFor="qk" className="label">Source</label>
          <select id="qk" className="input-field" value={kind} onChange={(e) => setKind(e.target.value as "ollama" | "hf")}>
            <option value="ollama">Ollama library</option><option value="hf">Hugging Face (GGUF)</option>
          </select></div>
        <div><label htmlFor="qr" className="label">{kind === "ollama" ? "Model (name:size)" : "Repository (owner/name)"}</label>
          <input id="qr" className="input-field mono" value={ref} onChange={(e) => setRef(e.target.value)} spellCheck={false} placeholder={kind === "ollama" ? "qwen2.5:7b-instruct" : "bartowski/Qwen2.5-7B-Instruct-GGUF"} /></div>
        <div><label htmlFor="qq" className="label">Quantization</label>
          <select id="qq" className="input-field" value={quant} onChange={(e) => setQuant(e.target.value)}>
            {(data?.quants ?? [{ id: "Q4_K_M", bpw: 4.85, note: "" }]).map((q) => <option key={q.id} value={q.id}>{q.id} · {q.bpw} bits</option>)}
          </select></div>
      </div>
      {sel && <p className="mt-1 text-xs text-text-muted">{sel.note}</p>}
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div><label htmlFor="qc" className="label">Context</label>
          <select id="qc" className="input-field" value={ctx} onChange={(e) => setCtx(+e.target.value)}>{[2048, 4096, 8192, 16384, 32768].map((n) => <option key={n} value={n}>{n}</option>)}</select></div>
        <div><label htmlFor="qv" className="label">Cache precision</label>
          <select id="qv" className="input-field" value={kv} onChange={(e) => setKv(e.target.value)}>{["f16", "q8_0", "q4_0"].map((n) => <option key={n}>{n}</option>)}</select></div>
        <button className="btn-secondary" disabled={busy || ref.trim().length < 3} onClick={verify}><Gauge size={14} /> Check</button>
        <button className="btn-primary" disabled={busy || ref.trim().length < 3 || (check !== null && !check.available)} onClick={download}><DownloadSimple size={14} /> Download</button>
      </div>
      {check && (
        <div className="well mt-3 space-y-1 p-3 text-sm" role="status">
          <p className="font-semibold">{check.available ? `${check.name} is available${check.size_gb ? ` · ${check.size_gb} GB download` : ""}` : `${check.name} is not available at this precision`}</p>
          {check.estimate && <p className="text-xs text-text-muted">Memory when running: about {check.estimate.total_gb} GB ({check.estimate.weights_gb} weights + {check.estimate.cache_gb} cache) · {check.fit?.label}</p>}
          {!!check.skipped?.length && <p className="text-xs text-text-muted">Not used for safety: {check.skipped.join(", ")}</p>}
        </div>
      )}
      <ErrorNote error={error} onClose={() => setError("")} />
      {note && <p className="mt-2 text-xs text-text-secondary" role="status">{note}. Progress shows below and in Jobs.</p>}
      {running.map((j) => <div key={j.id} className="mt-2"><Progress value={j.progress} label={`${j.title} · ${j.message || "queued"}`} /></div>)}

      <p className="mb-1 mt-5 text-xs font-semibold text-text-muted">Installed, as stored</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-xs text-text-muted"><th className="py-1 pr-3 font-medium">Model</th><th className="pr-3 font-medium">Quantization</th><th className="pr-3 font-medium">Parameters</th><th className="pr-3 font-medium">Size</th><th className="font-medium">Re-quantize to</th></tr></thead>
          <tbody className="divide-y divide-line">
            {!data ? <tr><td colSpan={5}><div className="skeleton h-8" /></td></tr> : data.installed.map((m) => (
              <tr key={m.name}>
                <td className="py-2 pr-3 mono">{m.name}{m.loaded && <span className="badge-ok ml-2">loaded</span>}</td>
                <td className="pr-3">{m.quant ?? "?"}</td><td className="pr-3">{m.params ?? "?"}</td><td className="pr-3">{m.size_gb} GB</td>
                <td><Requant name={m.name} current={m.quant} quants={data.quants} onGo={requant} disabled={busy} /></td>
              </tr>))}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-xs text-text-muted">Re-quantizing needs a high-precision source (F16 or Q8_0); going up in precision from a lower one does not restore quality.</p>

      {data && <Runtime data={data} onSaved={reload} setError={setError} />}
    </Card>
  );
}

function Requant({ name, current, quants, onGo, disabled }: { name: string; current: string | null; quants: Quant[]; onGo: (s: string, q: string) => void; disabled: boolean }) {
  const [q, setQ] = useState("Q4_K_M");
  return (
    <span className="flex items-center gap-1">
      <select aria-label={`Target precision for ${name}`} className="input-field h-8 py-0" value={q} onChange={(e) => setQ(e.target.value)}>
        {quants.filter((x) => x.id !== "F16" && x.id !== current).map((x) => <option key={x.id}>{x.id}</option>)}
      </select>
      <button className="icon-btn" aria-label={`Re-quantize ${name} to ${q}`} disabled={disabled} onClick={() => onGo(name, q)}><ArrowsClockwise size={14} /></button>
    </span>
  );
}

function Runtime({ data, onSaved, setError }: { data: Overview; onSaved: () => void; setError: (s: string) => void }) {
  const [vals, setVals] = useState(data.runtime.values);
  const [saved, setSaved] = useState("");
  const dirty = JSON.stringify(vals) !== JSON.stringify(data.runtime.values);
  const save = async (restart: boolean) => {
    setError(""); setSaved("");
    try {
      let force = false;
      if (restart) {
        try { await api("/models/runtime", { method: "PUT", json: { values: vals, restart: true } }); setSaved("Saved and Ollama restarted."); onSaved(); return; }
        catch (e) { if (!/external|started outside|force/i.test(errText(e))) throw e; if (!confirm("Ollama was started outside KeiroLake. Restart it anyway?")) return; force = true; }
      }
      await api("/models/runtime", { method: "PUT", json: { values: vals, restart, force } });
      setSaved(restart ? "Saved and Ollama restarted." : "Saved. Applies the next time Ollama starts.");
      onSaved();
    } catch (e) { setError(errText(e)); }
  };
  return (
    <div className="mt-5">
      <p className="mb-2 text-xs font-semibold text-text-muted">How models load</p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(data.runtime.options).map(([key, o]) => (
          <div key={key}><label htmlFor={key} className="label">{o.label}</label>
            <select id={key} className="input-field" value={vals[key] ?? ""} onChange={(e) => setVals({ ...vals, [key]: e.target.value })}>
              {o.choices.map((c) => <option key={c} value={c}>{c === "" ? "Default" : c === "-1" ? "Always" : c}</option>)}
            </select></div>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button className="btn-secondary" disabled={!dirty} onClick={() => save(false)}>Save</button>
        <button className="btn-primary" disabled={!dirty} onClick={() => save(true)}>Save and restart Ollama</button>
        {saved && <span className="text-xs text-text-secondary" role="status">{saved}</span>}
      </div>
    </div>
  );
}
