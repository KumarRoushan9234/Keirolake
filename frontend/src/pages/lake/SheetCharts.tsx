import { useEffect, useMemo, useState } from "react";
import { api, errText } from "../../api";
import { Chart, Series } from "../../Chart";
import { ErrorNote, Empty, useKept } from "../../ui";
import { Sheet } from "./types";

type Result = { series: Series; x: string; y: string; agg: string; total: number; kind: "group" | "hist"; line?: boolean };
const AGGS = [["count", "Number of rows"], ["sum", "Sum"], ["avg", "Average"], ["min", "Smallest"], ["max", "Largest"]] as const;

/** Chart any column of the whole sheet, computed by the engine so large sheets are never sampled. */
export default function SheetCharts({ sheet }: { sheet: Sheet }) {
  const numbers = sheet.columns.filter((c) => c.type === "number");
  const [x, setX] = useKept(`chart:${sheet.id}:x`, sheet.columns.find((c) => c.type !== "number" && c.type !== "url")?.name ?? sheet.columns[0]?.name ?? "");
  const [agg, setAgg] = useKept(`chart:${sheet.id}:agg`, "count");
  const [y, setY] = useKept(`chart:${sheet.id}:y`, numbers[0]?.name ?? "");
  const [shape, setShape] = useKept<"bars" | "line">(`chart:${sheet.id}:shape`, "bars");
  const [res, setRes] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const xCol = sheet.columns.find((c) => c.name === x);
  const histogram = xCol?.type === "number" && agg === "count";

  useEffect(() => {
    if (!x) return;
    let live = true;
    const q = new URLSearchParams({ x, agg, ...(agg !== "count" && y ? { y } : {}), ...(histogram ? { bins: "12" } : {}) });
    api<Result>(`/sheets/${sheet.id}/chart?${q}`).then((r) => { if (live) { setRes(r); setError(""); } }).catch((e) => { if (live) { setRes(null); setError(errText(e)); } });
    return () => { live = false; };
  }, [sheet.id, x, y, agg, histogram, sheet.rows]);

  const line = useMemo(() => (shape === "line" ? true : res?.kind === "hist" ? false : false), [shape, res]);
  if (!sheet.columns.length || !sheet.rows) return <Empty title="Nothing to chart yet">Add rows to this sheet first.</Empty>;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div><label htmlFor="cx" className="label">Group by</label>
          <select id="cx" className="input-field" value={x} onChange={(e) => setX(e.target.value)}>{sheet.columns.map((c) => <option key={c.name}>{c.name}</option>)}</select></div>
        <div><label htmlFor="ca" className="label">Show</label>
          <select id="ca" className="input-field" value={agg} onChange={(e) => setAgg(e.target.value)}>
            {AGGS.filter(([a]) => a === "count" || numbers.length).map(([a, l]) => <option key={a} value={a}>{l}</option>)}</select></div>
        {agg !== "count" && (
          <div><label htmlFor="cy" className="label">Of column</label>
            <select id="cy" className="input-field" value={y} onChange={(e) => setY(e.target.value)}>{numbers.map((c) => <option key={c.name}>{c.name}</option>)}</select></div>
        )}
        <div><label htmlFor="cs" className="label">Style</label>
          <select id="cs" className="input-field" value={shape} onChange={(e) => setShape(e.target.value as "bars" | "line")}><option value="bars">Bars</option><option value="line">Line</option></select></div>
      </div>
      <ErrorNote error={error} />
      {res && res.series.length >= 1 && <Chart series={res.series} x={res.x} y={res.y} line={line || !!res.line && shape !== "bars"} />}
      {res && !res.series.length && <p className="text-sm text-text-secondary">That column has no values to chart.</p>}
      {res && <p className="text-xs text-text-muted">{res.kind === "hist" ? "Values split into 12 equal ranges" : `Top ${res.series.length} groups`} across all {res.total.toLocaleString()} rows.</p>}
    </div>
  );
}
