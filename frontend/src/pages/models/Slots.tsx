import { ArrowClockwise, Brain, CheckCircle } from "@phosphor-icons/react";
import { useMemo, useState } from "react";
import { api, errText } from "../../api";
import { Card, ErrorNote, usePoll } from "../../ui";

type Entry = { id: string; provider: string; model: string; name: string; context: number | null; price_in: number | null; price_out: number | null;
  local: boolean; usable: boolean; use_cases: string[]; released: string };
type Slot = { slot: string; label: string; help: string; value: string; current: Entry | null; suggestions: Entry[] };
type Data = { slots: Slot[]; catalog: { fetched_at: number; error: string; refreshing: boolean; usable_providers: string[] }; models: Entry[] };

const price = (e: Entry) => e.local ? "on this computer" : e.price_in == null ? "price unknown" : e.price_in === 0 && e.price_out === 0 ? "free" : `$${e.price_in} / $${e.price_out} per 1M tokens`;
const size = (n: number | null) => !n ? "" : n >= 1e6 ? `${Math.round(n / 1e5) / 10}M context` : `${Math.round(n / 1000)}K context`;

/** Pick a model for each kind of task, from a live list with what each model is good for. */
export default function Slots() {
  const [data, reload] = usePoll<Data>("/model-slots", 30000);
  const [error, setError] = useState("");
  const [open, setOpen] = useState("");
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => { setError(""); setBusy(true); try { await fn(); } catch (e) { setError(errText(e)); } setBusy(false); reload(); };
  const assign = (slot: string, model: string) => act(() => api(`/model-slots/${slot}`, { method: "PUT", json: { model } }));
  const updated = data?.catalog.fetched_at ? new Date(data.catalog.fetched_at * 1000).toLocaleDateString() : "not yet";

  const browse = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (data?.models ?? []).filter((m) => m.usable && (!f || `${m.name} ${m.id} ${m.use_cases.join(" ")}`.toLowerCase().includes(f)))
      .sort((a, b) => (b.released || "").localeCompare(a.released || "")).slice(0, 80);
  }, [data, filter]);

  return (
    <Card title={<span className="flex items-center gap-2"><Brain size={16} className="text-accent-400" />Model for each kind of task</span>}
      actions={<button className="btn-ghost" disabled={busy} onClick={() => act(() => api("/model-slots/refresh", { method: "POST" }))}><ArrowClockwise size={14} /> Update list</button>}>
      <p className="mb-3 text-sm text-text-secondary">Everything starts on your local model. Choose a stronger one for the tasks that need it; anything you don't choose keeps the default. Private notes always stay on this computer.</p>
      <ErrorNote error={error} onClose={() => setError("")} />
      {data && !data.catalog.usable_providers.length && (
        <p className="mb-3 text-sm text-text-muted">Add a cloud provider key below (OpenRouter, OpenAI, Anthropic, Gemini…) to choose cloud models. Models on this computer are always available.</p>
      )}
      <div className="divide-y divide-line">
        {data?.slots.map((s) => (
          <div key={s.slot} className="py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0"><p className="font-medium">{s.label}</p><p className="text-xs text-text-muted">{s.help}</p></div>
              <div className="flex items-center gap-2">
                {s.value ? <span className="badge-ok mono max-w-[16rem] truncate" title={s.value}>{s.value}</span> : <span className="badge-muted">Default</span>}
                {s.value && <button className="btn-ghost px-2 py-1 text-xs" disabled={busy} onClick={() => assign(s.slot, "")}>Clear</button>}
                <button className="btn-secondary px-2 py-1 text-xs" aria-expanded={open === s.slot} onClick={() => setOpen(open === s.slot ? "" : s.slot)}>{open === s.slot ? "Close" : "Choose"}</button>
              </div>
            </div>
            {s.current && <p className="mt-1 text-xs text-text-muted">{s.current.use_cases.join(" · ")} · {price(s.current)} {size(s.current.context) && `· ${size(s.current.context)}`}</p>}
            {open === s.slot && (
              <div className="well mt-3 space-y-3 p-3">
                <div>
                  <p className="mb-1 text-xs font-semibold text-text-secondary">Best fits among what you can use now</p>
                  {s.suggestions.length === 0 && <p className="text-xs text-text-muted">Nothing to suggest yet: start a local model or add a provider key.</p>}
                  <ul className="space-y-1">
                    {s.suggestions.map((m) => (
                      <li key={m.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-surface-primary px-3 py-2 text-sm">
                        <div className="min-w-0"><p className="truncate font-medium">{m.name}</p>
                          <p className="text-xs text-text-muted">{m.use_cases.slice(0, 4).join(" · ")} · {price(m)}</p></div>
                        {s.value === m.id ? <span className="flex items-center gap-1 text-xs text-sage-500"><CheckCircle size={14} weight="fill" /> In use</span>
                          : <button className="btn-primary px-3 py-1 text-xs" disabled={busy} onClick={() => assign(s.slot, m.id)}>Use for {s.label.toLowerCase()}</button>}
                      </li>
                    ))}
                  </ul>
                </div>
                <details>
                  <summary className="cursor-pointer text-xs text-text-secondary">Browse every model ({data.models.filter((m) => m.usable).length})</summary>
                  <input className="input-field mt-2" aria-label="Filter models" placeholder="Filter: vision, cheap, claude, reasoning…" value={filter} onChange={(e) => setFilter(e.target.value)} />
                  <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto">
                    {browse.map((m) => (
                      <li key={m.id} className="flex items-center justify-between gap-2 text-xs">
                        <span className="min-w-0 truncate"><span className="font-medium">{m.name}</span> <span className="text-text-muted">{m.use_cases.slice(0, 3).join(" · ")} · {price(m)}</span></span>
                        <button className="btn-ghost px-2 py-0.5 text-xs" disabled={busy || s.value === m.id} onClick={() => assign(s.slot, m.id)}>{s.value === m.id ? "In use" : "Use"}</button>
                      </li>
                    ))}
                    {!browse.length && <li className="text-text-muted">No matches.</li>}
                  </ul>
                </details>
              </div>
            )}
          </div>
        ))}
        {!data && <div className="skeleton h-24" />}
      </div>
      <p className="mt-2 text-xs text-text-muted">
        List from models.dev and OpenRouter, updated {updated}{data?.catalog.refreshing ? " (updating…)" : ""}. {data?.catalog.error && `Last problem: ${data.catalog.error}`}
      </p>
    </Card>
  );
}
