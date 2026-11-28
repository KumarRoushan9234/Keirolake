import { CheckCircle, Cpu, DownloadSimple, HardDrives, Lightning, Play, Trash } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";
import { api, errText, Job } from "../api";
import Heretic from "./models/Heretic";
import Quantized from "./models/Quantized";
import Slots from "./models/Slots";
import { Card, ErrorNote, PageHeader, Progress, usePoll } from "../ui";

type Model = { id: string; label: string; size_gb: number; need_gb?: number; note: string; dim?: number };

const CLOUD = [
  { key: "OPENROUTER", label: "OpenRouter" },
  { key: "GROQCLOUD", label: "Groq" },
  { key: "DEEPSEEK", label: "DeepSeek" },
  { key: "GEMINI", label: "Gemini" },
  { key: "ANTHROPIC", label: "Anthropic" },
  { key: "OPENAI", label: "OpenAI" },
];

export default function Models() {
  const [hw] = usePoll<any>("/local/hardware", 60000);
  const [status, reloadStatus] = usePoll<any>("/local/status", 5000);
  const [catalog, setCatalog] = useState<{ llms: Model[]; embedders: Model[] } | null>(null);
  const [jobs] = usePoll<Job[]>("/jobs?limit=30", 2000);
  const [error, setError] = useState("");
  useEffect(() => { api("/local/catalog").then(setCatalog); }, []);

  const pulls = useMemo(() => (jobs ?? []).filter((j) => j.title?.startsWith("Download ") && (j.status === "running" || j.status === "queued")), [jobs]);
  const pullFor = (id: string) => pulls.find((j) => j.title === `Download ${id}`);
  const ollama = status?.ollama;
  const installed = new Set<string>((ollama?.models ?? []).filter((m: any) => m.local).map((m: any) => m.name.replace(/:latest$/, "")));
  const has = (id: string) => installed.has(id) || installed.has(id.replace(/:latest$/, ""));
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reloadStatus(); };
  const pull = (model: string) => act(() => api("/local/ollama/pull", { method: "POST", json: { model } }));
  const use = (model: string) => act(() => api("/local/use", { method: "POST", json: { model } }));
  const setPref = (values: Record<string, string>) => act(() => api("/settings", { method: "PUT", json: { values } }));

  const h = hw?.hardware;
  const rec = hw?.recommendation;

  return (
    <div className="space-y-4">
      <PageHeader title="Models" />
      <Usage />
      <ErrorNote error={error} onClose={() => setError("")} />
      <Slots />

      <Card title={<span className="flex items-center gap-2"><Cpu size={16} className="text-accent-400" />This computer</span>}>
        {!h ? <div className="skeleton h-16" /> : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Processor" value={h.cpu} sub={`${h.cores} cores · ${h.threads} threads`} />
            <Stat label="Memory" value={`${h.ram_gb} GB`} sub={`${h.ram_free_gb} GB free`} />
            <Stat label="Graphics" value={h.gpus.length ? h.gpus.map((g: any) => g.name).join(", ") : "No dedicated GPU"} sub={h.vram_gb ? `${h.vram_gb} GB VRAM` : "Models run on the CPU"} />
            <Stat label="Disk" value={`${h.disk_free_gb} GB free`} sub={`of ${h.disk_total_gb} GB`} />
          </div>
        )}
        {rec && (
          <div className="well mt-4 space-y-2 p-4 text-sm">
            <p className="font-semibold">Recommended</p>
            <Row icon={<Lightning size={14} />} title={`${rec.llm.label}`} sub={`${rec.llm.size_gb} GB · ${rec.llm.note}`}>
              <ModelAction loading={!status} id={rec.llm.id} has={has(rec.llm.id)} active={status?.local_model === rec.llm.id} job={pullFor(rec.llm.id)} pull={pull} use={use} />
            </Row>
            {rec.llm_fast && (
              <Row icon={<Lightning size={14} />} title={`${rec.llm_fast.label} (faster)`} sub={`${rec.llm_fast.size_gb} GB · ${rec.llm_fast.note}`}>
                <ModelAction loading={!status} id={rec.llm_fast.id} has={has(rec.llm_fast.id)} active={status?.local_model === rec.llm_fast.id} job={pullFor(rec.llm_fast.id)} pull={pull} use={use} />
              </Row>
            )}
            <Row icon={<HardDrives size={14} />} title={`${rec.embedder.label} for offline search`} sub={`${rec.embedder.size_gb} GB · ${rec.embedder.note}`}>
              <EmbedAction loading={!status} id={rec.embedder.id} has={has(rec.embedder.id)} active={status?.wiki_embed_model === rec.embedder.id}
                job={pullFor(rec.embedder.id)} pull={pull} use={(m) => setPref({ WIKI_EMBED_MODEL: m })} />
            </Row>
            <p className="text-xs text-text-muted">
              {rec.wikipedia ? `${rec.wikipedia.label}, ${rec.wikipedia.size_gb} GB, fits this drive. ` : ""}{rec.index_note}. {rec.reasons.join(". ")}.
            </p>
          </div>
        )}
      </Card>

      <Card title="Local runtime" actions={ollama && (
        ollama.running ? <span className="badge-ok">Ollama {ollama.version} running</span>
          : ollama.installed ? <button className="btn-primary" onClick={() => act(() => api("/local/ollama/start", { method: "POST" }))}><Play size={14} weight="fill" /> Start Ollama</button>
            : <a className="btn-primary" href={ollama.install_url} target="_blank" rel="noreferrer">Install Ollama</a>
      )}>
        <p className="mb-3 text-sm text-text-secondary">
          Answers offline with <b>{status?.local_model || "no model chosen"}</b>
          {status?.mode === "cloud" ? " (Cloud mode: local models are not used)" : ""}.
        </p>
        <div className="divide-y divide-line">
          {catalog?.llms.map((m) => (
            <Row key={m.id} title={m.label} sub={`${m.size_gb} GB · needs ~${m.need_gb} GB ${h?.vram_gb ? "VRAM" : "RAM"} · ${m.note}`}>
              <ModelAction loading={!status} id={m.id} has={has(m.id)} active={status?.local_model === m.id} job={pullFor(m.id)} pull={pull} use={use}
                remove={() => confirm(`Delete ${m.id}?`) && act(() => api(`/local/ollama/models/${m.id}`, { method: "DELETE" }))} />
            </Row>
          ))}
          {(ollama?.models ?? []).filter((m: any) => m.local && !catalog?.llms.some((c) => c.id === m.name.replace(/:latest$/, "")) && !catalog?.embedders.some((c) => m.name.startsWith(c.id))).map((m: any) => (
            <Row key={m.name} title={m.name} sub={`${m.size_gb} GB · installed`}>
              <ModelAction loading={!status} id={m.name} has active={status?.local_model === m.name} pull={pull} use={use}
                remove={() => confirm(`Delete ${m.name}?`) && act(() => api(`/local/ollama/models/${m.name}`, { method: "DELETE" }))} />
            </Row>
          ))}
        </div>
        <p className="mb-1 mt-4 text-xs font-semibold text-text-muted">Embedding models (offline search)</p>
        <div className="divide-y divide-line">
          {catalog?.embedders.map((m) => (
            <Row key={m.id} title={m.label} sub={`${m.size_gb} GB · ${m.note}`}>
              <EmbedAction loading={!status} id={m.id} has={has(m.id)} active={status?.wiki_embed_model === m.id} job={pullFor(m.id)} pull={pull}
                use={(id) => setPref({ WIKI_EMBED_MODEL: id })} />
            </Row>
          ))}
        </div>
        <LlamaCpp status={status?.llamacpp} onChange={reloadStatus} setError={setError} />
      </Card>

      <Quantized />

      <Heretic />

      <Cloud />
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="well min-w-0 p-3">
      <p className="text-xs text-text-muted">{label}</p>
      <p className="truncate text-sm font-semibold" title={value}>{value}</p>
      <p className="text-xs text-text-muted">{sub}</p>
    </div>
  );
}

