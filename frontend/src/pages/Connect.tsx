import { CheckCircle, Copy, FloppyDisk, Key, Plugs, ShieldCheck, Trash, WarningCircle } from "@phosphor-icons/react";
import { useState } from "react";
import { api, base64Download, errText, gb, isDesktop, KB, shell } from "../api";
import { Card, ErrorNote, PageHeader, Progress, useJobRunner, usePoll } from "../ui";

type ApiKey = { id: string; name: string; prefix: string; scopes: string[]; kb_ids: string[]; expires_at: string | null; last_used_at: string | null; revoked: boolean; expired: boolean };
type Hook = { id: string; url: string; events: string[]; last_status: string; last_at: string | null };
export type Net = { mode: string; running: boolean; port: number; urls: string[]; fingerprint: string; error: string; addresses: string[] };
export type Overview = { keys: ApiKey[]; webhooks: Hook[]; network: Net; scopes: Record<string, string>; events: Record<string, string> };

const NET_MODES = [["off", "Off"], ["local", "This computer"], ["lan", "Home network"]] as const;

export function CopyBox({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-start gap-2 rounded-xl bg-surface-secondary p-3">
      <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs">{text}</pre>
      <button className="icon-btn" aria-label={`Copy ${label}`} onClick={() => { navigator.clipboard?.writeText(text).then(() => setCopied(true)).catch(() => {}); }}>
        {copied ? <CheckCircle size={16} className="text-sage-500" /> : <Copy size={16} />}
      </button>
    </div>
  );
}

/** Use your data from your own apps: API keys, network access, AI tools (MCP), webhooks, backups and exports. */
export default function Connect() {
  const [data, reload] = usePoll<Overview>("/access", 15000);
  const [kbs] = usePoll<KB[]>("/kbs", 60000);
  const [log] = usePoll<any[]>("/access/log?limit=30", 10000);
  const [error, setError] = useState("");
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); };
  const net = data?.network;
  const base = net?.urls[0] ?? "http://127.0.0.1:8443";

  return (
    <div className="space-y-4">
      <PageHeader title="Connect" />
      <ErrorNote error={error} onClose={() => setError("")} />
      <div className="grid gap-4 lg:grid-cols-2">
        <NetworkCard net={net} act={act} />
        <KeysCard data={data ?? undefined} kbs={kbs ?? []} act={act} base={base} />
      </div>
      <McpCard base={base} running={!!net?.running} />
      <div className="grid gap-4 lg:grid-cols-2">
        <HooksCard data={data ?? undefined} act={act} />
        <Card title="Access log">
          <div className="max-h-72 divide-y divide-line overflow-y-auto text-xs">
            {log?.map((r, i) => (
              <div key={i} className="flex gap-2 py-1.5">
                <span className={r.status < 400 ? "text-sage-500" : "text-coral-500"}>{r.status}</span>
                <span className="min-w-0 flex-1 truncate font-mono">{r.method} {r.path}</span>
                <span className="text-text-muted">{r.key} · {new Date(r.at).toLocaleTimeString()}</span>
              </div>
            ))}
            {log && !log.length && <p className="text-text-muted">No requests yet.</p>}
          </div>
        </Card>
      </div>
    </div>
  );
}

