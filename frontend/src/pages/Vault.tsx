import { LockKey, LockKeyOpen, Plus, Trash } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errText } from "../api";
import { Card, ErrorNote } from "../ui";

type Status = { exists: boolean; unlocked: boolean; items: number; idle_minutes: number };
type Item = { id: string; title: string; chars: number };

export default function Vault() {
  const [status, setStatus] = useState<Status | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [pass, setPass] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<{ id?: string; title: string; text: string } | null>(null);

  const load = useCallback(async () => {
    const s = await api<Status>("/vault");
    setStatus(s);
    setItems(s.unlocked ? await api<Item[]>("/vault/items").catch(() => []) : []);
  }, []);
  useEffect(() => { load(); }, [load]);

  const guard = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); } catch (e) { setError(errText(e)); }
    load();
  };
  const submit = () => guard(async () => {
    await api(status?.exists ? "/vault/unlock" : "/vault/init", { method: "POST", json: { passphrase: pass } });
    setPass("");
  });
  const open = (id: string) => guard(async () => setEditing(await api(`/vault/items/${id}`)));
  const save = () => guard(async () => { await api("/vault/items", { method: "POST", json: editing }); setEditing(null); });

  if (!status) return <div className="skeleton h-32" />;

  if (!status.unlocked) {
    return (
      <Card title={<span className="flex items-center gap-2"><LockKey size={16} className="text-accent-400" />Private vault</span>}>
        <p className="mb-4 max-w-xl text-sm text-text-secondary">
          Keep IDs, medical details, contacts and home notes encrypted on this computer. Nothing is readable without your passphrase,
          and unlocked notes are only ever shown to models running on this computer, never to cloud models.
          {!status.exists && " There is no recovery if you forget the passphrase."}
        </p>
        <form className="flex max-w-md gap-2" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <input className="input-field flex-1" type="password" autoComplete="off" placeholder={status.exists ? "Passphrase" : "Choose a passphrase (8+ characters)"}
            value={pass} onChange={(e) => setPass(e.target.value)} />
          <button className="btn-primary" disabled={!pass}>{status.exists ? "Unlock" : "Create vault"}</button>
        </form>
        <div className="mt-3"><ErrorNote error={error} /></div>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onClose={() => setError("")} />
      <Card title={<span className="flex items-center gap-2"><LockKeyOpen size={16} className="text-sage-500" />Vault unlocked</span>}
        actions={<>
          <button className="btn-secondary" onClick={() => setEditing({ title: "", text: "" })}><Plus size={14} /> New note</button>
          <button className="btn-ghost" onClick={() => guard(async () => { await api("/vault/lock", { method: "POST" }); setEditing(null); })}>Lock</button>
        </>}>
        <p className="mb-3 text-xs text-text-muted">Locks by itself after {status.idle_minutes} idle minutes. In Ask, switch on "Private notes" to let a local model use these.</p>
        {editing ? (
          <div className="space-y-2">
            <input className="input-field" placeholder="Title (e.g. Passport, Allergies, Water shutoff)" value={editing.title} onChange={(e) => setEditing({ ...editing, title: e.target.value })} />
            <textarea className="input-field min-h-[160px]" placeholder="Details" value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} />
            <div className="flex gap-2">
              <button className="btn-primary" disabled={!editing.title.trim() || !editing.text.trim()} onClick={save}>Save encrypted</button>
              <button className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
            </div>
          </div>
        ) : items.length ? (
          <div className="divide-y divide-line">
            {items.map((i) => (
              <div key={i.id} className="flex items-center gap-3 py-2 text-sm">
                <button className="min-w-0 flex-1 truncate text-left hover:text-accent-600" onClick={() => open(i.id)}>{i.title}</button>
                <span className="text-xs text-text-muted">{i.chars} chars</span>
                <button className="icon-btn" aria-label={`Delete ${i.title}`} onClick={() => confirm(`Delete "${i.title}"?`) && guard(() => api(`/vault/items/${i.id}`, { method: "DELETE" }))}><Trash size={14} /></button>
              </div>
            ))}
          </div>
        ) : <p className="text-sm text-text-muted">No notes yet.</p>}
      </Card>
    </div>
  );
}
