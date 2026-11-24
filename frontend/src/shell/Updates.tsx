import { ArrowCircleUp, ArrowSquareOut, CheckCircle } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { api, errText, isDesktop, onShellEvent, shell, waitJob } from "../api";
import { notify } from "../background";
import { Card, ErrorNote, Progress } from "../ui";

/** What the desktop shell reports (signed updater) or the engine reports (GitHub Releases), in one shape. */
type Check = { current: string; available: boolean; version: string | null; notes: string | null; install?: string; url?: string; error?: string };
const RELEASES = "https://github.com/KumarRoushan9234/Keirolake/releases/latest";
const PREFS = "updates";
type Prefs = { auto: boolean; install: boolean; last: number };

function prefs(): Prefs {
  try { return { auto: true, install: false, last: 0, ...JSON.parse(localStorage.getItem(PREFS) ?? "{}") }; } catch { return { auto: true, install: false, last: 0 }; }
}
function savePrefs(p: Prefs) { try { localStorage.setItem(PREFS, JSON.stringify(p)); } catch { /* blocked */ } }

async function check(force = false): Promise<Check> {
  if (isDesktop) {
    try { return (await shell<Check>("update_check"))!; }
    catch (e) {  // no release yet, offline, or no updater manifest: fall back to what the engine can see
      const r = await api<Check>(`/updates?force=${force}`).catch(() => null);
      return r ?? { current: "", available: false, version: null, notes: null, error: errText(e) };
    }
  }
  return api<Check>(`/updates?force=${force}`);
}

async function install(onProgress: (f: number) => void): Promise<void> {
  const stop = await onShellEvent<[number, number | null]>("update-progress", ([got, total]) => onProgress(total ? got / total : 0.5));
  try { await shell("update_install"); } finally { stop(); }
}

/** Runs once per app start: checks GitHub for a newer version (at most every six hours) and says so, or installs it. */
export function UpdateWatcher() {
  useEffect(() => {
    const p = prefs();
    if (!p.auto || Date.now() - p.last < 6 * 3600_000) return;
    const t = window.setTimeout(async () => {
      const c = await check().catch(() => null);
      savePrefs({ ...prefs(), last: Date.now() });
      if (!c?.available) return;
      if (isDesktop && p.install && c.install !== "package") {
        notify({ title: `Installing KeiroLake ${c.version}`, body: "The app restarts by itself when it's done.", tone: "info" }, 20000);
        install(() => {}).catch((e) => notify({ title: "The update didn't install", body: errText(e), tone: "failed" }));
      } else {
        notify({ title: `KeiroLake ${c.version} is available`, body: `You have ${c.current}.`, tone: "info",
          action: { label: "Update", go: { page: "Settings", params: { section: "general" } } } }, 30000);
      }
    }, 15000);
    return () => window.clearTimeout(t);
  }, []);
  return null;
}

/** Settings → Updates: check now, install, and whether to check or install automatically. */
export function UpdatesCard() {
  const [c, setC] = useState<Check | null>(null);
  const [p, setP] = useState(prefs);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState("");
  const set = (patch: Partial<Prefs>) => { const next = { ...p, ...patch }; setP(next); savePrefs(next); };
  const run = async (force: boolean) => {
    setBusy(true); setError("");
    try { setC(await check(force)); } catch (e) { setError(errText(e)); }
    setBusy(false);
  };
  useEffect(() => { run(false); }, []);
  const [installed, setInstalled] = useState(false);
  const go = async () => {
    setError(""); setProgress(0);
    try { await install(setProgress); } catch (e) { setError(errText(e)); setProgress(null); }
  };
  /** .deb/.rpm: the engine downloads the package, checks its signature and installs it with the system's password prompt. */
  const goPackage = async () => {
    setError(""); setProgress(0);
    try {
      const { job_id } = await api<{ job_id: string }>("/updates/install", { method: "POST" });
      const job = await waitJob(job_id, (j) => setProgress(j.progress));
      setInstalled(!!job.result?.installed);
    } catch (e) { setError(errText(e)); }
    setProgress(null);
  };
  const selfUpdating = isDesktop && c?.install !== "package";
  const packaged = isDesktop && c?.install === "package";

  return (
    <Card title="Updates" actions={<button className="btn-secondary" disabled={busy || progress !== null} onClick={() => run(true)}>{busy ? "Checking…" : "Check now"}</button>}>
      <div className="space-y-3">
        {c && !c.available && !c.error && <p className="flex items-center gap-2 text-sm"><CheckCircle size={16} weight="fill" className="text-sage-500" /> KeiroLake {c.current} is the newest version.</p>}
        {c?.error && !c.available && <p className="text-sm text-text-muted">Couldn't check right now: {c.error}</p>}
        {c?.available && (
          <div className="rounded-md border border-accent-200 bg-accent-50 p-3">
            <p className="flex items-center gap-2 text-sm font-semibold"><ArrowCircleUp size={16} className="text-accent-600" /> KeiroLake {c.version} is available <span className="font-normal text-text-muted">(you have {c.current})</span></p>
            {c.notes && <p className="mt-2 line-clamp-6 whitespace-pre-wrap text-xs text-text-secondary">{c.notes}</p>}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {selfUpdating
                ? <button className="btn-primary" disabled={progress !== null} onClick={go}>Install and restart</button>
                : packaged
                  ? installed
                    ? <button className="btn-primary" onClick={() => shell("relaunch")}>Restart KeiroLake</button>
                    : <button className="btn-primary" disabled={progress !== null} onClick={goPackage}>Install update</button>
                  : <a className="btn-primary" href={c.url ?? RELEASES} target="_blank" rel="noreferrer">See the release <ArrowSquareOut size={14} /></a>}
            </div>
            {progress !== null && <div className="mt-3"><Progress value={progress} label={progress < 0.86 ? `Downloading ${Math.round(progress * 100)}%` : packaged ? "Checking and installing" : "Installing, the app will restart"} /></div>}
            {packaged && <p className="mt-2 text-xs text-text-muted">{installed ? "Installed. Restart to use the new version." : "Installed from a .deb or .rpm: the update goes through your package manager, so your system asks for your password."}</p>}
            {!isDesktop && <p className="mt-2 text-xs text-text-muted">Server: <span className="mono">docker compose pull && docker compose up -d</span>, or download the new release.</p>}
          </div>
        )}
        <ErrorNote error={error} onClose={() => setError("")} />
        <label className="flex items-center justify-between gap-3 text-sm">Check for updates when KeiroLake starts
          <input type="checkbox" role="switch" className="accent-accent-400" checked={p.auto} onChange={(e) => set({ auto: e.target.checked })} /></label>
        {isDesktop && (
          <label className="flex items-center justify-between gap-3 text-sm">Install updates automatically
            <input type="checkbox" role="switch" className="accent-accent-400" checked={p.install} disabled={!p.auto} onChange={(e) => set({ install: e.target.checked })} /></label>
        )}
        <p className="text-xs text-text-muted">Updates come from the project's GitHub Releases and are checked against a signature before they install. Nothing about you is sent.</p>
      </div>
    </Card>
  );
}
