import { ArrowRight, CheckCircle, Cpu, Database, Globe, Key, Lightning, Plugs, Sparkle, Table as TableIcon, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { api, errText, Job } from "../api";
import Share from "../pages/settings/Share";
import { ErrorNote, Progress, usePoll } from "../ui";

const STEPS = ["Welcome", "This computer", "Web access", "First topic", "Other devices", "Ready"] as const;

type Hw = { hardware: { cpu: string; ram_gb: number; vram_gb: number; gpus: { name: string }[]; disk_free_gb: number };
  recommendation: { llm: { id: string; label: string; size_gb: number; note: string }; embedder: { id: string; label: string; size_gb: number } } };
type Status = { ollama: { running: boolean; installed: boolean; install_url: string; models: { name: string; local: boolean }[] }; local_model: string };

/** First-run guide: what KeiroLake is, then the few setup choices that matter, each one optional. Reopen it any time. */
export default function Welcome({ onClose }: { onClose: (finished: boolean) => void }) {
  const [step, setStep] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => { root.current?.querySelector<HTMLElement>("h2")?.focus(); }, [step]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const last = step === STEPS.length - 1;
  const next = () => (last ? onClose(true) : setStep(step + 1));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-3 sm:p-6" role="dialog" aria-modal="true" aria-label="Welcome to KeiroLake">
      <div ref={root} className="flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-line bg-surface-primary shadow-xl">
        <header className="flex items-center gap-3 border-b border-line px-5 py-3">
          <img src="/keirolabs.png" alt="" className="h-6 w-6" />
          <div className="min-w-0 flex-1">
            <p className="text-xs text-text-muted">Step {step + 1} of {STEPS.length}</p>
            <div className="mt-1 flex gap-1" aria-hidden="true">{STEPS.map((s, i) => <span key={s} className={`h-1 flex-1 rounded-full ${i <= step ? "bg-accent-600" : "bg-surface-tertiary"}`} />)}</div>
          </div>
          <button className="icon-btn" aria-label="Skip the guide" onClick={() => onClose(false)}><X size={16} /></button>
        </header>

        <div className="min-h-[340px] flex-1 overflow-y-auto px-5 py-5">
          {step === 0 && <Intro />}
          {step === 1 && <Computer />}
          {step === 2 && <WebAccess />}
          {step === 3 && <FirstTopic />}
          {step === 4 && <><h2 tabIndex={-1} className="text-xl outline-none">Use your lake from other devices</h2><p className="mb-3 mt-1 text-sm text-text-secondary">Optional. Do it now or later in Settings.</p><Share compact /></>}
          {step === 5 && <Ready />}
        </div>

        <footer className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
          <button className="btn-ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</button>
          <div className="flex items-center gap-2">
            {!last && step > 0 && <button className="btn-ghost" onClick={next}>Skip this step</button>}
            <button className="btn-primary" onClick={next}>{last ? "Start using KeiroLake" : step === 0 ? "Get started" : "Continue"}<ArrowRight size={14} /></button>
          </div>
        </footer>
      </div>
    </div>
  );
}

function Heading({ children, sub }: { children: string; sub?: string }) {
  return <><h2 tabIndex={-1} className="text-xl outline-none">{children}</h2>{sub && <p className="mb-4 mt-1 text-sm text-text-secondary">{sub}</p>}</>;
}

function Intro() {
  const items = [
    { icon: Globe, title: "Collect", text: "Name a topic and KeiroLake reads the web for you, or add files, feeds and APIs. Every web call uses zero data retention." },
    { icon: Database, title: "Hold", text: "Facts land in clean tables you can edit and chart. Pages stay searchable. Everything lives on this computer." },
    { icon: Lightning, title: "Use", text: "Ask questions, open the map, export CSV, or let your other apps and devices read it through a private link." },
  ];
  return (
    <>
      <Heading sub="A private data lake that runs on your computer. It uses local AI models first and only reaches for the cloud when you allow it.">Welcome to KeiroLake</Heading>
      <div className="grid gap-3 sm:grid-cols-3">
        {items.map(({ icon: I, title, text }) => (
          <div key={title} className="rounded-md border border-line p-3"><I size={22} className="text-accent-600" /><p className="mt-2 font-semibold">{title}</p><p className="mt-1 text-sm text-text-secondary">{text}</p></div>
        ))}
      </div>
      <p className="mt-4 text-sm text-text-secondary">This guide takes about two minutes. Every step can be skipped and changed later.</p>
    </>
  );
}

