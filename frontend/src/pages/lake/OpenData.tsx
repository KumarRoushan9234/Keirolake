import { ArrowLeft, CheckCircle, DownloadSimple } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText } from "../../api";
import { Card, ErrorNote, Progress, useJobRunner, usePoll } from "../../ui";

type Item = { id: string; title: string; publisher: string; category: string; description: string; size_mb: number; license: string; attribution: string; added: boolean };

/** Public datasets you can add as sheets. Nothing is downloaded until you press Add; the licence and credit stay with the sheet. */
export default function OpenData({ onDone, onCancel }: { onDone: (sheetId?: string) => void; onCancel: () => void }) {
  const [items, reload] = usePoll<Item[]>("/opendata", 60000);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const job = useJobRunner(reload, "opendata");
  const add = async (it: Item) => {
    setError(""); setBusy(it.id);
    const ok = await job.run(() => api(`/opendata/${it.id}/import`, { method: "POST" }));
    setBusy("");
    if (ok) {
      const jobs = await api<any[]>("/jobs?limit=5").catch(() => []);
      const j = jobs.find((x) => x.title?.includes(it.title));
      onDone(j?.result?.sheet_id);
    }
  };
  const groups = [...new Set((items ?? []).map((i) => i.category))];
  return (
    <div className="space-y-3">
      <button className="btn-ghost -ml-2" onClick={onCancel}><ArrowLeft size={15} /> Sheets</button>
      <ErrorNote error={error || job.error} onClose={() => setError("")} />
      {job.job && <Progress value={job.job.progress} label={job.job.message || "Starting"} />}
      {groups.map((g) => (
        <Card key={g} title={g} className="overflow-hidden p-0">
          <div className="divide-y divide-line">
            {items!.filter((i) => i.category === g).map((it) => (
              <div key={it.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{it.title}</p>
                  <p className="text-xs text-text-muted">{it.description}</p>
                  <p className="mono mt-0.5 text-text-muted">{it.publisher} · {it.license} · {it.size_mb} MB</p>
                </div>
                {it.added ? <span className="badge-ok flex items-center gap-1"><CheckCircle size={12} /> Added</span>
                  : <button className="btn-secondary" disabled={!!busy} onClick={() => add(it)}><DownloadSimple size={15} /> {busy === it.id ? "Adding…" : "Add"}</button>}
              </div>
            ))}
          </div>
        </Card>
      ))}
      {items && <p className="text-xs text-text-muted">Check each licence before you share a sheet: some require credit, and one asks that derived data stay open. Offline Wikipedia and other reference libraries are under Lake → Libraries.</p>}
    </div>
  );
}
