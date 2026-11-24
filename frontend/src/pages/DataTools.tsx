import { ArrowSquareOut, CheckCircle, Key, Lightning, Play, Table as TableIcon } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText } from "../api";
import { Card, ErrorNote, PageHeader, useKept, useNav, usePoll } from "../ui";

type Template = { id: string; name: string; about: string; input: string; example: string; licence: string };
type Scraper = { id: string; group: string; site?: string; name: string; about: string; credits: number; input: { label: string; placeholder?: string; example?: string }; available?: boolean; unavailableReason?: string };
type Overview = { templates: Template[]; renderdata: { key_set: boolean; base: string } };
type Conn = { id: string; name: string; sheet_id: string | null; last_run_at: string | null; last_result: { added?: number; error?: string } };

const EVERY = [[0, "Once"], [60, "Hourly"], [360, "Every 6 hours"], [1440, "Daily"], [10080, "Weekly"]] as const;

/** More ways to fill a table: RenderData's scrapers (AI answers, trends, any page) and free open-data APIs. */
export default function DataTools() {
  const [o, reload] = usePoll<Overview>("/datatools", 30000);
  const [conns] = usePoll<Conn[]>("/connectors", 3000);
  const [made, setMade] = useKept<string[]>("datatools:made", []);
  const { go } = useNav();
  const mine = (conns ?? []).filter((c) => made.includes(c.id));
  return (
    <div className="space-y-4">
      <PageHeader title="Data tools" />
      {!!mine.length && (
        <Card title="Collected here">
          <ul className="divide-y divide-line text-sm">{mine.map((c) => (
            <li key={c.id} className="flex items-center gap-2 py-1.5">
              <span className="min-w-0 flex-1 truncate">{c.name}</span>
              {c.last_result?.error ? <span className="truncate text-xs text-coral-500" title={c.last_result.error}>{c.last_result.error}</span>
                : c.last_run_at ? <span className="text-xs text-text-muted">{c.last_result?.added ?? 0} rows added</span> : <span className="text-xs text-text-muted">Collecting…</span>}
              {c.sheet_id && <button className="btn-ghost py-1" onClick={() => go({ page: "Tables", params: { sheet: c.sheet_id! } })}><TableIcon size={14} />Open</button>}
            </li>))}</ul>
        </Card>
      )}
      <RenderData o={o} reload={reload} onMade={(id) => setMade([id, ...made].slice(0, 20))} />
      <OpenData templates={o?.templates ?? []} onMade={(id) => setMade([id, ...made].slice(0, 20))} />
    </div>
  );
}