function Computer() {
  const [hw] = usePoll<Hw>("/local/hardware", 60000);
  const [status, reload] = usePoll<Status>("/local/status", 3000);
  const [jobs] = usePoll<Job[]>("/jobs?limit=20", 2000);
  const [error, setError] = useState("");
  const rec = hw?.recommendation, h = hw?.hardware;
  const have = (id: string) => !!status?.ollama.models.some((m) => m.local && (m.name === id || m.name.replace(/:latest$/, "") === id));
  const pulling = (id: string) => (jobs ?? []).find((j) => j.title === `Download ${id}` && (j.status === "running" || j.status === "queued"));
  const pull = async (model: string) => { setError(""); try { await api("/local/ollama/pull", { method: "POST", json: { model } }); } catch (e) { setError(errText(e)); } reload(); };
  const item = (id: string, label: string, size: number, why: string) => {
    const job = pulling(id);
    return (
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-line p-3">
        <div className="min-w-0 flex-1"><p className="font-medium">{label}</p><p className="text-xs text-text-muted">{size} GB · {why}</p></div>
        {job ? <div className="w-44"><Progress value={job.progress} label={job.message || "Queued"} /></div> : have(id) ? <span className="badge-ok"><CheckCircle size={12} className="mr-1" />Installed</span>
          : <button className="btn-secondary" disabled={!status?.ollama.running} onClick={() => pull(id)}>Install</button>}
      </div>
    );
  };
  return (
    <>
      <Heading sub="Local models answer questions and read pages without sending your data anywhere.">Set up on this computer</Heading>
      {!hw ? <div className="skeleton h-24" /> : (
        <p className="mb-3 flex items-center gap-2 text-sm text-text-secondary"><Cpu size={16} className="text-accent-600" />{h!.ram_gb} GB memory, {h!.gpus.length ? `${h!.gpus[0].name} (${h!.vram_gb} GB)` : "no dedicated graphics"}, {h!.disk_free_gb} GB free.</p>
      )}
      {status && !status.ollama.running && (
        <div className="field-error mb-3">{status.ollama.installed ? "The local model runtime (Ollama) is stopped."
          : <>The local model runtime (Ollama) isn't installed. <a className="font-semibold underline" href={status.ollama.install_url} target="_blank" rel="noreferrer">Get it here</a>, then come back.</>}
          {status.ollama.installed && <button className="btn-secondary ml-2" onClick={async () => { await api("/local/ollama/start", { method: "POST" }).catch((e) => setError(errText(e))); reload(); }}>Start it</button>}</div>
      )}
      {rec && <div className="space-y-2">{item(rec.llm.id, rec.llm.label, rec.llm.size_gb, rec.llm.note)}{item(rec.embedder.id, rec.embedder.label, rec.embedder.size_gb, "lets you search by meaning")}</div>}
      <ErrorNote error={error} onClose={() => setError("")} />
      <p className="mt-3 text-xs text-text-muted">Downloads continue in the background if you move on.</p>
    </>
  );
}

function WebAccess() {
  const [key, setKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [s] = usePoll<{ keiro_configured?: boolean; values?: Record<string, { set: boolean }> }>("/settings", 5000);
  const has = saved || !!s?.values?.KEIROLABS_SEARCH_LITE_API_KEY?.set;
  const save = async () => { setError(""); try { await api("/settings", { method: "PUT", json: { values: { KEIROLABS_SEARCH_LITE_API_KEY: key.trim() } } }); setSaved(true); setKey(""); } catch (e) { setError(errText(e)); } };
  return (
    <>
      <Heading sub="KeiroLabs lets KeiroLake search and read the web for your topics. Without a key everything else still works.">Web access</Heading>
      {has ? <p className="field-ok flex items-center gap-2"><CheckCircle size={16} />A KeiroLabs key is saved on this computer.</p> : (
        <div className="space-y-2">
          <label htmlFor="wk" className="label">KeiroLabs API key</label>
          <div className="flex gap-2"><input id="wk" className="input-field mono min-w-0 flex-1" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Paste your key" />
            <button className="btn-primary" disabled={key.trim().length < 8} onClick={save}><Key size={14} />Save</button></div>
          <a className="text-sm text-accent-600 hover:underline" href="https://platform.keirolabs.cloud" target="_blank" rel="noreferrer">Get a key</a>
        </div>
      )}
      <ErrorNote error={error} onClose={() => setError("")} />
      <ul className="mt-4 space-y-1 text-sm text-text-secondary">
        <li className="flex gap-2"><Sparkle size={14} className="mt-0.5 shrink-0 text-accent-600" />Zero data retention is on for every web call.</li>
        <li className="flex gap-2"><Sparkle size={14} className="mt-0.5 shrink-0 text-accent-600" />Cloud AI models stay off unless you add a key for one, and have a daily limit.</li>
      </ul>
    </>
  );
}

function FirstTopic() {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState("");
  const [error, setError] = useState("");
  const [topics] = usePoll<{ id: string; name: string; status: string }[]>(made ? "/topics" : null, 3000);
  const [jobs] = usePoll<Job[]>(made ? "/jobs?limit=10" : null, 1500);
  const job = (jobs ?? []).find((j) => j.title === `Topic: ${made}`);
  const topic = topics?.find((t) => t.name === made);
  const create = async () => { setBusy(true); setError(""); try { await api("/topics", { method: "POST", json: { name: name.trim() } }); setMade(name.trim()); } catch (e) { setError(errText(e)); } setBusy(false); };
  return (
    <>
      <Heading sub="Try it now: name any subject. KeiroLake finds pages, builds a clean table, and keeps the text searchable.">Build your first topic</Heading>
      {!made ? (
        <div className="flex gap-2"><input aria-label="Topic" className="input-field min-w-0 flex-1" value={name} onChange={(e) => setName(e.target.value)} placeholder="For example: Solar panel prices in India" onKeyDown={(e) => e.key === "Enter" && name.trim() && create()} />
          <button className="btn-primary" disabled={busy || !name.trim()} onClick={create}><TableIcon size={14} />Build</button></div>
      ) : (
        <div className="space-y-2" role="status">
          <p className="text-sm font-medium">{made}</p>
          {topic?.status === "ready" ? <p className="field-ok flex items-center gap-2"><CheckCircle size={16} />Ready. Find it in Lake, under Topics.</p>
            : topic?.status === "error" ? <p className="field-error">It didn't finish. Open Lake, Topics to see why and try again.</p>
              : <Progress value={job?.progress ?? 0.02} label={job?.message || "Starting"} />}
          <p className="text-xs text-text-muted">You can continue while it builds.</p>
        </div>
      )}
      <ErrorNote error={error} onClose={() => setError("")} />
    </>
  );
}

function Ready() {
  const tips = [
    [TableIcon, "Lake", "Your topics, sheets and collections. Open a sheet to edit, clean, chart and export it."],
    [Plugs, "Map", "See how everything connects, fix problems, and add connectors for APIs, feeds and folders."],
    [Cpu, "Models", "Install models at the precision you want, or run Heretic on one."],
    [Key, "Connect", "Give your apps a key so they can read the lake."],
  ] as const;
  return (
    <>
      <Heading sub="You can reopen this guide from Troubleshoot at any time, or press Ctrl K to find anything.">You're ready</Heading>
      <div className="grid gap-3 sm:grid-cols-2">
        {tips.map(([I, t, d]) => <div key={t} className="flex gap-3 rounded-md border border-line p-3"><I size={20} className="mt-0.5 shrink-0 text-accent-600" /><div><p className="font-semibold">{t}</p><p className="text-sm text-text-secondary">{d}</p></div></div>)}
      </div>
    </>
  );
}