function Row({ icon, title, sub, children }: { icon?: React.ReactNode; title: string; sub: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3 py-2">
      {icon && <span className="text-accent-400">{icon}</span>}
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-semibold">{title}</p>
        <p className="truncate text-xs text-text-muted">{sub}</p>
      </div>
      <div className="flex items-center gap-2">{children}</div>
    </div>
  );
}

function ModelAction({ id, has, active, job, pull, use, remove, loading }: {
  id: string; has: boolean; active: boolean; job?: Job; pull: (m: string) => void; use: (m: string) => void; remove?: () => void; loading?: boolean;
}) {
  if (loading) return <div className="skeleton h-8 w-20" />;
  if (job) return <div className="w-40"><Progress value={job.progress} label={job.message || "Queued"} /></div>;
  if (!has) return <button className="btn-secondary" onClick={() => pull(id)}><DownloadSimple size={14} /> Install</button>;
  return (
    <>
      {active ? <span className="badge-ok"><CheckCircle size={12} className="mr-1" /> In use</span>
        : <button className="btn-secondary" onClick={() => use(id)}>Use</button>}
      {remove && !active && <button className="icon-btn" aria-label={`Delete ${id}`} onClick={remove}><Trash size={14} /></button>}
    </>
  );
}

function EmbedAction({ id, has, active, job, pull, use, loading }: { id: string; has: boolean; active: boolean; job?: Job; pull: (m: string) => void; use: (m: string) => void; loading?: boolean }) {
  if (loading) return <div className="skeleton h-8 w-20" />;
  if (job) return <div className="w-40"><Progress value={job.progress} label={job.message || "Queued"} /></div>;
  if (!has) return <button className="btn-secondary" onClick={() => pull(id)}><DownloadSimple size={14} /> Install</button>;
  return active ? <span className="badge-ok"><CheckCircle size={12} className="mr-1" /> In use</span>
    : <button className="btn-secondary" title="Changing it needs a re-index of libraries" onClick={() => use(id)}>Use</button>;
}

