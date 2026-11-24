import { useState } from "react";

/** A one-series chart for a query result: bars for categories, a line for dates. No dependencies. */
export type Series = { label: string; value: number }[];

const DATE = /^\d{4}(-\d{2}(-\d{2})?)?/;
const MAX_POINTS = 40;

/** Picks a label column and one number column; null when the result isn't chartable. */
export function chartable(columns: string[], rows: any[][]): { series: Series; x: string; y: string; line: boolean } | null {
  if (rows.length < 2 || rows.length > MAX_POINTS || columns.length < 2) return null;
  const numeric = (i: number) => rows.every((r) => r[i] === null || (typeof r[i] === "number" && Number.isFinite(r[i])));
  const yi = columns.findIndex((_, i) => i > 0 && numeric(i));
  if (yi < 0) return null;
  const xi = columns.findIndex((_, i) => i !== yi && !numeric(i));
  if (xi < 0) return null;
  const series = rows.map((r) => ({ label: String(r[xi] ?? ""), value: Number(r[yi] ?? 0) }));
  return { series, x: columns[xi], y: columns[yi], line: series.every((p) => DATE.test(p.label)) };
}

const fmt = (n: number) => (Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : n.toLocaleString(undefined, { maximumFractionDigits: 2 }));

export function Chart({ series, x, y, line }: { series: Series; x: string; y: string; line: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 220, L = 48, R = 8, T = 12, B = 28;
  const max = Math.max(0, ...series.map((p) => p.value));
  const min = Math.min(0, ...series.map((p) => p.value));
  const span = max - min || 1;
  const step = (W - L - R) / series.length;
  const yOf = (v: number) => T + (H - T - B) * (1 - (v - min) / span);
  const xOf = (i: number) => L + step * i + step / 2;
  const ticks = [min, min + span / 2, max];
  const every = Math.ceil(series.length / 8);
  const peak = series.reduce((b, p, i) => (p.value > series[b].value ? i : b), 0);
  const tip = hover === null ? null : series[hover];

  return (
    <figure className="card relative">
      <figcaption className="mb-2 text-sm font-semibold">{y} by {x}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={`${y} by ${x}`} onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={yOf(t)} y2={yOf(t)} stroke="rgb(var(--line))" strokeWidth={1} />
            <text x={L - 6} y={yOf(t) + 4} textAnchor="end" fontSize={11} fill="rgb(var(--text-muted))">{fmt(t)}</text>
          </g>
        ))}
        {line ? (
          <polyline fill="none" stroke="rgb(var(--accent-600))" strokeWidth={2} strokeLinejoin="round"
            points={series.map((p, i) => `${xOf(i)},${yOf(p.value)}`).join(" ")} />
        ) : series.map((p, i) => {
          const top = yOf(Math.max(p.value, 0)), bottom = yOf(Math.min(p.value, 0));
          const w = Math.max(2, Math.min(28, step - 2));
          return <rect key={i} x={xOf(i) - w / 2} y={top} width={w} height={Math.max(1, bottom - top)} rx={4}
            fill="rgb(var(--accent-600))" opacity={hover === null || hover === i ? 1 : 0.55} />;
        })}
        {line && hover !== null && <circle cx={xOf(hover)} cy={yOf(series[hover].value)} r={4} fill="rgb(var(--accent-600))" stroke="rgb(var(--surface-primary))" strokeWidth={2} />}
        {series.map((p, i) => i % every === 0 && (
          <text key={i} x={xOf(i)} y={H - 8} textAnchor="middle" fontSize={11} fill="rgb(var(--text-muted))">{p.label.length > 10 ? p.label.slice(0, 9) + "…" : p.label}</text>
        ))}
        {!line && <text x={xOf(peak)} y={yOf(series[peak].value) - 4} textAnchor="middle" fontSize={11} fill="rgb(var(--text-secondary))">{fmt(series[peak].value)}</text>}
        {series.map((_, i) => <rect key={i} x={L + step * i} y={T} width={step} height={H - T - B} fill="transparent" onMouseEnter={() => setHover(i)} />)}
      </svg>
      {tip && hover !== null && (
        <div className="pointer-events-none absolute rounded-lg border border-line bg-surface-primary px-2 py-1 text-xs shadow"
          style={{ left: `${(xOf(hover) / W) * 100}%`, top: 36, transform: "translateX(-50%)" }}>
          <span className="text-text-muted">{tip.label}</span> <span className="font-semibold text-text-primary">{fmt(tip.value)}</span>
        </div>
      )}
    </figure>
  );
}
