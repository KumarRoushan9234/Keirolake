import { useEffect, useState } from "react";
import { api, errText, isDesktop, shell } from "../api";
import { Accent, ACCENTS, SIZES, ThemeChoice, useDisplay } from "../display";
import { UpdatesCard } from "../shell/Updates";
import { Card, ErrorNote, PageHeader, useKept, useNav, usePoll } from "../ui";
import { BackupsCard, ExportCard, Net, NetworkCard } from "./Connect";

const SECTIONS = [["general", "General"], ["data", "Data and storage"], ["advanced", "Advanced"]] as const;

function Row({ label, children, hint }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line py-3 last:border-0">
      <span className="min-w-0"><span className="block text-sm font-medium">{label}</span>{hint && <span className="block text-xs text-text-muted">{hint}</span>}</span>
      {children}
    </div>
  );
}

/** Settings with a section list, like Docker Desktop's. Changes apply at once. */
export default function Settings() {
  const { params } = useNav();
  const [section, setSection] = useKept("settings:section", params.section ?? "general");
  const current = SECTIONS.some(([id]) => id === (params.section ?? section)) ? (params.section ?? section) : "general";
  return (
    <div className="space-y-4">
      <PageHeader title="Settings" />
      <div className="grid gap-4 md:grid-cols-[12rem_1fr]">
        <nav aria-label="Settings sections" className="flex gap-1 overflow-x-auto md:flex-col">
          {SECTIONS.map(([id, label]) => (
            <button key={id} className={current === id ? "nav-item-active" : "nav-item"} aria-current={current === id ? "page" : undefined}
              onClick={() => setSection(id)}>{label}</button>
          ))}
        </nav>
        <div className="min-w-0 space-y-4">
          {current === "general" && <><General /><UpdatesCard /></>}
          {current === "data" && <><SampleData /><BackupsCard /><ExportCard /></>}
          {current === "advanced" && <Advanced />}
        </div>
      </div>
    </div>
  );
}

function General() {
  const display = useDisplay();
  const [s, reload] = usePoll<any>("/settings", 30000);
  const setPref = (values: Record<string, string>) => api("/settings", { method: "PUT", json: { values } }).then(reload);
  return (
    <Card title="General">
      <Row label="Theme">
        <div className="seg" role="group" aria-label="Theme">
          {(["system", "light", "dark"] as ThemeChoice[]).map((t) => (
            <button key={t} aria-pressed={display.theme === t} onClick={() => display.set({ theme: t })}>{t === "system" ? "System" : t === "light" ? "Light" : "Dark"}</button>
          ))}
        </div>
      </Row>
      <Row label="Text size">
        <div className="seg" role="group" aria-label="Text size">
          {SIZES.map((v) => <button key={v} aria-pressed={display.size === v} onClick={() => display.set({ size: v })}>{Math.round(v * 100)}%</button>)}
        </div>
      </Row>
      <Row label="Accent colour">
        <div className="flex gap-2" role="radiogroup" aria-label="Accent colour">
          {(Object.keys(ACCENTS) as Accent[]).map((a) => (
            <button key={a} role="radio" aria-checked={display.accent === a} aria-label={a} title={a} onClick={() => display.set({ accent: a })}
              className={`h-6 w-6 rounded-full ring-offset-2 ring-offset-surface-primary ${display.accent === a ? "ring-2 ring-text-primary" : ""}`} style={{ background: ACCENTS[a] }} />
          ))}
        </div>
      </Row>
      <Row label="High contrast"><input type="checkbox" role="switch" className="accent-accent-400" aria-label="High contrast" checked={display.contrast} onChange={(e) => display.set({ contrast: e.target.checked })} /></Row>
      <Row label="Reduce motion"><input type="checkbox" role="switch" className="accent-accent-400" aria-label="Reduce motion" checked={display.calm} onChange={(e) => display.set({ calm: e.target.checked })} /></Row>
      {isDesktop && <Autostart />}
      <Row label="Offline mode" hint="Local models only, no web, scheduled refreshes paused">
        <input type="checkbox" role="switch" className="accent-accent-400" aria-label="Offline mode" checked={!!s?.offline}
          onChange={(e) => setPref({ OFFLINE_MODE: e.target.checked ? "1" : "", EMERGENCY_MODE: "" })} />
      </Row>
    </Card>
  );
}

