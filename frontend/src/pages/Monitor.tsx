import { ArrowClockwise, ArrowSquareOut, Bell, ChatCircleText, Newspaper, Trash, UploadSimple } from "@phosphor-icons/react";
import { useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, errText, safeUrl } from "../api";
import { Card, ErrorNote, PageHeader, useNav, usePoll } from "../ui";

type Source = { id: string; group: string; label: string; description: string; attribution: string; url: string; enabled: boolean; interval_min: number;
  last_run: string | null; last_ok: boolean; last_error: string; last_count: number; needs_key: string; key_set: boolean; running: boolean };
type Overview = { sources: Source[]; markets: { provider: string; days: number; first_day: string; last_day: string; symbols: number; indian_symbols: number; watchlist: string[]; finnhub_key: boolean }; alerts_unseen: number };
type Mover = { symbol: string; close: number; base_close: number; pct: number; turnover_cr: number };
type Movers = { ok: boolean; reason?: string; latest?: string; base?: string; days?: number; universe?: number; min_turnover_cr?: number; scope?: string; excluded?: { symbol: string; day: string; change_pct: number }[]; gainers: Mover[]; losers: Mover[]; source?: string };
type Quote = { symbol: string; day: string; close: number; currency: string; change_pct: number | null; range_low: number | null; range_high: number | null };
type Ev = { id: string; source: string; kind: string; title: string; summary: string; url: string; severity: string; country: string; occurred_at: string };
type Alert = { id: string; title: string; body: string; url: string; severity: string; scope: string; seen: boolean; created_at: string };
type Rule = { id: string; kind: string; scope: string; config: Record<string, any>; enabled: boolean };
type Brief = { id: string; title: string; body: string; sources: { n: number; title: string; url: string }[]; model: string; created_at: string };

const TABS = [["markets", "Markets"], ["world", "World"], ["cyber", "Cyber"], ["alerts", "Alerts and brief"]] as const;
const SEV: Record<string, string> = { critical: "badge-bad", high: "badge-bad", medium: "badge-warn", low: "badge-muted", info: "badge-muted" };
const ago = (iso: string) => {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

/** Markets, world events and cyber threats from open sources, kept on this computer. */
export default function Monitor() {
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("markets");
  const [ov, reload] = usePoll<Overview>("/monitor", 8000);
  const [error, setError] = useState("");
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); };
  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <PageHeader title="Monitor">
        <div className="seg" role="tablist" aria-label="Monitor">
          {TABS.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
              {label}{id === "alerts" && ov?.alerts_unseen ? <span className="badge-bad ml-1.5">{ov.alerts_unseen}</span> : null}
            </button>))}
        </div>
      </PageHeader>
      <ErrorNote error={error} onClose={() => setError("")} />
      {tab === "markets" && <Markets ov={ov} act={act} />}
      {tab === "world" && <Feed group="crisis" kinds={[["", "All"], ["quake", "Earthquakes"], ["disaster", "Disasters"], ["outbreak", "Outbreaks"], ["news", "News"]]} ov={ov} act={act} />}
      {tab === "cyber" && <Cyber ov={ov} act={act} />}
      {tab === "alerts" && <Alerts act={act} ov={ov} />}
    </div>
  );
}

