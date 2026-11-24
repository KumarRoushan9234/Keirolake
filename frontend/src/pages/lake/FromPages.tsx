import { ArrowLeft, Link } from "@phosphor-icons/react";
import { useEffect } from "react";
import { api } from "../../api";
import { clearTask, runTask, useTask } from "../../background";
import { Card, ErrorNote, useKept } from "../../ui";

/** Paste page addresses and a question: each page becomes a row, with columns chosen for the question. */
export default function FromPages({ onDone, onCancel }: { onDone: (sheetId?: string) => void; onCancel: () => void }) {
  const [urls, setUrls] = useKept("frompages:urls", "");
  const [question, setQuestion] = useKept("frompages:q", "");
  const task = useTask<string>("frompages");  // reading the pages goes on if you leave the page
  const busy = task.status === "running";
  const error = task.error ?? "";
  const setError = (_: string) => clearTask("frompages");
  useEffect(() => {
    if (task.status === "done" && task.result) { const id = task.result; clearTask("frompages"); setUrls(""); setQuestion(""); onDone(id); }
  }, [task.status]);
  const list = urls.split(/\s+/).filter((u) => /^https?:\/\//i.test(u));
  const create = () => runTask("frompages", `Table from ${list.length} page${list.length === 1 ? "" : "s"}`,
    async () => (await api<{ id: string }>("/sheets/from-urls", { method: "POST", json: { urls: list, question } })).id, { page: "Tables" });
  return (
    <div className="space-y-3">
      <button className="btn-ghost -ml-2" onClick={onCancel}><ArrowLeft size={15} /> Sheets</button>
      <Card title={<span className="flex items-center gap-2"><Link size={16} className="text-accent-600" />From web pages</span>}>
        <label htmlFor="urls" className="label">Page addresses, one per line</label>
        <textarea id="urls" className="input-field mono min-h-[110px]" value={urls} onChange={(e) => setUrls(e.target.value)} placeholder="https://…" spellCheck={false} />
        <label htmlFor="q" className="label mt-3">What should the sheet capture?</label>
        <input id="q" className="input-field" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Price, range and launch date of each scooter" />
        <p className="mt-2 text-xs text-text-muted">{list.length} page{list.length === 1 ? "" : "s"} · read with KeiroLabs, zero data retention · up to 15</p>
        <button className="btn-primary mt-3" disabled={busy || !list.length || question.trim().length < 3} onClick={create}>{busy ? "Reading pages…" : "Make the sheet"}</button>
      </Card>
      <ErrorNote error={error} onClose={() => setError("")} />
    </div>
  );
}
