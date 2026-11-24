import { FloppyDisk, Play, Trash } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText } from "../../api";
import { Chart, chartable } from "../../Chart";
import { Card, Empty, ErrorNote, useKept, usePoll } from "../../ui";

type Result = { columns: string[]; rows: unknown[][]; row_count: number; truncated: boolean; elapsed_ms: number };
type View = { name: string; sql: string; description: string };

/** Run read-only SQL over every table, chart the result, and save it as a view apps can read at /v1/views/<name>. */
export function Query() {
  const [sql, setSql] = useKept("query:sql", "");
  const [res, setRes] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [schema] = usePoll<{ table: string; rows: number; columns: { name: string }[] }[]>("/warehouse/schema", 60000);
  const run = async () => { setError(""); setNote(""); try { setRes(await api<Result>("/warehouse/query", { method: "POST", json: { sql, limit: 1000 } })); } catch (e) { setError(errText(e)); setRes(null); } };
  const save = async () => { setError(""); try { await api("/views", { method: "POST", json: { name, sql } }); setNote(`Saved. Apps read it at /v1/views/${name.toLowerCase()}`); setName(""); } catch (e) { setError(errText(e)); } };
  const chart = res ? chartable(res.columns, res.rows as never[][]) : null;
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_16rem]">
      <div className="min-w-0 space-y-3">
        <textarea aria-label="SQL" className="input-field mono min-h-[120px]" value={sql} onChange={(e) => setSql(e.target.value)} spellCheck={false}
          placeholder={'SELECT state, COUNT(*) FROM "ds_cities" GROUP BY state'} onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") run(); }} />
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-primary" disabled={!sql.trim()} onClick={run}><Play size={14} weight="fill" /> Run</button>
          <span className="text-xs text-text-muted">Ctrl Enter · read-only</span>
          <span className="flex-1" />
          {res && <>
            <input aria-label="View name" className="input-field h-8 w-44 py-0 mono" placeholder="view_name" value={name} onChange={(e) => setName(e.target.value)} />
            <button className="btn-secondary" disabled={name.length < 2} onClick={save}><FloppyDisk size={14} /> Save as view</button>
          </>}
        </div>
        <ErrorNote error={error} onClose={() => setError("")} />
        {note && <p className="field-ok" role="status">{note}</p>}
        {res && (
          <>
            {chart && <Chart {...chart} />}
            <div className="card overflow-x-auto p-0">
              <table className="table">
                <thead><tr>{res.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>{res.rows.slice(0, 300).map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className="mono">{v === null ? <span className="text-text-muted">null</span> : String(v)}</td>)}</tr>)}</tbody>
              </table>
            </div>
            <p className="text-xs text-text-muted">{res.row_count.toLocaleString()} rows{res.truncated ? " (first 1,000)" : ""} · {res.elapsed_ms} ms</p>
          </>
        )}
      </div>
      <Card title="Tables">
        <ul className="max-h-[28rem] space-y-1 overflow-y-auto text-sm">
          {schema?.map((t) => (
            <li key={t.table}><button className="mono w-full truncate text-left text-accent-600 hover:underline" title={t.columns.map((c) => c.name).join(", ")}
              onClick={() => setSql(`SELECT * FROM "${t.table}" LIMIT 100`)}>{t.table}</button><span className="text-xs text-text-muted"> {t.rows.toLocaleString()} rows</span></li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export function Views() {
  const [views, reload] = usePoll<View[]>("/views", 10000);
  const [error, setError] = useState("");
  if (views && !views.length) return <Empty title="No saved views">Run a query on the Query tab and save it as a view.</Empty>;
  return (
    <div className="space-y-2">
      <ErrorNote error={error} onClose={() => setError("")} />
      {views?.map((v) => (
        <div key={v.name} className="card flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1"><p className="mono font-semibold">{v.name}</p><p className="mono mt-1 truncate text-xs text-text-muted" title={v.sql}>{v.sql}</p>
            <p className="mt-1 text-xs text-text-secondary">GET /v1/views/{v.name} · JSON, CSV or JSONL</p></div>
          <button className="icon-btn" aria-label={`Delete view ${v.name}`} onClick={async () => { if (!confirm(`Delete the view ${v.name}?`)) return; try { await api(`/views/${v.name}`, { method: "DELETE" }); } catch (e) { setError(errText(e)); } reload(); }}><Trash size={15} /></button>
        </div>
      ))}
    </div>
  );
}