function Sources({ ov, group, act }: { ov: Overview | null; group: string; act: (fn: () => Promise<unknown>) => void }) {
  const list = (ov?.sources ?? []).filter((s) => s.group === group);
  return (
    <Card title="Sources">
      <p className="mb-2 text-xs text-text-muted">Nothing is fetched until you switch a source on. Everything is stored on this computer.</p>
      <ul className="divide-y divide-line">
        {list.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
            <label className="flex min-w-0 flex-1 items-start gap-3">
              <input type="checkbox" role="switch" className="mt-1 accent-accent-400" checked={s.enabled} aria-label={s.label}
                onChange={(e) => act(() => api(`/monitor/sources/${s.id}`, { method: "PUT", json: { enabled: e.target.checked } }).then(() => e.target.checked && api(`/monitor/sources/${s.id}/run`, { method: "POST" })))} />
              <span className="min-w-0"><span className="font-medium">{s.label}</span>
                <span className="block text-xs text-text-muted">{s.description}</span>
                <span className="block text-xs text-text-muted">
                  {s.running ? "Updating…" : s.last_run ? (s.last_ok ? `${ago(s.last_run)} · ${s.last_count} items` : <span className="text-coral-500">{s.last_error || "Last run failed"}</span>) : s.enabled ? "Waiting for the first run" : "Off"}
                  {" · "}<a href={safeUrl(s.url)} target="_blank" rel="noreferrer" className="underline">{s.attribution}</a>
                  {s.needs_key && !s.key_set ? " · needs a free key" : ""}
                </span></span>
            </label>
            <button className="btn-ghost px-2 py-1 text-xs" disabled={s.running || (!!s.needs_key && !s.key_set)} onClick={() => act(() => api(`/monitor/sources/${s.id}/run`, { method: "POST" }))}><ArrowClockwise size={13} /> Run now</button>
          </li>))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------- markets
function Markets({ ov, act }: { ov: Overview | null; act: (fn: () => Promise<unknown>) => void }) {
  const [days, setDays] = useState(30);
  const [movers] = usePoll<Movers>(`/monitor/markets/movers?days=${days}&limit=10`, 30000, [days]);
  const [watch] = usePoll<Quote[]>("/monitor/markets/watchlist", 30000);
  const [list, setList] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const m = ov?.markets;
  const set = (json: Record<string, string>) => act(() => api("/monitor/markets/settings", { method: "PUT", json }));
  const { go } = useNav();
  return (
    <div className="space-y-4">
      <Card title={`Top performers in India${movers?.latest ? ` · to ${movers.latest}` : ""}`}
        actions={<div className="seg" role="group" aria-label="Period">{[1, 7, 30, 90].map((d) => <button key={d} aria-pressed={days === d} onClick={() => setDays(d)}>{d === 1 ? "1 day" : `${d} days`}</button>)}</div>}>
        {!movers && <div className="skeleton h-32" />}
        {movers && !movers.ok && (
          <div className="space-y-2 text-sm">
            <p className="text-text-secondary">{movers.reason}</p>
            <button className="btn-primary" onClick={() => act(async () => { await api("/monitor/sources/market.largecap", { method: "PUT", json: { enabled: true } }); await api("/monitor/sources/market.largecap/run", { method: "POST" }); })}>
              Turn on India large companies</button>
          </div>)}
        {movers?.ok && (
          <>
            <div className="grid gap-4 md:grid-cols-2">
              <MoverTable title="Best" rows={movers.gainers} />
              <MoverTable title="Weakest" rows={movers.losers} />
            </div>
            <p className="mt-3 text-xs text-text-muted">{movers.universe} {movers.scope} compared, closing prices from {movers.base} to {movers.latest}.{movers.scope === "all NSE stocks" ? ` Only stocks that traded more than ₹${movers.min_turnover_cr} crore on ${movers.latest}.` : ""} Source: {movers.source}. Not investment advice.
              {movers.excluded?.length ? ` Left out because the price jumped overnight, which is almost always a split or bonus issue and not a real move: ${movers.excluded.map((e) => `${e.symbol} (${e.change_pct > 0 ? "+" : ""}${e.change_pct}% on ${e.day})`).join(", ")}.` : ""}</p>
            <button className="btn-ghost mt-2 px-2 py-1 text-xs" onClick={() => go({ page: "Ask", params: { q: `Which Indian stocks were the top performers in the last ${days} days? Give me the top 10 with their returns.` } })}><ChatCircleText size={13} /> Ask about this</button>
          </>)}
        {m && <div className="mt-3 space-y-2 text-xs text-text-muted">
          <p>{m.indian_symbols} Indian stocks stored over {m.days} trading days{m.first_day ? ` (${m.first_day} to ${m.last_day})` : ""}.</p>
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn-secondary px-3 py-1 text-xs" onClick={() => fileRef.current?.click()}><UploadSimple size={13} /> Import NSE files</button>
            <input ref={fileRef} type="file" multiple accept=".csv,text/csv" hidden aria-label="NSE end-of-day files"
              onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ""; if (files.length) act(async () => { const f = new FormData(); files.forEach((x) => f.append("files", x)); const r = await api<{ days: number; problems: string[] }>("/monitor/markets/import", { method: "POST", body: f }); setNote(`Imported ${r.days} trading day${r.days === 1 ? "" : "s"}${r.problems.length ? `; skipped ${r.problems.length}: ${r.problems[0]}` : ""}`); }); }} />
            <span>For every NSE stock: download the "sec_bhavdata_full" file for each day you want from <a className="underline" href="https://www.nseindia.com/all-reports" target="_blank" rel="noreferrer">nseindia.com → Daily Reports</a>, then import them here (about 30 files for a month).</span>
          </div>
          {note && <p className="field-ok" role="status">{note}</p>}
        </div>}
      </Card>

      <Card title="Watchlist" actions={<button className="btn-ghost px-2 py-1 text-xs" onClick={() => setList(list === null ? (m?.watchlist ?? []).join(", ") : null)}>{list === null ? "Edit" : "Close"}</button>}>
        {list !== null && (
          <div className="mb-3 space-y-2">
            <textarea className="input-field" rows={2} aria-label="Watchlist symbols" value={list} onChange={(e) => setList(e.target.value)} placeholder="RELIANCE.NS, TCS.NS, ^NSEI, AAPL" />
            <p className="text-xs text-text-muted">Yahoo Finance symbols: add .NS for NSE stocks (INFY.NS), ^NSEI for the Nifty, AAPL for US stocks.</p>
            <button className="btn-primary" onClick={() => { set({ watchlist: list }); act(() => api("/monitor/sources/market.yahoo", { method: "PUT", json: { enabled: true } }).then(() => api("/monitor/sources/market.yahoo/run", { method: "POST" }))); setList(null); }}>Save and fetch</button>
          </div>)}
        {watch?.length ? (
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {watch.map((q) => (
              <li key={q.symbol} className="well flex items-center justify-between p-3 text-sm">
                <div><p className="mono font-medium">{q.symbol}</p><p className="text-xs text-text-muted">{q.day}{q.range_low != null ? ` · range ${q.range_low.toFixed(0)}–${q.range_high?.toFixed(0)}` : ""}</p></div>
                <div className="text-right"><p>{q.close.toLocaleString(undefined, { maximumFractionDigits: 2 })} <span className="text-xs text-text-muted">{q.currency}</span></p>
                  {q.change_pct != null && <p className={`text-xs ${q.change_pct >= 0 ? "text-sage-500" : "text-coral-500"}`}>{q.change_pct >= 0 ? "▲" : "▼"} {Math.abs(q.change_pct).toFixed(2)}%</p>}</div>
              </li>))}
          </ul>) : <p className="text-sm text-text-muted">No prices yet. Switch on "Indices and global stocks" below, or add symbols with Edit.</p>}
      </Card>

      <Card title="Data source for prices">
        <div className="space-y-3 text-sm">
          <div role="radiogroup" aria-label="Price data" className="grid gap-2 sm:grid-cols-3">
            {[["free", "Free data", "Yahoo Finance charts, plus NSE files you import. No key."], ["finnhub", "Finnhub key", "Live quotes and quarterly earnings. Free key."], ["web", "Web search only", "No stored prices; answers use web pages."]].map(([id, name, how]) => (
              <button key={id} role="radio" aria-checked={m?.provider === id} onClick={() => set({ provider: id })}
                className={`card p-3 text-left transition hover:border-accent-400 ${m?.provider === id ? "border-accent-400 ring-2 ring-accent-100" : ""}`}><span className="block font-medium">{name}</span><span className="text-xs text-text-muted">{how}</span></button>))}
          </div>
          {m?.provider === "finnhub" && (
            <div className="flex flex-wrap items-center gap-2">
              <input type="password" className="input-field max-w-xs" aria-label="Finnhub API key" placeholder={m.finnhub_key ? "Key saved" : "Paste your Finnhub key"} value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" />
              <button className="btn-secondary" disabled={!key} onClick={() => { set({ finnhub_key: key }); setKey(""); }}>Save key</button>
              <a className="text-xs underline" href="https://finnhub.io/register" target="_blank" rel="noreferrer">Get a free key <ArrowSquareOut size={11} className="inline" /></a>
            </div>)}
          <p className="text-xs text-text-muted">Quarterly results for Indian companies aren't in free price data: read the filing on the NSE or BSE website, or add a Finnhub key (best for US companies). KeiroLake says so instead of guessing.</p>
        </div>
      </Card>
      <Sources ov={ov} group="market" act={act} />
    </div>
  );
}

