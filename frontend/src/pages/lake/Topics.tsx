import { ArrowClockwise, ChatCircleText, DownloadSimple, FolderOpen, Plus, Table as TableIcon, Trash } from "@phosphor-icons/react";
import { useState } from "react";
import { api, base64Download, errText, Job } from "../../api";
import { Card, Empty, ErrorNote, Progress, useKept, useNav, usePoll } from "../../ui";

type Topic = {
  id: string; name: string; question: string; urls: string[]; sheet_id: string | null; kb_id: string | null; status: "new" | "building" | "ready" | "error";
  summary: { pages?: number; rows?: number; columns?: number; error?: string; folder?: string; csv?: string; indexed?: Record<string, number>; built_at?: string;
    cleaned?: { duplicates_removed: number; emptied_cells: number; trimmed_cells: number; empty_rows_removed: number; retyped: { name: string; to: string }[] } };
  updated_at: string;
};

const PILL: Record<string, string> = { ready: "badge-ok", building: "badge-muted", error: "badge-bad", new: "badge-muted" };
const LABEL: Record<string, string> = { ready: "Ready", building: "Building", error: "Needs attention", new: "Not built" };

/** A topic holds everything about a subject: a sheet of facts, searchable pages, and a CSV. One box in, all of it out. */
export default function Topics() {
  const { go } = useNav();
  const [topics, reload] = usePoll<Topic[]>("/topics", 3000);
  const [jobs] = usePoll<Job[]>("/jobs?limit=20", 2000);
  const [adding, setAdding] = useKept("topics:adding", false);
  const [name, setName] = useKept("topics:name", "");
  const [question, setQuestion] = useKept("topics:question", "");
  const [urls, setUrls] = useKept("topics:urls", "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const list = urls.split(/\s+/).filter((u) => /^https?:\/\//i.test(u));
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); };
  const jobFor = (t: Topic) => (jobs ?? []).find((j) => j.title === `Topic: ${t.name}` && (j.status === "running" || j.status === "queued"));

  const create = async () => {
    setBusy(true);
    await act(async () => { await api("/topics", { method: "POST", json: { name, question, urls: list } }); setName(""); setQuestion(""); setUrls(""); setAdding(false); });
    setBusy(false);
  };
  const download = (t: Topic) => act(async () => { if (t.sheet_id) await base64Download(`/sheets/${t.sheet_id}/export?format=csv`); });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-text-secondary">Pick a subject and KeiroLake gathers the pages, makes a clean table, and keeps the text searchable.</p>
        {!adding && <button className="btn-primary" onClick={() => setAdding(true)}><Plus size={14} /> New topic</button>}
      </div>
      <ErrorNote error={error} onClose={() => setError("")} />

      {adding && (
        <Card title="New topic">
          <div className="space-y-3">
            <div><label htmlFor="tn" className="label">Name</label><input id="tn" className="input-field" value={name} onChange={(e) => setName(e.target.value)} placeholder="Electric scooters in India" /></div>
            <div><label htmlFor="tq" className="label">What do you want to know? (optional)</label>
              <input id="tq" className="input-field" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Model, price, range and launch date of each scooter" /></div>
            <div><label htmlFor="tu" className="label">Pages to use (optional, one per line)</label>
              <textarea id="tu" className="input-field mono min-h-[72px]" value={urls} onChange={(e) => setUrls(e.target.value)} placeholder="Leave empty and KeiroLake finds them" spellCheck={false} /></div>
            <div className="flex flex-wrap items-center gap-2">
              <button className="btn-primary" disabled={busy || !name.trim()} onClick={create}>{busy ? "Starting…" : "Build topic"}</button>
              <button className="btn-ghost" onClick={() => setAdding(false)}>Cancel</button>
              <span className="text-xs text-text-muted">Uses KeiroLabs with zero data retention. {list.length ? `${list.length} page${list.length === 1 ? "" : "s"} named.` : "Up to 12 pages."}</span>
            </div>
          </div>
        </Card>
      )}

      {topics && !topics.length && !adding && <Empty title="No topics yet">Add one and everything is built for you.</Empty>}
      <div className="grid gap-3 lg:grid-cols-2">
        {topics?.map((t) => {
          const job = jobFor(t);
          const s = t.summary;
          return (
            <Card key={t.id} title={<span className="flex items-center justify-between gap-2"><span className="truncate">{t.name}</span><span className={PILL[t.status]}>{LABEL[t.status]}</span></span>}>
              <p className="text-sm text-text-secondary">{t.question}</p>
              {job && <div className="mt-3"><Progress value={job.progress} label={job.message || "Queued"} /></div>}
              {t.status === "ready" && (
                <dl className="mt-3 grid grid-cols-3 gap-2 text-center">
                  {[[s.pages ?? 0, "pages"], [s.rows ?? 0, "rows"], [s.columns ?? 0, "columns"]].map(([n, l]) => (
                    <div key={l as string} className="well p-2"><dt className="text-xs text-text-muted">{l}</dt><dd className="text-base font-semibold">{n}</dd></div>
                  ))}
                </dl>
              )}
              {t.status === "ready" && s.cleaned && (
                <p className="mt-2 text-xs text-text-muted">Cleaned: {s.cleaned.duplicates_removed} duplicates removed, {s.cleaned.emptied_cells} placeholders emptied{s.cleaned.retyped.length ? `, ${s.cleaned.retyped.map((r) => `${r.name} is now ${r.to}`).join(", ")}` : ""}.</p>
              )}
              {t.status === "error" && <p className="field-error mt-3">{s.error}</p>}
              <div className="mt-3 flex flex-wrap gap-2">
                {t.sheet_id && <button className="btn-secondary" onClick={() => go({ page: "Lake", params: { sheet: t.sheet_id! } })}><TableIcon size={14} /> Open sheet</button>}
                {t.kb_id && <button className="btn-secondary" onClick={() => { try { localStorage.setItem("kb", t.kb_id!); } catch { /* ignore */ } go({ page: "Ask" }); }}><ChatCircleText size={14} /> Ask about it</button>}
                {t.sheet_id && <button className="btn-ghost" onClick={() => download(t)}><DownloadSimple size={14} /> CSV</button>}
                {s.folder && <span className="inline-flex items-center gap-1 text-xs text-text-muted" title={s.folder}><FolderOpen size={13} />Saved in the lake</span>}
                <span className="flex-1" />
                <button className="icon-btn" aria-label={`Rebuild ${t.name}`} disabled={!!job || t.status === "building"} onClick={() => act(() => api(`/topics/${t.id}/build`, { method: "POST" }))}><ArrowClockwise size={15} /></button>
                <button className="icon-btn" aria-label={`Delete ${t.name}`} onClick={() => {
                  if (!confirm(`Delete the topic "${t.name}"?`)) return;
                  const all = !!(t.sheet_id || t.kb_id) && confirm("Also delete its sheet and searchable pages? OK deletes them too. Cancel keeps them.");
                  act(() => api(`/topics/${t.id}?remove_data=${all}`, { method: "DELETE" }));
                }}><Trash size={15} /></button>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