function Schedule({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <select aria-label="How often" className="input-field w-auto" value={value} onChange={(e) => onChange(+e.target.value)}>
      {EVERY.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}

function RenderData({ o, reload, onMade }: { o: Overview | null; reload: () => void; onMade: (id: string) => void }) {
  const [catalog] = usePoll<{ groups: string[]; scrapers: Scraper[] }>(o?.renderdata ? "/datatools/renderdata/catalog" : null, 600000);
  const [key, setKey] = useState("");
  const [pick, setPick] = useKept("datatools:rd", "");
  const [input, setInput] = useState("");
  const [every, setEvery] = useState(0);
  const [append, setAppend] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [account, reloadAccount] = usePoll<{ key_set: boolean; valid?: boolean; email?: string; error?: string }>(o?.renderdata.key_set ? "/datatools/renderdata/account" : null, 600000);
  const PASTE: Scraper = { id: "url", group: "Any website", site: "Any link", name: "Paste a link", credits: 1, about: "Paste any page address and RenderData picks the right scraper for it; unknown sites are read as an article.", input: { label: "Page address", placeholder: "https://…" } };
  const scrapers = catalog ? [PASTE, ...catalog.scrapers] : [];
  const scraper = scrapers.find((s) => s.id === pick);
  const saveKey = async () => { setError(""); try { await api("/settings", { method: "PUT", json: { values: { RENDERDATA_API_KEY: key.trim() } } }); setKey(""); reload(); reloadAccount(); } catch (e) { setError(errText(e)); } };
  const forget = async () => { setError(""); try { await api("/settings", { method: "PUT", json: { values: { RENDERDATA_API_KEY: "" } } }); reload(); } catch (e) { setError(errText(e)); } };
  const run = async () => {
    if (!scraper) return;
    setBusy(true); setError("");
    try {
      const c = await api<{ id: string }>("/datatools/renderdata", { method: "POST", json: { scraper: scraper.id, input, name: `${scraper.site ?? scraper.name}: ${input}`.slice(0, 80), interval_minutes: every, append } });
      onMade(c.id); setInput("");
    } catch (e) { setError(errText(e)); }
    setBusy(false);
  };
  return (
    <Card title={<span className="flex items-center gap-2"><Lightning size={16} className="text-accent-600" />RenderData</span>}
      actions={<a className="btn-ghost py-1" href="https://renderdata.cloud" target="_blank" rel="noreferrer">renderdata.cloud <ArrowSquareOut size={13} /></a>}>
      <p className="text-sm text-text-secondary">Ready-made scrapers: what ChatGPT, Gemini and Google AI Mode answer, Google Trends, and any page as clean text. Runs use your RenderData credits; failed runs are free. Each result becomes a table, and a schedule turns it into a time series.</p>
      {!o ? <div className="skeleton mt-3 h-10" /> : !o.renderdata.key_set ? (
        <div className="mt-3 flex flex-wrap gap-2">
          <input aria-label="RenderData API key" type="password" autoComplete="off" className="input-field mono min-w-0 flex-1" placeholder="RenderData API key (sb_...)" value={key} onChange={(e) => setKey(e.target.value)} />
          <button className="btn-primary" disabled={key.trim().length < 8} onClick={saveKey}><Key size={14} />Save key</button>
        </div>
      ) : account?.valid === false ? (
        <p className="field-error mt-3 flex items-center justify-between gap-2">{account.error}<button className="btn-ghost py-0.5" onClick={forget}>Change key</button></p>
      ) : (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-text-muted"><CheckCircle size={13} className="text-sage-500" />
          {account?.email ? `Connected as ${account.email}` : "Key saved on this computer"}<button className="ml-2 underline" onClick={forget}>Change key</button></p>
      )}
      {catalog && (
        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {scrapers.map((s) => (
            <button key={s.id} aria-pressed={pick === s.id} disabled={s.available === false} title={s.unavailableReason}
              onClick={() => { setPick(s.id); setInput(""); }}
              className={`rounded-md border p-3 text-left transition disabled:opacity-50 ${pick === s.id ? "border-accent-600 bg-accent-50" : "border-line hover:border-accent-400"}`}>
              <span className="flex items-center justify-between gap-2"><span className="font-medium">{s.site && !s.name.includes(s.site) ? `${s.site} · ${s.name}` : s.name}</span><span className="text-xs text-text-muted">{s.credits} credit{s.credits === 1 ? "" : "s"}</span></span>
              <span className="mt-0.5 block text-xs text-text-muted">{s.group}</span>
            </button>
          ))}
        </div>
      )}
      {scraper && (
        <div className="mt-4 space-y-2 border-t border-line pt-4">
          <p className="text-sm text-text-secondary">{scraper.about}</p>
          <div className="flex flex-wrap gap-2">
            <input aria-label={scraper.input.label} className="input-field min-w-0 flex-1" value={input} onChange={(e) => setInput(e.target.value)}
              placeholder={scraper.input.placeholder || scraper.input.example || scraper.input.label} />
            <Schedule value={every} onChange={setEvery} />
            <button className="btn-primary" disabled={busy || !input.trim() || !o?.renderdata.key_set} onClick={run}><Play size={14} weight="fill" />{busy ? "Starting…" : "Run"}</button>
          </div>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={append} onChange={(e) => setAppend(e.target.checked)} />Keep every run as new rows (to compare answers over time)</label>
        </div>
      )}
      <ErrorNote error={error} onClose={() => setError("")} />
    </Card>
  );
}

function OpenData({ templates, onMade }: { templates: Template[]; onMade: (id: string) => void }) {
  const [pick, setPick] = useKept("datatools:open", "");
  const [input, setInput] = useState("");
  const [every, setEvery] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const t = templates.find((x) => x.id === pick);
  const run = async () => {
    if (!t) return;
    setBusy(true); setError("");
    try { const c = await api<{ id: string }>("/datatools/use", { method: "POST", json: { template: t.id, input, name: `${t.name}: ${input}`.slice(0, 80), interval_minutes: every } }); onMade(c.id); setInput(""); }
    catch (e) { setError(errText(e)); }
    setBusy(false);
  };
  return (
    <Card title="Open data">
      <p className="text-sm text-text-secondary">Free public sources that need no key. Each search becomes a table you can refresh.</p>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {templates.map((x) => (
          <button key={x.id} aria-pressed={pick === x.id} onClick={() => { setPick(x.id); setInput(""); }}
            className={`rounded-md border p-3 text-left transition ${pick === x.id ? "border-accent-600 bg-accent-50" : "border-line hover:border-accent-400"}`}>
            <span className="block font-medium">{x.name}</span><span className="mt-0.5 block text-xs text-text-muted">{x.about}</span>
          </button>
        ))}
      </div>
      {t && (
        <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-4">
          <input aria-label={t.input} className="input-field min-w-0 flex-1" value={input} onChange={(e) => setInput(e.target.value)} placeholder={`${t.input}, for example ${t.example}`} />
          <Schedule value={every} onChange={setEvery} />
          <button className="btn-primary" disabled={busy || !input.trim()} onClick={run}><Play size={14} weight="fill" />{busy ? "Starting…" : "Collect"}</button>
          <p className="w-full text-xs text-text-muted">Licence: {t.licence}. Credit the source when you share the data.</p>
        </div>
      )}
      <ErrorNote error={error} onClose={() => setError("")} />
    </Card>
  );
}