function MoverTable({ title, rows }: { title: string; rows: Mover[] }) {
  return (
    <div>
      <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">{title}</p>
      <table className="w-full text-sm"><caption className="sr-only">{title} performers</caption>
        <thead><tr className="text-left text-xs text-text-muted"><th scope="col" className="py-1 font-normal">Stock</th><th scope="col" className="py-1 text-right font-normal">Return</th><th scope="col" className="py-1 text-right font-normal">Price</th></tr></thead>
        <tbody className="divide-y divide-line">
          {rows.map((r) => (
            <tr key={r.symbol}><th scope="row" className="mono py-1.5 text-left font-medium">{r.symbol}</th>
              <td className={`py-1.5 text-right ${r.pct >= 0 ? "text-sage-500" : "text-coral-500"}`}>{r.pct >= 0 ? "+" : ""}{r.pct.toFixed(1)}%</td>
              <td className="py-1.5 text-right text-text-secondary">₹{r.base_close.toFixed(0)} → ₹{r.close.toFixed(0)}</td></tr>))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- world and cyber
function Feed({ group, kinds, ov, act }: { group: string; kinds: [string, string][]; ov: Overview | null; act: (fn: () => Promise<unknown>) => void }) {
  const [kind, setKind] = useState("");
  const [sev, setSev] = useState("low");
  const [days, setDays] = useState(7);
  const [q, setQ] = useState("");
  const [events] = usePoll<Ev[]>(`/monitor/events?group=${group}&kind=${kind}&min_severity=${sev}&days=${days}&q=${encodeURIComponent(q)}&limit=80`, 20000, [kind, sev, days, q]);
  const { go } = useNav();
  const on = (ov?.sources ?? []).some((s) => s.group === group && s.enabled);
  return (
    <div className="space-y-4">
      <Card title={group === "crisis" ? "What's happening" : "Threats"}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="seg overflow-x-auto" role="group" aria-label="Type">{kinds.map(([id, label]) => <button key={id} aria-pressed={kind === id} onClick={() => setKind(id)}>{label}</button>)}</div>
          <select className="input-field w-auto" aria-label="Severity" value={sev} onChange={(e) => setSev(e.target.value)}>
            <option value="info">All severities</option><option value="low">Low and up</option><option value="medium">Medium and up</option><option value="high">High and up</option><option value="critical">Critical</option></select>
          <select className="input-field w-auto" aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={1}>Last day</option><option value={7}>Last week</option><option value={30}>Last month</option></select>
          <input className="input-field max-w-[14rem]" aria-label="Search events" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        {!events && <div className="skeleton h-32" />}
        {events && !events.length && <p className="text-sm text-text-muted">{on ? "Nothing matches yet. Sources update on their schedule, or press Run now below." : "Switch on a source below to start collecting."}</p>}
        <ul className="divide-y divide-line">
          {events?.map((e) => (
            <li key={e.id} className="flex items-start gap-3 py-2.5 text-sm">
              <span className={`${SEV[e.severity] ?? "badge-muted"} mt-0.5 shrink-0`}>{e.severity}</span>
              <div className="min-w-0 flex-1">
                <p className="font-medium">{safeUrl(e.url) ? <a href={safeUrl(e.url)} target="_blank" rel="noreferrer" className="hover:underline">{e.title}</a> : e.title}</p>
                {e.summary && <p className="line-clamp-2 text-xs text-text-secondary">{e.summary}</p>}
                <p className="text-xs text-text-muted">{[e.country, ago(e.occurred_at), e.source.split(".")[1]].filter(Boolean).join(" · ")}</p>
              </div>
              <button className="btn-ghost shrink-0 px-2 py-1 text-xs" aria-label={`Ask about ${e.title}`} onClick={() => go({ page: "Ask", params: { q: `What should I know about this: ${e.title}?` } })}><ChatCircleText size={13} /></button>
            </li>))}
        </ul>
      </Card>
      <Sources ov={ov} group={group} act={act} />
    </div>
  );
}

function Cyber({ ov, act }: { ov: Overview | null; act: (fn: () => Promise<unknown>) => void }) {
  const [term, setTerm] = useState("");
  const [found, setFound] = useState<Ev[] | null>(null);
  const [watch, setWatch] = useState("");
  const [rules] = usePoll<Rule[]>("/monitor/rules", 10000);
  const look = () => act(async () => setFound(await api<Ev[]>(`/monitor/threat/${encodeURIComponent(term.trim())}`)));
  const mine = (rules ?? []).filter((r) => r.kind === "keyword" && r.scope === "cyber");
  return (
    <div className="space-y-4">
      <Card title="Look up a vulnerability or product">
        <div className="flex flex-wrap gap-2">
          <input className="input-field max-w-sm" aria-label="CVE, vendor or product" placeholder="CVE-2026-1234, Fortinet, Exchange…" value={term} onChange={(e) => setTerm(e.target.value)} onKeyDown={(e) => e.key === "Enter" && term.trim() && look()} />
          <button className="btn-secondary" disabled={!term.trim()} onClick={look}>Look up</button>
        </div>
        {found && (found.length ? <ul className="mt-3 divide-y divide-line text-sm">{found.map((e, i) => <li key={i} className="py-2"><span className={`${SEV[e.severity] ?? "badge-muted"} mr-2`}>{e.severity}</span>
          <a href={safeUrl(e.url)} target="_blank" rel="noreferrer" className="hover:underline">{e.title}</a></li>)}</ul> : <p className="mt-3 text-sm text-text-muted">Nothing stored about that yet. Only what your sources have collected is searched.</p>)}
      </Card>
      <Card title="Watch for">
        <p className="mb-2 text-xs text-text-muted">Get an alert when a new vulnerability or indicator mentions any of these words (a vendor, a product, your own software).</p>
        <div className="mb-2 flex flex-wrap gap-2">{mine.flatMap((r) => (r.config.terms as string[]).map((t) => (
          <span key={r.id + t} className="chip">{t}<button aria-label={`Stop watching ${t}`} onClick={() => act(() => api(`/monitor/rules/${r.id}`, { method: "DELETE" }))}><Trash size={11} /></button></span>)))}</div>
        <div className="flex flex-wrap gap-2">
          <input className="input-field max-w-xs" aria-label="Word to watch" placeholder="Fortinet, Citrix, Exchange…" value={watch} onChange={(e) => setWatch(e.target.value)} />
          <button className="btn-secondary" disabled={!watch.trim()} onClick={() => { act(() => api("/monitor/rules", { method: "POST", json: { kind: "keyword", scope: "cyber", config: { terms: watch.split(",").map((t) => t.trim()).filter(Boolean) } } })); setWatch(""); }}>Watch</button>
        </div>
      </Card>
      <Feed group="cyber" kinds={[["", "All"], ["cve", "Vulnerabilities"], ["ioc", "Indicators"]]} ov={ov} act={act} />
    </div>
  );
}

// ---------------------------------------------------------------- alerts and brief
function Alerts({ act, ov }: { act: (fn: () => Promise<unknown>) => void; ov: Overview | null }) {
  const [alerts, reloadAlerts] = usePoll<Alert[]>("/monitor/alerts?limit=60", 10000);
  const [rules, reloadRules] = usePoll<Rule[]>("/monitor/rules", 10000);
  const [briefs, reloadBriefs] = usePoll<Brief[]>("/monitor/briefs", 15000);
  const [pct, setPct] = useState("5");
  const brief = briefs?.[0];
  const done = (fn: () => Promise<unknown>) => act(async () => { await fn(); reloadAlerts(); reloadRules(); reloadBriefs(); });
  const label = (r: Rule) => r.kind === "severity" ? `${r.scope} events rated ${r.config.min} or worse${r.config.sources ? ` (${(r.config.sources as string[]).map((s) => s.split(".")[1]).join(", ")})` : ""}`
    : r.kind === "keyword" ? `${r.scope} events mentioning ${(r.config.terms as string[]).join(", ")}` : r.kind === "price_move" ? `watchlist prices moving ${r.config.pct}% in a day` : r.kind === "new_high" ? "watchlist at a new high" : "watchlist at a new low";
  return (
    <div className="space-y-4">
      <Card title="Daily brief" actions={<button className="btn-primary" onClick={() => done(() => api("/monitor/briefs", { method: "POST" }))}><Newspaper size={15} /> Write today's brief</button>}>
        {brief ? (
          <article>
            <p className="mb-2 text-xs text-text-muted">{brief.title} · written {ago(brief.created_at)} by {brief.model}</p>
            <div className="md text-[15px] leading-relaxed"><Markdown remarkPlugins={[remarkGfm]} urlTransform={(u) => safeUrl(u) ?? ""}>{brief.body}</Markdown></div>
            <details className="mt-3 text-xs"><summary className="cursor-pointer text-text-secondary">Sources ({brief.sources.length})</summary>
              <ol className="mt-1 space-y-0.5">{brief.sources.map((s) => <li key={s.n}>[{s.n}] {safeUrl(s.url) ? <a href={safeUrl(s.url)} target="_blank" rel="noreferrer" className="underline">{s.title}</a> : s.title}</li>)}</ol></details>
          </article>) : <p className="text-sm text-text-muted">A cited summary of what changed: markets, world events and cyber threats. Switch on a few sources first, then write one. Switch on "Daily brief" in Sources to get one every morning.</p>}
      </Card>

      <Card title={<span className="flex items-center gap-2"><Bell size={16} className="text-accent-400" />Alerts</span>} actions={<button className="btn-ghost" onClick={() => done(() => api("/monitor/alerts/seen", { method: "POST", json: {} }))}>Mark all read</button>}>
        {alerts && !alerts.length && <p className="text-sm text-text-muted">No alerts yet. They appear when a rule below matches something new.</p>}
        <ul className="divide-y divide-line">{alerts?.map((a) => (
          <li key={a.id} className="flex items-start gap-3 py-2 text-sm">
            <span className={`${SEV[a.severity] ?? "badge-muted"} mt-0.5 shrink-0`}>{a.severity}</span>
            <div className="min-w-0 flex-1"><p className={a.seen ? "" : "font-semibold"}>{safeUrl(a.url) ? <a href={safeUrl(a.url)} target="_blank" rel="noreferrer" className="hover:underline">{a.title}</a> : a.title}</p>
              <p className="text-xs text-text-muted">{a.scope} · {ago(a.created_at)}</p></div></li>))}</ul>
      </Card>

      <Card title="Alert rules">
        <ul className="divide-y divide-line">{rules?.map((r) => (
          <li key={r.id} className="flex items-center justify-between gap-2 py-2 text-sm">
            <label className="flex items-center gap-3"><input type="checkbox" role="switch" className="accent-accent-400" checked={r.enabled} onChange={(e) => done(() => api(`/monitor/rules/${r.id}`, { method: "PUT", json: { enabled: e.target.checked } }))} aria-label={label(r)} />{label(r)}</label>
            <button className="icon-btn" aria-label="Delete rule" onClick={() => done(() => api(`/monitor/rules/${r.id}`, { method: "DELETE" }))}><Trash size={15} /></button></li>))}</ul>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-text-secondary">Add:</span>
          <select className="input-field w-auto" aria-label="Minimum severity" defaultValue="" onChange={(e) => { const [scope, min] = e.target.value.split(":"); if (scope) done(() => api("/monitor/rules", { method: "POST", json: { kind: "severity", scope, config: { min } } })); e.target.value = ""; }}>
            <option value="">Events by severity…</option>
            {["crisis", "cyber"].flatMap((sc) => ["medium", "high", "critical"].map((v) => <option key={sc + v} value={`${sc}:${v}`}>{sc} · {v} or worse</option>))}</select>
          <span className="flex items-center gap-1"><input className="input-field w-16" aria-label="Percent" inputMode="decimal" value={pct} onChange={(e) => setPct(e.target.value)} />%
            <button className="btn-secondary" onClick={() => done(() => api("/monitor/rules", { method: "POST", json: { kind: "price_move", scope: "market", config: { pct: Number(pct) || 5 } } }))}>Price moves</button></span>
          <button className="btn-secondary" onClick={() => done(() => api("/monitor/rules", { method: "POST", json: { kind: "new_high", scope: "market", config: {} } }))}>New highs</button>
          <button className="btn-secondary" onClick={() => done(() => api("/monitor/rules", { method: "POST", json: { kind: "new_low", scope: "market", config: {} } }))}>New lows</button>
        </div>
        <p className="mt-2 text-xs text-text-muted">Alerts also go to any webhook subscribed to <span className="mono">monitor.alert</span> (Serve → Webhooks).</p>
      </Card>
      <Sources ov={ov} group="brief" act={act} />
    </div>
  );
}