/** Other devices: open in a browser here, the password they sign in with, and who is signed in. Shown on the Sharing page. */
export function Devices() {
  const [data, reload] = usePoll<{ network: Net }>("/access", 15000);
  const [auth, reloadAuth] = usePoll<{ password_set: boolean }>("/auth/status", 15000);
  const [devices, reloadDevices] = usePoll<{ id: string; device: string; client: string; last_seen: string }[]>("/auth/sessions", 15000);
  const [pw, setPw] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const act = async (fn: () => Promise<unknown>) => { setError(""); setNote(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); reloadAuth(); reloadDevices(); };
  const openBrowser = () => act(async () => { const { url } = await api<{ url: string }>("/auth/open-link", { method: "POST" }); window.open(url, "_blank", "noopener"); });
  return (
    <>
      <ErrorNote error={error} onClose={() => setError("")} />
      {note && <p className="field-ok">{note}</p>}
      <NetworkCard net={data?.network} act={act} />
      <Card title="Open KeiroLake in a browser" actions={<button className="btn-secondary" onClick={openBrowser}>Open in browser</button>}>
        <p className="text-sm text-text-secondary">The same app, in any browser on this PC, with no password. Other devices sign in with the password below once the data API is on for your home network.</p>
      </Card>
      <Card title="Password for other devices">
        <div className="flex flex-wrap gap-2">
          <input className="input-field min-w-0 flex-1" type="password" autoComplete="new-password" aria-label="New password" placeholder={auth?.password_set ? "New password (10+ characters)" : "Set a password (10+ characters)"}
            value={pw} onChange={(e) => setPw(e.target.value)} />
          <button className="btn-primary" disabled={pw.length < 10} onClick={() => act(async () => { await api("/auth/password", { method: "PUT", json: { password: pw } }); setPw(""); setNote("Password saved. Every device was signed out."); })}>
            {auth?.password_set ? "Change" : "Set password"}</button>
          {auth?.password_set && <button className="btn-ghost" onClick={() => confirm("Remove the password? Other devices will no longer be able to sign in.") && act(() => api("/auth/password", { method: "DELETE" }))}>Remove</button>}
        </div>
        <div className="mt-4">
          <div className="mb-1 flex items-center justify-between"><p className="text-sm font-medium">Signed-in devices</p>
            {!!devices?.length && <button className="btn-ghost py-1" onClick={() => act(() => api("/auth/sessions/sign-out-all", { method: "POST" }))}>Sign out everywhere</button>}</div>
          <div className="divide-y divide-line text-sm">
            {devices?.map((d) => (
              <div key={d.id} className="flex items-center gap-2 py-1.5">
                <span className="min-w-0 flex-1 truncate">{d.device}<span className="mono ml-2 text-text-muted">{d.client}</span></span>
                <span className="text-xs text-text-muted">{new Date(d.last_seen).toLocaleString()}</span>
                <button className="btn-ghost py-1" onClick={() => act(() => api(`/auth/sessions/${d.id}`, { method: "DELETE" }))}>Sign out</button>
              </div>
            ))}
            {devices && !devices.length && <p className="py-1.5 text-text-muted">No devices are signed in.</p>}
          </div>
        </div>
      </Card>
    </>
  );
}

function Advanced() {
  const [about] = usePoll<any>("/about", 60000);
  const [note, setNote] = useState("");
  const open = async (sub: string) => { try { await shell("open_data_folder", { sub }); } catch (e) { setNote(errText(e)); } };
  return (
    <Card title="Advanced">
      <Row label="Data folder" hint={about?.data_dir}>{isDesktop && <button className="btn-secondary" onClick={() => open("")}>Open</button>}</Row>
      <Row label="Logs">{isDesktop && <button className="btn-secondary" onClick={() => open("logs")}>Open</button>}</Row>
      <Row label="Vector store" hint={about?.vector_store}>{null}</Row>
      <Row label="Version" hint={about ? `${about.version} · ${about.os}` : ""}>{null}</Row>
      {note && <p className="field-error mt-2">{note}</p>}
    </Card>
  );
}

/** KeiroLake keeps running in the tray when the window is closed; this starts it when you sign in to the computer. */
function Autostart() {
  const [on, setOn] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { shell<boolean>("autostart_get").then((v) => setOn(!!v)).catch(() => setOn(false)); }, []);
  return (
    <Row label="Start at login" hint="Runs in the tray, so refreshes and the data API keep working when the window is closed">
      <span className="flex items-center gap-2">
        {error && <span className="text-xs text-coral-500">{error}</span>}
        <input type="checkbox" role="switch" className="accent-accent-400" aria-label="Start at login" checked={!!on} disabled={on === null}
          onChange={async (e) => { setError(""); try { await shell("autostart_set", { enabled: e.target.checked }); setOn(e.target.checked); } catch (err) { setError(errText(err)); } }} />
      </span>
    </Row>
  );
}

/** The sample table and guide a fresh install starts with: remove them, or put them back. */
function SampleData() {
  const [st, reload] = usePoll<{ present: boolean; table_id: string | null; collection_id: string | null }>("/samples", 15000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const act = async (method: "POST" | "DELETE") => {
    if (method === "DELETE" && !confirm("Remove the sample table, its saved view and the guide collection?")) return;
    setBusy(true); setError("");
    try { await api("/samples", { method }); } catch (e) { setError(errText(e)); }
    setBusy(false); reload();
  };
  return (
    <Card title="Sample data" actions={st && (st.present
      ? <button className="btn-secondary" disabled={busy} onClick={() => act("DELETE")}>Remove samples</button>
      : <button className="btn-secondary" disabled={busy} onClick={() => act("POST")}>Add samples</button>)}>
      <p className="text-sm text-text-secondary">A World countries table (World Bank, CC BY 4.0) with a saved view, and a short KeiroLake guide you can ask questions about. {st?.present ? "They are in your lake now." : "They are not in your lake."}</p>
      <ErrorNote error={error} onClose={() => setError("")} />
    </Card>
  );
}
