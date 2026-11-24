import { Flask, Play, Terminal } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText, Job } from "../../api";
import { Card, ErrorNote, Progress, useKept, usePoll } from "../../ui";

type Run = { run: string; model?: string; preset?: string; trials?: number; saved: boolean; adapter: boolean; size_gb: number };
type St = {
  found: boolean; dir: string; installed: boolean; problem?: string; torch?: string; cuda?: boolean; gpu?: string | null; heretic?: string;
  configs: { id: string; file: string }[]; runs: Run[]; license: string;
};

const PRESET_NOTE: Record<string, string> = {
  default: "Removes refusals", noslop: "Also reduces stock phrases", terse: "Also makes answers shorter", nohumor: "Removes jokes",
  nosyco: "Reduces flattery", emotions: "Adjusts emotional tone",
};

/** Run Heretic on a model from here. It lives in its own folder and environment; this page only starts it and shows what it prints. */
export default function Heretic() {
  const [st, reload] = usePoll<St>("/models/heretic", 4000);
  const [jobs] = usePoll<Job[]>("/jobs?limit=30", 2000);
  const [dir, setDir] = useKept("heretic:dir", "");
  const [model, setModel] = useKept("heretic:model", "Qwen/Qwen3-0.6B");
  const [preset, setPreset] = useKept("heretic:preset", "default");
  const [trials, setTrials] = useKept("heretic:trials", 30);
  const [bnb, setBnb] = useKept("heretic:bnb", true);
  const [strategy, setStrategy] = useKept<"merge" | "adapter">("heretic:strategy", "merge");
  const [logRun, setLogRun] = useState("");
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => { setError(""); setBusy(true); try { await fn(); } catch (e) { setError(errText(e)); } setBusy(false); reload(); };
  const running = (jobs ?? []).filter((j) => (j.title?.startsWith("Heretic ") || j.title === "Set up Heretic" || j.title?.startsWith("Import ")) && (j.status === "running" || j.status === "queued"));
  const showLog = async (run: string) => { setLogRun(run); try { setLines((await api<{ lines: string[] }>(`/models/heretic/log/${run}`)).lines); } catch (e) { setError(errText(e)); } };
  const setupRunning = running.some((j) => j.title === "Set up Heretic");
  const [names, setNames] = useState<Record<string, string>>({});

  return (
    <Card title={<span className="flex items-center gap-2"><Flask size={16} className="text-accent-600" />Heretic</span>}>
      <p className="mb-3 text-sm text-text-secondary">
        Removes a model's refusals by editing its weights, then saves it so you can quantize it and use it here. It runs from its own folder and environment; KeiroLake only starts it.
        Heretic is AGPL-3.0 software and is not bundled. Runs download the original model and need a GPU with enough memory (a 0.6B to 4B model fits 8 GB with 4-bit loading).
      </p>
      {!st ? <div className="skeleton h-16" /> : !st.found ? (
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-0 flex-1"><label htmlFor="hd" className="label">Heretic folder</label>
            <input id="hd" className="input-field mono" value={dir} onChange={(e) => setDir(e.target.value)} placeholder="C:\path\to\heretic (contains pyproject.toml)" spellCheck={false} /></div>
          <button className="btn-primary" disabled={busy || !dir.trim()} onClick={() => act(() => api("/models/heretic/dir", { method: "PUT", json: { dir } }))}>Use this folder</button>
        </div>
      ) : !st.installed ? (
        <div className="space-y-2">
          <p className="text-sm">Folder found: <span className="mono">{st.dir}</span></p>
          {st.problem && <p className="text-xs text-text-muted">{st.problem}</p>}
          <button className="btn-primary" disabled={busy || setupRunning} onClick={() => act(() => api("/models/heretic/setup", { method: "POST" }))}>Set up Heretic</button>
          <span className="ml-2 text-xs text-text-muted">Creates a private environment and installs PyTorch ({"several GB"}). Runs in the background.</span>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-xs text-text-muted">Heretic {st.heretic} · PyTorch {st.torch} · {st.cuda ? `GPU: ${st.gpu}` : "no GPU found, runs will be very slow"}</p>
          <div className="grid gap-3 sm:grid-cols-[1fr_12rem_6rem]">
            <div><label htmlFor="hm" className="label">Model (Hugging Face id or folder)</label>
              <input id="hm" className="input-field mono" value={model} onChange={(e) => setModel(e.target.value)} spellCheck={false} /></div>
            <div><label htmlFor="hp" className="label">Preset</label>
              <select id="hp" className="input-field" value={preset} onChange={(e) => setPreset(e.target.value)}>
                {st.configs.map((c) => <option key={c.id} value={c.id}>{c.id}{PRESET_NOTE[c.id] ? ` · ${PRESET_NOTE[c.id]}` : ""}</option>)}
              </select></div>
            <div><label htmlFor="ht" className="label">Trials</label>
              <input id="ht" type="number" min={1} max={500} className="input-field" value={trials} onChange={(e) => setTrials(Math.max(1, Math.min(500, +e.target.value || 1)))} /></div>
          </div>
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <label className="flex items-center gap-2"><input type="checkbox" checked={bnb} onChange={(e) => setBnb(e.target.checked)} />Load in 4-bit while working (saves memory)</label>
            <label className="flex items-center gap-2">Save as
              <select aria-label="Save as" className="input-field h-8 w-auto py-0" value={strategy} onChange={(e) => setStrategy(e.target.value as "merge" | "adapter")}>
                <option value="merge">Full model</option><option value="adapter">LoRA adapter only</option>
              </select></label>
            <button className="btn-primary" disabled={busy || model.trim().length < 3}
              onClick={() => act(async () => { const r = await api<{ run: string }>("/models/heretic/run", { method: "POST", json: { model: model.trim(), preset, trials, bnb_4bit: bnb, strategy } }); showLog(r.run); })}>
              <Play size={14} weight="fill" /> Run Heretic</button>
          </div>
          <p className="text-xs text-text-muted">More trials search longer for a better result. Each trial takes about a minute on a small model.</p>
        </div>
      )}
      <ErrorNote error={error} onClose={() => setError("")} />
      {running.map((j) => <div key={j.id} className="mt-3"><Progress value={j.progress} label={`${j.title} · ${j.message || "queued"}`} /></div>)}

      {!!st?.runs.length && (
        <>
          <p className="mb-1 mt-5 text-xs font-semibold text-text-muted">Previous runs</p>
          <div className="divide-y divide-line">
            {st.runs.map((r) => (
              <div key={r.run} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <div className="min-w-0 flex-1"><p className="truncate font-semibold mono">{r.model ?? r.run}</p>
                  <p className="text-xs text-text-muted">{r.preset} · {r.trials} trials · {r.saved ? `${r.size_gb} GB saved${r.adapter ? " (adapter)" : ""}` : "no saved model"}</p></div>
                <button className="btn-ghost" onClick={() => showLog(r.run)}><Terminal size={14} /> Log</button>
                {r.saved && !r.adapter && (
                  <>
                    <input aria-label={`Ollama name for ${r.run}`} className="input-field h-8 w-44 py-0 mono" placeholder="name in Ollama" value={names[r.run] ?? ""} onChange={(e) => setNames({ ...names, [r.run]: e.target.value })} />
                    <button className="btn-secondary" disabled={busy || (names[r.run] ?? "").length < 2}
                      onClick={() => act(() => api("/models/heretic/import", { method: "POST", json: { run: r.run, name: names[r.run], quant: "Q4_K_M" } }))}>Import as Q4_K_M</button>
                  </>
                )}
              </div>))}
          </div>
        </>
      )}
      {logRun && (
        <div className="mt-3">
          <div className="flex items-center justify-between"><p className="text-xs font-semibold text-text-muted">Log · {logRun}</p>
            <span><button className="btn-ghost" onClick={() => showLog(logRun)}>Refresh</button><button className="btn-ghost" onClick={() => setLogRun("")}>Close</button></span></div>
          <pre className="well mono mt-1 max-h-64 overflow-auto whitespace-pre-wrap p-3 text-xs" aria-label="Heretic log">{lines.join("\n") || "Nothing yet."}</pre>
        </div>
      )}
    </Card>
  );
}
