import { ArrowClockwise, MagnifyingGlass } from "@phosphor-icons/react";
import { Fragment, useMemo, useState } from "react";
import { Card, Empty, PageHeader, Progress, useKept, usePoll } from "../ui";

type Run = { id: string; kind: string; title: string; status: string; progress: number; message: string; result: Record<string, any>;
  created_at: string; finished_at: string | null };

const STATUS = [["all", "All"], ["running", "Running"], ["done", "Done"], ["failed", "Failed"]] as const;
const pill = (s: string) => (s === "done" ? "badge-ok" : s === "failed" ? "badge-bad" : s === "running" ? "badge-warn" : "badge-muted");

function duration(r: Run) {
  const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now();
  const s = Math.max(0, Math.round((end - new Date(r.created_at).getTime()) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** Every build, refresh, research, import and download, like Docker Desktop's Builds. */
export default function Runs() {
  const [runs, reload] = usePoll<Run[]>("/jobs?limit=200", 3000);
  const [status, setStatus] = useKept("runs:status", "all");
  const [q, setQ] = useKept("runs:q", "");
  const [open, setOpen] = useState<string | null>(null);
  const shown = useMemo(() => (runs ?? []).filter((r) =>
    (status === "all" || (status === "running" ? ["running", "queued"].includes(r.status) : r.status === status)) &&
    (!q || `${r.title} ${r.kind} ${r.message}`.toLowerCase().includes(q.toLowerCase()))), [runs, status, q]);

  return (
    <div className="space-y-4">
      <PageHeader title="Jobs">
        <button className="btn-ghost" onClick={reload}><ArrowClockwise size={15} /> Refresh</button>
      </PageHeader>
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative min-w-0 flex-1 sm:max-w-sm">
          <MagnifyingGlass size={15} className="pointer-events-none absolute left-2.5 top-2.5 text-text-muted" />
          <input className="input-field pl-8" placeholder="Search runs" aria-label="Search runs" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <div className="seg" role="group" aria-label="Status">
          {STATUS.map(([v, l]) => <button key={v} aria-pressed={status === v} onClick={() => setStatus(v)}>{l}</button>)}
        </div>
      </div>
      <Card className="overflow-hidden p-0">
        {!runs ? <div className="skeleton m-4 h-40" /> : !shown.length ? <Empty title="No runs" /> : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead><tr><th>Name</th><th>Kind</th><th>Status</th><th>Started</th><th>Duration</th></tr></thead>
              <tbody>
                {shown.map((r) => (
                  <Fragment key={r.id}>
                    <tr className="cursor-pointer" onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id}>
                      <td className="max-w-md">
                        <span className="block truncate font-medium">{r.title}</span>
                        {["running", "queued"].includes(r.status) ? <div className="mt-1 max-w-xs"><Progress value={r.progress} label={r.message || "Queued"} /></div>
                          : r.message && <span className={`block truncate text-xs ${r.status === "failed" ? "text-coral-500" : "text-text-muted"}`}>{r.message}</span>}
                      </td>
                      <td className="mono text-text-muted">{r.kind}</td>
                      <td><span className={pill(r.status)}>{r.status}</span></td>
                      <td className="whitespace-nowrap text-text-muted">{new Date(r.created_at).toLocaleString()}</td>
                      <td className="mono text-text-muted">{duration(r)}</td>
                    </tr>
                    {open === r.id && (
                      <tr><td colSpan={5} className="bg-surface-secondary">
                        <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify({ id: r.id, message: r.message, result: r.result }, null, 2)}</pre>
                      </td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
