import { Broom } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { api, errText } from "../../api";
import { clearTask, runTask, useTask } from "../../background";
import { ErrorNote } from "../../ui";
import { Sheet } from "./types";

type Report = {
  rows_before: number; rows_after: number; trimmed_cells: number; emptied_cells: number; duplicates_removed: number; empty_rows_removed: number;
  retyped: { name: string; to: string }[]; empty_columns: string[]; dropped_columns: string[];
};

/** Tidy a sheet: see exactly what would change, then apply. A dated copy goes to History first. */
export default function CleanTab({ sheet, onChange }: { sheet: Sheet; onChange: () => void }) {
  const [plan, setPlan] = useState<Report | null>(null);
  const [done, setDone] = useState<Report | null>(null);
  const [dropEmpty, setDropEmpty] = useState(false);
  const task = useTask<Report>(`clean:${sheet.id}`);  // cleaning a big sheet goes on if you leave the page
  const busy = task.status === "running";
  const [lookError, setError] = useState("");
  const error = lookError || task.error || "";
  const look = () => { setPlan(null); api<Report>(`/sheets/${sheet.id}/clean`, { method: "POST", json: { apply: false, drop_empty_columns: dropEmpty } }).then(setPlan).catch((e) => setError(errText(e))); };
  useEffect(look, [sheet.id, sheet.rows, dropEmpty]);
  useEffect(() => {
    if (task.status === "done" && task.result) { setDone(task.result); clearTask(`clean:${sheet.id}`); onChange(); }
  }, [task.status]);
  const apply = () => { setError(""); runTask(`clean:${sheet.id}`, `Clean ${sheet.name}`,
    () => api<Report>(`/sheets/${sheet.id}/clean`, { method: "POST", json: { apply: true, drop_empty_columns: dropEmpty } }), { page: "Tables", params: { sheet: sheet.id } }); };
  const nothing = plan && !plan.trimmed_cells && !plan.emptied_cells && !plan.duplicates_removed && !plan.empty_rows_removed && !plan.retyped.length && !plan.empty_columns.length;
  const rows: [string, number | string][] = plan ? [
    ["Extra spaces removed", plan.trimmed_cells], ["Placeholders (N/A, -, null) emptied", plan.emptied_cells], ["Duplicate rows removed", plan.duplicates_removed],
    ["Blank rows removed", plan.empty_rows_removed], ["Columns turned into numbers or dates", plan.retyped.map((r) => `${r.name} → ${r.to}`).join(", ") || 0],
    ["Columns with nothing in them", plan.empty_columns.join(", ") || 0],
  ] : [];
  return (
    <div className="max-w-2xl space-y-3">
      <p className="flex items-center gap-2 text-sm text-text-secondary"><Broom size={16} className="text-accent-600" />Cleaning never invents values. Text that does not clearly read as a number or date is left alone.</p>
      <ErrorNote error={error} onClose={() => { setError(""); clearTask(`clean:${sheet.id}`); }} />
      {!plan ? <div className="skeleton h-40" /> : (
        <div className="card">
          <p className="mb-2 text-sm font-semibold">{nothing ? "This sheet is already clean." : `${plan.rows_before.toLocaleString()} rows now, ${plan.rows_after.toLocaleString()} after cleaning`}</p>
          <dl className="divide-y divide-line text-sm">
            {rows.map(([label, v]) => <div key={label} className="flex justify-between gap-3 py-1.5"><dt className="text-text-secondary">{label}</dt><dd className={v ? "font-semibold" : "text-text-muted"}>{v || "None"}</dd></div>)}
          </dl>
          {!!plan.empty_columns.length && <label className="mt-3 flex items-center gap-2 text-sm"><input type="checkbox" checked={dropEmpty} onChange={(e) => setDropEmpty(e.target.checked)} />Also remove the empty columns</label>}
          <button className="btn-primary mt-4" disabled={busy || !!nothing} onClick={apply}>{busy ? "Cleaning…" : "Clean this sheet"}</button>
          {done && <p className="field-ok mt-3" role="status">Done. {done.rows_after.toLocaleString()} rows kept. The earlier version is in History.</p>}
        </div>
      )}
    </div>
  );
}
