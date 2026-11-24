import { ArrowClockwise, Broom, CheckCircle, DownloadSimple, FolderOpen, Keyboard, Lifebuoy, Question, Stethoscope, WarningCircle, XCircle } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { api, base64Download, errText, isDesktop, shell } from "../api";
import { lazy, Suspense } from "react";
import { Card, ErrorNote, PageHeader, useNav } from "../ui";
const StatusPanel = lazy(() => import("./Status"));

type Check = { id: string; label: string; status: "ok" | "warn" | "fail"; detail: string; fix: string };
type About = { version: string; python: string; frozen: boolean; os: string; data_dir: string; vector_store: string; mode: string; supervised: boolean;
  support: { keirolabs: string; ollama: string } };

const SHORTCUTS: [string, string][] = [
  ["Ctrl K", "Command palette: go anywhere, run actions"],
  ["Ctrl 1 – 9", "Jump to a page"],
  ["Ctrl + / Ctrl − / Ctrl 0", "Larger, smaller, reset text"],
  ["Ctrl B", "Collapse or expand the sidebar"],
  ["Enter / Shift Enter", "Send a question / new line in Ask"],
  ["Tab (first)", "Skip to content"],
];

const FAQ: [string, string][] = [
  ["Does it work without internet?", "Yes. Sheets, collections, offline libraries, the vault and a local model all work offline. Building sheets from the web, web columns and cloud models wait for the connection. Settings → Offline mode keeps everything on this computer."],
  ["Where is my data?", "On this computer, in the data folder (Settings → Advanced): the database, vectors, lake files, libraries, models and backups. Nothing leaves it except what you send to cloud models or KeiroLabs."],
  ["Which model answers?", "The model on this computer answers first. Cloud models are used only when it can't and \"Cloud models\" is on, up to the daily limit on the Models page. Vault notes only ever go to the local model."],
  ["Is KeiroLabs private?", "Every KeiroLabs request asks for zero data retention, and KeiroLabs confirms it on each answer. The status bar shows ZDR when it is on."],
  ["Can my own apps use this data?", "Yes. In Settings → Network, open the data API; in Connect, make a key with only the permissions an app needs and call /v1 or connect an AI tool through /mcp. Every request is logged."],
  ["How do I move to a new computer?", "Troubleshoot → Copy everything to a drive copies the database, vectors, lake and libraries. For regular safe copies, use Settings → Data & storage → Backups with a passphrase."],
  ["An answer looks wrong", "Check the sources it cites ([1] your data, [Z1] libraries, [W1] web). Anything marked 'From the model' has no source."],
];