function LlamaCpp({ status, onChange, setError }: { status: any; onChange: () => void; setError: (e: string) => void }) {
  const [open, setOpen] = useState(false);
  const [server, setServer] = useState("");
  const [model, setModel] = useState("");
  const [url, setUrl] = useState("");
  useEffect(() => { if (status) { setServer(status.server ?? ""); setModel(status.model_path ?? ""); } }, [status?.server, status?.model_path]);
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } onChange(); };
  return (
    <div className="mt-4 border-t border-line pt-3">
      <button className="text-xs font-semibold text-text-muted hover:text-text-primary" onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Without Ollama:"} llama.cpp {status?.running ? "· running" : ""}
      </button>
      {open && (
        <div className="mt-2 grid gap-2">
          <input className="input-field" placeholder="Path to llama-server" value={server} onChange={(e) => setServer(e.target.value)} />
          <input className="input-field" placeholder="Path to a .gguf model" value={model} onChange={(e) => setModel(e.target.value)} list="ggufs" />
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" onClick={() => act(() => api("/settings", { method: "PUT", json: { values: { LLAMACPP_SERVER: server, LLAMACPP_MODEL_PATH: model } } }))}>Save</button>
            <button className="btn-ghost" disabled={!server || !model} onClick={() => act(() => api("/local/llamacpp/start", { method: "POST" }))}>Start</button>
          </div>
          <div className="flex gap-2">
            <input className="input-field flex-1" placeholder="https://huggingface.co/…/model.gguf" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn-ghost" disabled={!url} onClick={() => act(async () => { await api("/local/llamacpp/download", { method: "POST", json: { url } }); setUrl(""); })}>Download</button>
          </div>
          {status?.ggufs?.length > 0 && <p className="text-xs text-text-muted">Downloaded: {status.ggufs.join(", ")}</p>}
        </div>
      )}
    </div>
  );
}