export function NetworkCard({ net, act }: { net?: Net; act: (fn: () => Promise<unknown>) => void }) {
  const [port, setPort] = useState<number | "">("");
  const set = (mode: string) => act(() => api("/access/network", { method: "PUT", json: { mode, port: port || net?.port || 8443 } }));
  return (
    <Card title={<span className="flex items-center gap-2"><Plugs size={16} className="text-accent-400" />Data API</span>}>
      <div className="flex flex-wrap items-center gap-2">
        <div className="seg" role="group" aria-label="Who can reach the data API">
          {NET_MODES.map(([m, label]) => <button key={m} aria-pressed={net?.mode === m} onClick={() => set(m)}>{label}</button>)}
        </div>
        <label className="flex items-center gap-1.5 text-xs text-text-secondary">Port
          <input className="input-field w-24 py-1" inputMode="numeric" value={port === "" ? net?.port ?? "" : port}
            onChange={(e) => setPort(e.target.value ? +e.target.value.replace(/\D/g, "") : "")} onBlur={() => net && net.mode !== "off" && port && set(net.mode)} />
        </label>
      </div>
      {net?.error && <p className="field-error mt-3">{net.error}</p>}
      {net?.running && (
        <div className="mt-3 space-y-2 text-sm">
          {net.urls.map((u) => <CopyBox key={u} text={u} label="address" />)}
          {net.fingerprint && <p className="break-all text-xs text-text-muted"><ShieldCheck size={12} className="mr-1 inline" />Certificate SHA-256 {net.fingerprint}</p>}
          {net.mode === "lan" && <p className="text-xs text-text-muted">From outside your home, use Tailscale instead of opening router ports.</p>}
        </div>
      )}
      {net && net.mode === "off" && <p className="mt-3 text-sm text-text-muted">Your apps can't reach your data until you turn this on. Every request needs a key.</p>}
    </Card>
  );
}

export function KeysCard({ data, kbs, act, base }: { data?: Overview; kbs: KB[]; act: (fn: () => Promise<unknown>) => void; base: string }) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [only, setOnly] = useState<string[]>([]);
  const [days, setDays] = useState("");
  const [secret, setSecret] = useState("");
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const create = () => act(async () => {
    const out = await api("/access/keys", { method: "POST", json: { name, scopes, kb_ids: only, expires_days: days ? +days : null } });
    setSecret(out.secret); setName("");
  });
  return (
    <Card title={<span className="flex items-center gap-2"><Key size={16} className="text-accent-400" />API keys</span>}>
      <div className="space-y-3">
        <input className="input-field w-full" placeholder="App name, e.g. Home dashboard" aria-label="Key name" value={name} onChange={(e) => setName(e.target.value)} />
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(data?.scopes ?? {}).map(([s, help]) => (
            <button key={s} title={help} className={scopes.includes(s) ? "chip-active" : "chip"} onClick={() => setScopes(toggle(scopes, s))}>{s}</button>
          ))}
        </div>
        {kbs.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="text-text-muted">Knowledge bases:</span>
            <button className={!only.length ? "chip-active" : "chip"} onClick={() => setOnly([])}>All</button>
            {kbs.map((k) => <button key={k.id} className={only.includes(k.id) ? "chip-active" : "chip"} onClick={() => setOnly(toggle(only, k.id))}>{k.name}</button>)}
          </div>
        )}
        <div className="flex gap-2">
          <select className="input-field" value={days} onChange={(e) => setDays(e.target.value)} aria-label="Expires">
            <option value="">Never expires</option><option value="7">7 days</option><option value="30">30 days</option><option value="365">1 year</option>
          </select>
          <button className="btn-primary" disabled={!name.trim() || !scopes.length} onClick={create}>Create key</button>
        </div>
        {secret && (
          <div className="space-y-2">
            <p className="flex items-center gap-1.5 text-xs font-semibold text-coral-500"><WarningCircle size={14} />Copy it now. It won't be shown again.</p>
            <CopyBox text={secret} label="API key" />
            <CopyBox text={`curl -H "Authorization: Bearer ${secret}" ${base}/v1/kbs`} label="example request" />
          </div>
        )}
      </div>
      <div className="mt-4 divide-y divide-line text-sm">
        {data?.keys.map((k) => (
          <div key={k.id} className="flex items-center gap-2 py-2">
            <span className="min-w-0 flex-1">
              <span className="block truncate font-semibold">{k.name}</span>
              <span className="block text-xs text-text-muted">{k.prefix}… · {k.scopes.join(", ")}{k.last_used_at ? ` · used ${new Date(k.last_used_at).toLocaleDateString()}` : " · never used"}</span>
            </span>
            {k.revoked ? <span className="badge-muted">Revoked</span> : k.expired ? <span className="badge-warn">Expired</span> :
              <button className="btn-ghost" onClick={() => act(() => api(`/access/keys/${k.id}/revoke`, { method: "POST" }))}>Revoke</button>}
            <button className="icon-btn" aria-label={`Delete ${k.name}`} onClick={() => act(() => api(`/access/keys/${k.id}`, { method: "DELETE" }))}><Trash size={14} /></button>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function McpCard({ base, running }: { base: string; running: boolean }) {
  const config = JSON.stringify({ mcpServers: { keirolake: { type: "http", url: `${base}/mcp`, headers: { Authorization: "Bearer YOUR_KEY" } } } }, null, 2);
  return (
    <Card title="Use your data in AI tools (MCP)">
      <p className="mb-3 text-sm text-text-secondary">Tools: search, ask, list datasets, SQL and add document, limited to the key's permissions.</p>
      <CopyBox text={config} label="MCP config" />
      {!running && <p className="mt-2 text-xs text-text-muted">Turn on the data API above first.</p>}
    </Card>
  );
}

export function HooksCard({ data, act }: { data?: Overview; act: (fn: () => Promise<unknown>) => void }) {
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>(["ingest.finished"]);
  const [secret, setSecret] = useState("");
  return (
    <Card title="Webhooks" actions={data?.webhooks.length ? <button className="btn-ghost" onClick={() => act(() => api("/access/webhooks/test", { method: "POST" }))}>Send test</button> : undefined}>
      <div className="space-y-2">
        <input className="input-field w-full" placeholder="https://my-app.local/hooks/keirolake" aria-label="Webhook address" value={url} onChange={(e) => setUrl(e.target.value)} />
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(data?.events ?? {}).map(([e, help]) => (
            <button key={e} title={help} className={events.includes(e) ? "chip-active" : "chip"} onClick={() => setEvents(events.includes(e) ? events.filter((x) => x !== e) : [...events, e])}>{e}</button>
          ))}
        </div>
        <button className="btn-secondary" disabled={!/^https?:\/\//.test(url) || !events.length}
          onClick={() => act(async () => { const out = await api("/access/webhooks", { method: "POST", json: { url, events } }); setSecret(out.secret); setUrl(""); })}>Add webhook</button>
        {secret && <><p className="text-xs text-text-muted">Signing secret (X-KeiroLake-Signature is HMAC-SHA256 of the body):</p><CopyBox text={secret} label="signing secret" /></>}
      </div>
      <div className="mt-3 divide-y divide-line text-sm">
        {data?.webhooks.map((h) => (
          <div key={h.id} className="flex items-center gap-2 py-2">
            <span className="min-w-0 flex-1"><span className="block truncate">{h.url}</span><span className="text-xs text-text-muted">{h.events.join(", ")}{h.last_status ? ` · last ${h.last_status}` : ""}</span></span>
            <button className="icon-btn" aria-label={`Delete webhook ${h.url}`} onClick={() => act(() => api(`/access/webhooks/${h.id}`, { method: "DELETE" }))}><Trash size={14} /></button>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function BackupsCard() {
  const [backups, reloadBackups] = usePoll<any[]>("/backups", 30000);
  const [settings, reloadSettings] = usePoll<any>("/settings", 60000);
  const [target, reloadTarget] = usePoll<{ target: string; include_lake: boolean; encrypted: boolean }>("/backups/target", 60000);
  const backup = useJobRunner(reloadBackups, "backup");
  const copier = useJobRunner(undefined, "backup-copy");
  const [folder, setFolder] = useState("");
  const [pass, setPass] = useState("");
  const [remember, setRemember] = useState(true);
  const [report, setReport] = useState<{ name: string; ok: boolean; checks: { check: string; ok: boolean; detail: string }[] } | null>(null);
  const [error, setError] = useState("");
  const val = (k: string) => settings?.values?.[k]?.value ?? "";
  const setPref = (k: string, v: string) => api("/settings", { method: "PUT", json: { values: { [k]: v } } }).then(reloadSettings);
  const dest = folder || target?.target || "";
  const pick = async () => { const dir = await shell<string | null>("pick_folder"); if (dir) setFolder(dir); };
  const latest = backups?.[0]?.name;

  const copy = async () => {
    if (!latest) return;
    await copier.run(() => api(`/backups/${latest}/copy`, { method: "POST", json: { target: dest, passphrase: pass, include_lake: true, remember } }));
    reloadTarget();
  };
  const verify = async (name: string) => {
    setError(""); setReport(null);
    try { const r = await api<{ ok: boolean; checks: { check: string; ok: boolean; detail: string }[] }>("/backups/verify", { method: "POST", json: { stamp: name } }); setReport({ name, ...r }); } catch (e) { setError(errText(e)); }
  };

  return (
    <Card title="Backups" actions={
      <button className="btn-secondary" disabled={!!backup.job} onClick={() => backup.run(() => api("/backups", { method: "POST" }))}>
        <FloppyDisk size={16} /> {backup.job ? "Backing up…" : "Back up now"}
      </button>}>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span className="text-text-secondary">Automatic</span>
        <select className="input-field py-1 text-xs" value={val("AUTO_BACKUP_HOURS")} onChange={(e) => setPref("AUTO_BACKUP_HOURS", e.target.value)} aria-label="Backup schedule">
          <option value="">Off</option><option value="24">Daily</option><option value="168">Weekly</option>
        </select>
        <span className="text-text-secondary">keep</span>
        <select className="input-field py-1 text-xs" value={val("KEEP_BACKUPS") || "7"} onChange={(e) => setPref("KEEP_BACKUPS", e.target.value)} aria-label="Backups to keep">
          {[3, 7, 14, 30].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        {target?.target && <span className="text-xs text-text-muted">also copied to {target.target}{target.encrypted ? " · encrypted" : ""}</span>}
      </div>
      {backup.job && <Progress value={backup.job.progress} label={backup.job.message} />}
      <ErrorNote error={backup.error || copier.error || error} onClose={() => setError("")} />

      <div className="well space-y-2 p-3">
        <p className="text-sm font-semibold">Copy to another drive or PC</p>
        <div className="flex flex-wrap gap-2">
          <input className="input-field min-w-0 flex-1" placeholder="E:\\ or \\\\nas\\backups or /media/usb" aria-label="Backup folder" value={dest} onChange={(e) => setFolder(e.target.value)} />
          {isDesktop && <button className="btn-ghost" onClick={pick}>Choose…</button>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input className="input-field min-w-0 flex-1" type="password" autoComplete="new-password" placeholder="Passphrase to encrypt (optional, 8+ characters)"
            aria-label="Backup passphrase" value={pass} onChange={(e) => setPass(e.target.value)} />
          <label className="flex items-center gap-1.5 text-xs text-text-secondary"><input type="checkbox" className="accent-accent-400" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Every backup</label>
          <button className="btn-primary" disabled={!dest.trim() || !latest || !!copier.job || (pass.length > 0 && pass.length < 8)} onClick={copy}>Copy latest</button>
        </div>
        {copier.job && <Progress value={copier.job.progress} label={copier.job.message} />}
        {!latest && <p className="text-xs text-text-muted">Make a backup first.</p>}
      </div>

      <div className="mt-3 divide-y divide-line text-sm">
        {backups?.map((b) => (
          <div key={b.name} className="flex items-center gap-2 py-1.5">
            <span className="min-w-0 flex-1">{b.name}</span>
            <span className="text-xs text-text-muted">{b.knowledge_bases} collections · {gb(b.bytes)}</span>
            <button className="btn-ghost py-1 text-xs" onClick={() => verify(b.name)}>Test restore</button>
          </div>
        ))}
        {backups && !backups.length && <p className="py-1.5 text-text-muted">No backups yet.</p>}
      </div>
      {report && (
        <div className="mt-3 rounded-xl border border-line p-3 text-sm" role="status">
          <p className={report.ok ? "field-ok" : "field-error"}>{report.ok ? `${report.name} can be restored.` : `${report.name} has problems.`}</p>
          <ul className="mt-2 space-y-0.5 text-xs">
            {report.checks.map((c) => <li key={c.check} className={c.ok ? "text-text-secondary" : "text-coral-500"}>{c.ok ? "✓" : "✗"} {c.check}{c.detail ? ` · ${c.detail}` : ""}</li>)}
          </ul>
        </div>
      )}
    </Card>
  );
}

export function ExportCard() {
  const [tables] = usePoll<{ table: string; kind: string; rows: number }[]>("/warehouse/schema", 60000);
  const [error, setError] = useState("");
  const mine = (tables ?? []).filter((t) => t.kind !== "system");
  const download = (t: string, f: string) => base64Download(`/access/export/${t}?format=${f}`).catch((e) => setError(errText(e)));
  if (!mine.length) return null;
  return (
    <Card title="Export tables">
      <ErrorNote error={error} onClose={() => setError("")} />
      <div className="divide-y divide-line text-sm">
        {mine.map((t) => (
          <div key={t.table} className="flex items-center gap-2 py-1.5">
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{t.table}</span>
            <span className="text-xs text-text-muted">{t.rows} rows</span>
            {["csv", "json", "jsonl"].map((f) => <button key={f} className="btn-ghost py-1 text-xs" onClick={() => download(t.table, f)}>{f.toUpperCase()}</button>)}
          </div>
        ))}
      </div>
    </Card>
  );
}