export default function Help() {
  const { go } = useNav();
  const [about, setAbout] = useState<About | null>(null);
  const [checks, setChecks] = useState<Check[] | null>(null);
  const [running, setRunning] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { api<About>("/about").then(setAbout).catch((e) => setError(errText(e))); runChecks(false); }, []);

  async function runChecks(probe: boolean) {
    setRunning(true); setError("");
    try { setChecks(await api<Check[]>(`/support/diagnostics?probe=${probe}`)); } catch (e) { setError(errText(e)); }
    setRunning(false);
  }
  const act = async (fn: () => Promise<string>) => { setError(""); setNote(""); try { setNote(await fn()); } catch (e) { setError(errText(e)); } };
  const counts = checks ? { fail: checks.filter((c) => c.status === "fail").length, warn: checks.filter((c) => c.status === "warn").length } : null;

  return (
    <div className="space-y-5">
      <PageHeader title="Troubleshoot" />
      <ErrorNote error={error} onClose={() => setError("")} />
      {note && <p className="field-ok" role="status">{note}</p>}

      <Card title="Welcome guide" actions={<button className="btn-secondary" onClick={() => window.dispatchEvent(new Event("keirolake:welcome"))}>Open the guide</button>}>
        <p className="text-sm text-text-secondary">A two-minute tour of what KeiroLake does, with the setup choices that matter.</p>
      </Card>
      <Card title={<span className="flex items-center gap-2"><Stethoscope size={16} className="text-accent-400" />Health check</span>}
        actions={<>
          {counts && <span className={counts.fail ? "badge-bad" : counts.warn ? "badge-warn" : "badge-ok"}>{counts.fail ? `${counts.fail} problem${counts.fail > 1 ? "s" : ""}` : counts.warn ? `${counts.warn} to review` : "All good"}</span>}
          <button className="btn-ghost" disabled={running} onClick={() => runChecks(false)}><ArrowClockwise size={14} /> Recheck</button>
          <button className="btn-secondary" disabled={running} onClick={() => runChecks(true)} title="Also contacts KeiroLabs (uses a fraction of a credit)">Deep check</button>
        </>}>
        {!checks ? <div className="skeleton h-40" /> : (
          <ul className="divide-y divide-line" aria-live="polite">
            {checks.map((c) => (
              <li key={c.id} className="flex items-start gap-3 py-2.5">
                {c.status === "ok" ? <CheckCircle size={18} weight="fill" className="mt-0.5 shrink-0 text-sage-400" aria-label="OK" />
                  : c.status === "warn" ? <WarningCircle size={18} weight="fill" className="mt-0.5 shrink-0 text-cream-500" aria-label="Warning" />
                    : <XCircle size={18} weight="fill" className="mt-0.5 shrink-0 text-coral-400" aria-label="Problem" />}
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold">{c.label} <span className="font-normal text-text-secondary">· {c.detail}</span></p>
                  {c.fix && <p className="text-xs text-text-muted">{c.fix}</p>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Suspense fallback={<div className="skeleton h-40" />}><StatusPanel /></Suspense>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title={<span className="flex items-center gap-2"><Lifebuoy size={16} className="text-accent-400" />Get help</span>}>
          <div className="space-y-3 text-sm">
            <div>
              <button className="btn-primary" onClick={() => act(async () => { const name = await base64Download("/support/bundle"); return `Saved ${name}. Attach it to your support request.`; })}>
                <DownloadSimple size={16} /> Save a support bundle
              </button>
              <p className="mt-1.5 text-xs text-text-muted">Diagnostics, versions, settings and recent logs, with keys and tokens removed. No documents, notes or vault contents.</p>
            </div>
            <ul className="space-y-1.5">
              <li><a className="text-accent-600 underline underline-offset-2" href={about?.support.keirolabs} target="_blank" rel="noreferrer">KeiroLabs support and account</a> <span className="text-text-muted">· keys, credits, web search problems</span></li>
              <li><a className="text-accent-600 underline underline-offset-2" href={about?.support.ollama} target="_blank" rel="noreferrer">Ollama download and help</a> <span className="text-text-muted">· local model runtime</span></li>
            </ul>
          </div>
        </Card>

        <Card title={<span className="flex items-center gap-2"><Broom size={16} className="text-accent-400" />Maintenance</span>}>
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" onClick={() => act(async () => { const r = await api("/support/clear-caches", { method: "POST" }); return `Cleared ${r.web_results} cached web results and ${r.vectors} cached vectors.`; })}>Clear caches</button>
            {about?.supervised && (
              <button className="btn-secondary" onClick={() => act(async () => { await api("/support/restart", { method: "POST" }); await new Promise((r) => setTimeout(r, 4000)); return "The engine restarted."; })}>Restart engine</button>
            )}
            <button className="btn-ghost" onClick={() => { try { localStorage.removeItem("display"); } catch { /* ignore */ } location.reload(); }}>Reset display settings</button>
            {isDesktop && <button className="btn-ghost" onClick={() => shell("open_data_folder", { sub: "logs" })}><FolderOpen size={14} /> Open logs</button>}
            {isDesktop && <button className="btn-ghost" onClick={() => shell("open_data_folder", {})}><FolderOpen size={14} /> Open data folder</button>}
          </div>
        </Card>
      </div>

      <Card title={<span className="flex items-center gap-2"><Keyboard size={16} className="text-accent-400" />Keyboard</span>}>
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          {SHORTCUTS.map(([k, v]) => (
            <div key={k} className="flex items-baseline gap-3"><dt className="w-44 shrink-0"><kbd className="rounded-md border border-line bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">{k}</kbd></dt><dd className="text-text-secondary">{v}</dd></div>
          ))}
        </dl>
      </Card>

      <Card title={<span className="flex items-center gap-2"><Question size={16} className="text-accent-400" />Questions</span>}>
        <div className="divide-y divide-line">
          {FAQ.map(([q, a]) => (
            <details key={q} className="group py-2.5">
              <summary className="cursor-pointer list-none text-sm font-semibold marker:hidden">{q}</summary>
              <p className="mt-1.5 text-sm text-text-secondary">{a}</p>
            </details>
          ))}
        </div>
      </Card>

      {about && (
        <Card title="About">
          <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
            {([["Version", `KeiroLake ${about.version}`], ["System", about.os], ["Engine", `Python ${about.python}${about.frozen ? " (packaged)" : ""}`],
               ["Vector store", about.vector_store], ["Model mode", about.mode], ["Data folder", about.data_dir]] as const).map(([k, v]) => (
              <div key={k} className="flex gap-3"><dt className="w-28 shrink-0 text-text-muted">{k}</dt><dd className="min-w-0 break-all">{v}</dd></div>
            ))}
          </dl>
        </Card>
      )}
    </div>
  );
}