function Cloud() {
  const [settings, reload] = usePoll<any>("/settings", 60000);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [probe, setProbe] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const v = settings?.values ?? {};
  const val = (k: string) => edits[k] ?? v[k]?.value ?? "";
  const set = (k: string, value: string) => setEdits((e) => ({ ...e, [k]: value }));

  async function save() {
    setError("");
    try { await api("/settings", { method: "PUT", json: { values: edits } }); setEdits({}); reload(); }
    catch (e) { setError(errText(e)); }
  }
  async function test() {
    setBusy(true);
    try { setProbe(await api("/providers?probe=true")); } catch (e) { setError(errText(e)); }
    setBusy(false);
  }

  return (
    <Card title="Cloud models and web data" actions={<>
      <button className="btn-ghost" disabled={busy} onClick={test}>{busy ? "Testing…" : "Test connections"}</button>
      <button className="btn-primary" disabled={!Object.keys(edits).length} onClick={save}>Save</button>
    </>}>
      <ErrorNote error={error} />
      <div className="grid gap-3">
        <label className="block"><span className="label">Provider order</span>
          <input className="input-field" placeholder="openrouter,groqcloud" value={val("LLM_FALLBACK_ORDER")} onChange={(e) => set("LLM_FALLBACK_ORDER", e.target.value)} /></label>
        {CLOUD.map(({ key, label }) => {
          const secret = v[`${key}_API_KEY`];
          return (
            <div key={key} className="grid gap-2 sm:grid-cols-[8rem_1fr_1fr] sm:items-center">
              <span className="text-sm font-semibold">{label}</span>
              <input className="input-field" type="password" autoComplete="off"
                placeholder={secret?.set ? `Key saved ${secret.hint}` : "API key"} value={edits[`${key}_API_KEY`] ?? ""}
                onChange={(e) => set(`${key}_API_KEY`, e.target.value)} />
              <input className="input-field" placeholder="Models, in order" value={val(`${key}_MODELS`)} onChange={(e) => set(`${key}_MODELS`, e.target.value)} />
            </div>
          );
        })}
        <div className="grid gap-2 sm:grid-cols-[8rem_1fr] sm:items-center">
          <span className="flex items-center gap-1.5 text-sm font-semibold"><img src="/keirolabs.png" alt="" className="h-4 w-4" />KeiroLabs</span>
          <input className="input-field" type="password" autoComplete="off"
            placeholder={v.KEIROLABS_SEARCH_LITE_API_KEY?.set ? `Key saved ${v.KEIROLABS_SEARCH_LITE_API_KEY.hint}` : "API key"}
            value={edits.KEIROLABS_SEARCH_LITE_API_KEY ?? ""} onChange={(e) => set("KEIROLABS_SEARCH_LITE_API_KEY", e.target.value)} />
        </div>
        <p className="text-xs text-text-muted">Keys are kept in your system keychain. Free-tier (":free") models are ignored.</p>
      </div>
      {probe && (
        <div className="mt-4 divide-y divide-line text-sm">
          {[...(probe.llm_probe ?? []), ...(probe.embeddings_probe ?? []).map((e: any) => ({ ...e, provider: "embed" }))].map((r: any) => (
            <div key={r.provider + r.model} className="flex items-center gap-2 py-1.5">
              <span className={r.ok ? "badge-ok" : "badge-bad"}>{r.ok ? "ok" : "failed"}</span>
              <span className="min-w-0 flex-1 truncate">{r.provider} · {r.model}</span>
              <span className="truncate text-xs text-text-muted">{r.ok ? `${r.latency_ms ?? ""} ms` : r.error}</span>
            </div>
          ))}
          {probe.keiro && <p className="py-1.5 text-xs text-text-muted">KeiroLabs: {probe.keiro.ok === false ? probe.keiro.error : `${probe.keiro.credits_remaining?.toLocaleString() ?? "?"} credits`}</p>}
        </div>
      )}
    </Card>
  );
}

/** Local first: what ran where today, the daily cloud limit, and KeiroLabs privacy. */
function Usage() {
  const [s, reload] = usePoll<any>("/settings", 15000);
  if (!s) return null;
  const u = s.usage;
  const set = (values: Record<string, string>) => api("/settings", { method: "PUT", json: { values } }).then(reload);
  const total = u.local_calls + u.cloud_calls;
  return (
    <Card title="Local first">
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <p className="text-2xl font-semibold tabular-nums">{total ? Math.round((u.local_calls / total) * 100) : 100}%</p>
          <p className="text-xs text-text-muted">answered on this computer today ({u.local_calls} local, {u.cloud_calls} cloud)</p>
        </div>
        <div>
          <label className="text-xs text-text-muted" htmlFor="cloud-limit">Cloud calls per day</label>
          <select id="cloud-limit" className="input-field mt-1 w-full" value={String(s.cloud_limit)} onChange={(e) => set({ CLOUD_DAILY_LIMIT: e.target.value })}>
            {["0", "50", "100", "300", "1000"].map((v) => <option key={v} value={v}>{v === "0" ? "No limit" : v}</option>)}
          </select>
          {u.cloud_left !== null && <p className="mt-1 text-xs text-text-muted">{u.cloud_left} left today</p>}
        </div>
        <div className="space-y-2 text-sm">
          <label className="flex items-center justify-between gap-2">KeiroLabs zero retention
            <input type="checkbox" role="switch" className="accent-accent-400" checked={s.zdr.on} onChange={(e) => set({ KEIRO_ZDR: e.target.checked ? "" : "0" })} />
          </label>
          <p className="text-xs text-text-muted">{!s.zdr.on ? "Off" : s.zdr.confirmed ? "Confirmed by KeiroLabs on the last request" : "Asked on every request"}</p>
          <label className="flex items-center justify-between gap-2">Reuse web results for 15 min
            <input type="checkbox" role="switch" className="accent-accent-400" checked={s.keiro_cache} onChange={(e) => set({ KEIRO_CACHE: e.target.checked ? "" : "0" })} />
          </label>
        </div>
      </div>
    </Card>
  );
}
