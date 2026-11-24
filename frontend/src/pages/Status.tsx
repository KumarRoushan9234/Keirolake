import { ArrowClockwise, Export } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText, gb, isDesktop, shell } from "../api";
import { Card, ErrorNote, Progress, useJobRunner, useNav, usePoll } from "../ui";

/** Services, lake size, recent jobs and lineage. Shown on the Help page. */
export default function StatusPanel() {
  const { online } = useNav();
  const [stats] = usePoll<any>("/stats", 10000);
  const [health] = usePoll<any>("/health", 10000);
  const [local] = usePoll<any>("/local/status", 10000);
  const [jobs] = usePoll<any[]>("/jobs?limit=12", 3000);
  const [runs] = usePoll<any[]>("/lineage/runs?limit=12", 15000);
  const exporter = useJobRunner(undefined, "export");
  const [target, setTarget] = useState("");
  const [withLibraries, setWithLibraries] = useState(true);
  const [done, setDone] = useState("");

  const pick = async () => { const dir = await shell<string | null>("pick_folder"); if (dir) setTarget(dir); };
  const exportNow = async () => {
    setDone("");
    if (await exporter.run(() => api("/export", { method: "POST", json: { target, include_libraries: withLibraries } }))) setDone(`Exported to ${target}`);
  };

  const keiro = health?.keiro;
  return (
    <div id="status" className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Services">
          <div className="space-y-2 text-sm">
            <Line label="Internet" ok={online} text={online ? "Online" : "Offline — local models and libraries only"} />
            <Line label="Vector store" ok={health?.qdrant?.ok} text={health?.qdrant?.ok ? `${health.qdrant.collections} collections` : health?.qdrant?.error ?? "…"} />
            <Line label="Local model" ok={local?.ollama?.running} text={!local ? "…" : local.ollama.running ? `Ollama ${local.ollama.version} · ${local.local_model || "no model chosen"}` : local.ollama.installed ? "Ollama stopped" : "Ollama not installed"} />
            <Line label="KeiroLabs" logo ok={keiro?.configured} text={keiro?.configured ? (keiro.credits_remaining != null ? `${keiro.credits_remaining.toLocaleString()} credits` : "Key set") : "No key"} />
          </div>
        </Card>
        <Card title="Lake">
          {stats && (
            <div className="space-y-2 text-sm">
              <p className="text-text-secondary">
                {stats.counts.knowledge_bases} collections · {stats.counts.documents} documents · {stats.counts.chunks} chunks · {stats.counts.datasets} tables
              </p>
              {stats.zones.map((z: any) => (
                <div key={z.zone} className="flex justify-between text-xs"><span className="font-semibold">{z.zone}</span><span className="text-text-muted">{z.files} files · {gb(z.bytes)}</span></div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card title="Copy everything to a drive" actions={<span className="text-xs text-text-muted">Database, vectors, lake files and libraries, for another computer</span>}>
        <div className="flex flex-wrap gap-2">
          <input className="input-field flex-1" placeholder="Folder, e.g. E:\\ or /media/usb" value={target} onChange={(e) => setTarget(e.target.value)} />
          {isDesktop && <button className="btn-ghost" onClick={pick}>Choose…</button>}
          <label className="flex items-center gap-2 text-xs text-text-secondary">
            <input type="checkbox" className="accent-accent-400" checked={withLibraries} onChange={(e) => setWithLibraries(e.target.checked)} /> Include libraries
          </label>
          <button className="btn-primary" disabled={!target.trim() || !!exporter.job} onClick={exportNow}><Export size={16} /> Export</button>
        </div>
        {exporter.job && <div className="mt-3"><Progress value={exporter.job.progress} label={exporter.job.message} /></div>}
        <div className="mt-2"><ErrorNote error={exporter.error} /></div>
        {done && !exporter.error && <p className="field-ok mt-2">{done}</p>}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Recent jobs" actions={<ArrowClockwise size={14} className="text-text-muted" />}>
          <div className="divide-y divide-line text-sm">
            {jobs?.map((j) => (
              <div key={j.id} className="py-1.5">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate">{j.title}</span>
                  <span className={j.status === "failed" ? "badge-bad" : j.status === "done" ? "badge-muted" : "badge-warn"}>{j.status}</span>
                </div>
                {j.status === "running" && <div className="mt-1"><Progress value={j.progress} label={j.message} /></div>}
                {j.status === "failed" && <p className="truncate text-xs text-coral-500" title={j.message}>{j.message}</p>}
              </div>
            ))}
          </div>
        </Card>
        <Card title="Where your data came from">
          <div className="divide-y divide-line text-sm">
            {runs?.map((r) => (
              <div key={r.id} className="flex items-center gap-2 py-1.5" title={r.raw_path}>
                <span className="min-w-0 flex-1 truncate">{r.source.name}</span>
                <span className="text-xs text-text-muted">{r.source.kind} · {r.stats?.documents ?? 0} docs</span>
                {r.status === "failed" && <span className="badge-bad" title={r.error ?? ""}>failed</span>}
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

function Line({ label, ok, text, logo }: { label: string; ok?: boolean; text: string; logo?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`h-2 w-2 shrink-0 rounded-full ${ok ? "bg-sage-400" : ok === false ? "bg-coral-400" : "bg-surface-tertiary"}`} />
      <span className="flex w-28 shrink-0 items-center gap-1.5 text-text-muted">{logo && <img src="/keirolabs.png" alt="" className="h-4 w-4" />}{label}</span>
      <span className="min-w-0 truncate">{text}</span>
    </div>
  );
}
