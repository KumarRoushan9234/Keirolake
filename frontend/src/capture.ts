/**
 * Capture from any device: voice, photos, files, links and text go to the lake's "Captured" collection.
 *
 * - Sending: each capture is a background job in the engine, so the phone can lock right after.
 * - Offline queue: with no connection, captures wait in this device's IndexedDB and are sent when the lake is
 *   reachable again (also used for files shared from other apps, which the service worker drops in the queue).
 * - Voice: recorded in the browser and turned into text on the lake's computer, never by an online service.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api, Job, waitJob } from "./api";
import { notify, trackJob } from "./background";

export type Item = { id: string; kind: "audio" | "image" | "file" | "text"; name: string; blob?: Blob; note?: string; text?: string; created: number };

// ---------- the queue (IndexedDB "keirolake", store "queue"; sw.js writes shared items to the same store) ----------
const DB = "keirolake", STORE = "queue";
function db(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => { if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE, { keyPath: "id" }); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const d = await db();
  return new Promise((resolve, reject) => {
    const r = fn(d.transaction(STORE, mode).objectStore(STORE));
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
const listQueue = () => tx<Item[]>("readonly", (s) => s.getAll() as IDBRequest<Item[]>).catch(() => [] as Item[]);
const putQueue = (item: Item) => tx("readwrite", (s) => s.put(item));
const dropQueue = (id: string) => tx("readwrite", (s) => s.delete(id));

let queued: Item[] = [];
const listeners = new Set<() => void>();
async function refresh() { queued = await listQueue(); listeners.forEach((l) => l()); }
export const useQueue = () => useSyncExternalStore((l) => { listeners.add(l); return () => { listeners.delete(l); }; }, () => queued);

// ---------- sending ----------
function form(fields: Record<string, string | Blob>, fileName?: string) {
  const f = new FormData();
  for (const [k, v] of Object.entries(fields)) v instanceof Blob ? f.append(k, v, fileName ?? "upload") : f.append(k, v);
  return f;
}

async function send(item: Item): Promise<string> {
  let r: { job_id: string };
  if (item.kind === "text") r = await api("/capture/text", { method: "POST", json: { text: item.text ?? "", title: item.note ?? "" } });
  else if (item.kind === "audio") r = await api("/capture/audio", { method: "POST", body: form({ file: item.blob!, mode: "note" }, item.name) });
  else if (item.kind === "image") r = await api("/capture/image", { method: "POST", body: form({ file: item.blob!, note: item.note ?? "" }, item.name) });
  else r = await api("/capture/file", { method: "POST", body: form({ files: item.blob! }, item.name) });
  trackJob(r.job_id);
  return r.job_id;
}

const unreachable = (e: unknown) => e instanceof TypeError || /Failed to fetch|NetworkError|Load failed|502|503|504/i.test(String(e));

/** Send now, or keep it on this device until the lake can be reached. */
export async function capture(item: Omit<Item, "id" | "created">): Promise<"sent" | "queued"> {
  const full: Item = { ...item, id: crypto.randomUUID(), created: Date.now() };
  try {
    await send(full);
    return "sent";
  } catch (e) {
    if (!unreachable(e)) throw e;
    await putQueue(full);
    await refresh();
    notify({ title: "Saved on this device", body: "It goes to your lake as soon as it can be reached.", tone: "info" });
    return "queued";
  }
}

let flushing = false;
/** Send everything waiting in the queue (called on start, when the connection returns, and every 30 s). */
export async function flush(): Promise<number> {
  if (flushing) return 0;
  flushing = true;
  let sent = 0;
  try {
    for (const item of await listQueue()) {
      try { await send(item); await dropQueue(item.id); sent++; }
      catch (e) { if (unreachable(e)) break; await dropQueue(item.id); notify({ title: `Couldn't save ${item.name}`, body: String(e), tone: "failed" }); }
    }
  } finally {
    flushing = false;
    await refresh();
  }
  if (sent) notify({ title: `Sent ${sent} saved capture${sent === 1 ? "" : "s"} to your lake`, tone: "done", action: { label: "Open", go: { page: "Collections" } } });
  return sent;
}

/** Keeps the queue moving for the whole app. */
export function useQueueFlusher() {
  useEffect(() => {
    refresh().then(() => queued.length && flush());
    const on = () => flush();
    window.addEventListener("online", on);
    const t = window.setInterval(() => { if (queued.length) flush(); }, 30000);
    return () => { window.removeEventListener("online", on); window.clearInterval(t); };
  }, []);
}

// ---------- voice ----------
export type Speech = { available: boolean; model: string; downloaded: boolean };
let speechStatus: Promise<{ speech: Speech; vision_model: string | null } | null> | null = null;
export const captureStatus = () => (speechStatus ??= api<{ speech: Speech; vision_model: string | null }>("/capture/status").catch(() => null));
export const canRecord = () => typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";

/** Record from the microphone. stop() resolves with the recording. */
export function useRecorder() {
  const [state, setState] = useState<"idle" | "recording" | "busy">("idle");
  const [seconds, setSeconds] = useState(0);
  const rec = useRef<{ r: MediaRecorder; chunks: Blob[]; stream: MediaStream; timer: number } | null>(null);
  const start = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported?.(t)) ?? "";
    const r = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks: Blob[] = [];
    r.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    r.start(1000);
    setSeconds(0);
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    rec.current = { r, chunks, stream, timer };
    setState("recording");
  }, []);
  const stop = useCallback(() => new Promise<{ blob: Blob; name: string } | null>((resolve) => {
    const cur = rec.current;
    if (!cur) return resolve(null);
    cur.r.onstop = () => {
      cur.stream.getTracks().forEach((t) => t.stop());
      window.clearInterval(cur.timer);
      rec.current = null;
      setState("idle");
      const type = cur.r.mimeType || "audio/webm";
      resolve({ blob: new Blob(cur.chunks, { type }), name: `voice.${type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm"}` });
    };
    cur.r.stop();
  }), []);
  useEffect(() => () => { rec.current?.stream.getTracks().forEach((t) => t.stop()); }, []);
  return { state, setState, seconds, start, stop };
}

/** Text of a recording, transcribed on the lake's computer (nothing is kept). */
export async function transcribe(blob: Blob, name: string, onUpdate?: (j: Job) => void): Promise<string> {
  const { job_id } = await api<{ job_id: string }>("/capture/audio", { method: "POST", body: form({ file: blob, mode: "transcribe" }, name) });
  const job = await waitJob(job_id, onUpdate);
  return String(job.result?.text ?? "");
}

/** Items the service worker received from the share sheet ("Share to KeiroLake"), sent now. */
export async function sendShared(): Promise<number> {
  await refresh();
  return flush();
}
