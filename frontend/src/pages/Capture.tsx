import { Camera, CloudArrowUp, File as FileIcon, Microphone, PaperPlaneRight, Stop } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { errText } from "../api";
import { useJobs } from "../background";
import { canRecord, capture, captureStatus, flush, sendShared, Speech, useQueue, useRecorder } from "../capture";
import { Card, ErrorNote, PageHeader, Progress, useNav } from "../ui";

const KINDS = ["Voice note", "Photo", "Link", "Note", "Voice to text"];

/** One screen to put anything into the lake, made for a phone: speak, take a photo, share a file, or paste a link. */
export default function Capture() {
  const { params, go } = useNav();
  const [status, setStatus] = useState<{ speech: Speech; vision_model: string | null } | null>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const rec = useRecorder();
  const queue = useQueue();
  const jobs = useJobs().filter((j) => KINDS.includes(j.title) || j.title.startsWith("Shared ")).slice(0, 8);
  const photo = useRef<HTMLInputElement>(null);
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => { captureStatus().then(setStatus); }, []);
  useEffect(() => { if (params.shared) sendShared().then((n) => n && setDone(`Saved ${n} shared item${n === 1 ? "" : "s"}`)); }, [params.shared]);

  const run = async (fn: () => Promise<"sent" | "queued">, what: string) => {
    setError(""); setDone("");
    try { const r = await fn(); setDone(r === "sent" ? `${what} sent to your lake` : `${what} saved on this device until the lake can be reached`); return true; }
    catch (e) { setError(errText(e)); return false; }
  };

  const voice = async () => {
    if (rec.state === "recording") {
      const audio = await rec.stop();
      if (audio && audio.blob.size > 1200) await run(() => capture({ kind: "audio", name: audio.name, blob: audio.blob }), "Voice note");
      return;
    }
    setError("");
    try { await rec.start(); } catch (e) { setError(/denied|NotAllowed/i.test(String(e)) ? "Allow the microphone in your browser to record." : errText(e)); }
  };
  const sendPhoto = async (f?: File) => { if (f && await run(() => capture({ kind: "image", name: f.name || "photo.jpg", blob: f, note }), "Photo")) setNote(""); };
  const sendFile = async (f?: File) => { if (f) await run(() => capture({ kind: "file", name: f.name, blob: f }), f.name); };
  const sendText = async () => { if (text.trim() && await run(() => capture({ kind: "text", name: "Note", text }), /^https?:\/\//.test(text.trim()) ? "Link" : "Note")) setText(""); };

  const speechReady = status?.speech.available && canRecord();
  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <PageHeader title="Capture" />
      <ErrorNote error={error} onClose={() => setError("")} />
      {done && <p className="field-ok" role="status">{done}</p>}

      <div className="grid gap-3 sm:grid-cols-2">
        <button className={`card flex min-h-[9rem] flex-col items-center justify-center gap-2 text-center transition hover:border-accent-400 disabled:opacity-50 ${rec.state === "recording" ? "border-coral-200 bg-coral-50" : ""}`}
          disabled={!speechReady} onClick={voice} aria-label={rec.state === "recording" ? "Stop and save the voice note" : "Record a voice note"}>
          {rec.state === "recording" ? <Stop size={34} weight="fill" className="text-coral-500" /> : <Microphone size={34} className="text-accent-600" />}
          <span className="text-base font-semibold">{rec.state === "recording" ? `Recording… ${Math.floor(rec.seconds / 60)}:${String(rec.seconds % 60).padStart(2, "0")}` : "Voice note"}</span>
          <span className="text-xs text-text-muted">{!canRecord() ? "This browser can't record" : !status?.speech.available ? "Not available in this copy" : rec.state === "recording" ? "Tap to stop and save" : "Turned into text on your computer"}</span>
        </button>
        <button className="card flex min-h-[9rem] flex-col items-center justify-center gap-2 text-center transition hover:border-accent-400" onClick={() => photo.current?.click()} aria-label="Take or choose a photo">
          <Camera size={34} className="text-accent-600" />
          <span className="text-base font-semibold">Photo of a page</span>
          <span className="text-xs text-text-muted">{status?.vision_model ? `Text read by ${status.vision_model}` : "Kept as a photo; install a vision model to read its text"}</span>
        </button>
      </div>
      <input ref={photo} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { sendPhoto(e.target.files?.[0]); e.target.value = ""; }} />
      <input ref={file} type="file" hidden onChange={(e) => { sendFile(e.target.files?.[0]); e.target.value = ""; }} />

      <Card>
        <label htmlFor="cap-text" className="label">A link, a thought, or some text</label>
        <textarea id="cap-text" rows={3} className="input-field resize-y" value={text} placeholder="https://… or anything you want to remember"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendText(); }} />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button className="btn-primary" disabled={!text.trim()} onClick={sendText}><PaperPlaneRight size={15} /> Save</button>
          <button className="btn-secondary" onClick={() => file.current?.click()}><FileIcon size={15} /> Add a file</button>
        </div>
        <input aria-label="Note for the next photo" className="input-field mt-3" placeholder="Note to save with the next photo (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      </Card>

      {queue.length > 0 && (
        <Card title={<span className="flex items-center gap-2"><CloudArrowUp size={16} className="text-accent-600" />Waiting on this device ({queue.length})</span>}
          actions={<button className="btn-secondary" onClick={() => flush()}>Send now</button>}>
          <ul className="space-y-1 text-sm">{queue.map((q) => <li key={q.id} className="flex justify-between gap-2"><span className="truncate">{q.kind === "text" ? q.text?.slice(0, 60) : q.name}</span><span className="text-xs text-text-muted">{new Date(q.created).toLocaleTimeString()}</span></li>)}</ul>
        </Card>
      )}

      {jobs.length > 0 && (
        <Card title="Recent" actions={<button className="btn-ghost" onClick={() => go({ page: "Collections" })}>Open Captured</button>}>
          <ul className="space-y-2">{jobs.map((j) => (
            <li key={j.id} className="text-sm">
              <div className="flex items-center justify-between gap-2"><span className="truncate">{j.title}</span>
                <span className={`badge ${j.status === "done" ? "badge-ok" : j.status === "failed" ? "badge-bad" : "badge-warn"}`}>{j.status}</span></div>
              {(j.status === "running" || j.status === "queued") && <Progress value={j.progress} label={j.message || "Queued"} />}
              {j.status === "done" && j.result?.text && <p className="mt-0.5 line-clamp-2 text-xs text-text-muted">{j.result.text}</p>}
              {j.status === "failed" && <p className="mt-0.5 text-xs text-coral-500">{j.message}</p>}
            </li>))}</ul>
        </Card>
      )}
      <p className="text-center text-xs text-text-muted">On Android, share from any app to KeiroLake. Everything goes to the Captured collection.</p>
    </div>
  );
}
