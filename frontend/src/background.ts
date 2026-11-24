/**
 * Work that keeps going when you switch pages.
 *
 * - Tasks: a request started from a page (ask a sheet, plan a table, try the gateway...) runs here, outside React, so
 *   leaving the page doesn't drop it. Coming back shows the result; if nobody is looking when it ends, a notice says so.
 * - Jobs: one shared watcher polls the engine's job list for every page, and announces jobs that finish.
 * - Notices: small messages in the corner, with an optional action (for example, open the table).
 */
import { useEffect, useSyncExternalStore } from "react";
import { api, errText, Job } from "./api";
import type { NavTarget } from "./ui";

type Listener = () => void;
function store<T>(initial: T) {
  let value = initial;
  const listeners = new Set<Listener>();
  return {
    get: () => value,
    set(next: T) { value = next; listeners.forEach((l) => l()); },
    subscribe(l: Listener) { listeners.add(l); return () => { listeners.delete(l); }; },
  };
}

// ---------- notices ----------
export type Notice = { id: number; title: string; body?: string; tone: "done" | "failed" | "info"; action?: { label: string; go: NavTarget } };
const notices = store<Notice[]>([]);
let noticeId = 0;

export function notify(n: Omit<Notice, "id">, ms = 9000) {
  const id = ++noticeId;
  notices.set([...notices.get(), { ...n, id }].slice(-4));
  window.setTimeout(() => dismiss(id), ms);
}
export const dismiss = (id: number) => notices.set(notices.get().filter((n) => n.id !== id));
export const useNotices = () => useSyncExternalStore(notices.subscribe, notices.get);

// ---------- tasks ----------
export type Task<T = any> = { status: "idle" | "running" | "done" | "failed"; label: string; result?: T; error?: string };
const IDLE: Task = { status: "idle", label: "" };
const tasks = store<Record<string, Task>>(readSaved());
const watchers = new Map<string, number>();  // how many pages are showing each task right now

function readSaved(): Record<string, Task> {
  try {
    const saved = JSON.parse(sessionStorage.getItem("tasks") ?? "{}") as Record<string, Task>;
    // A request can't survive a reload: keep finished results, drop ones that were still running.
    return Object.fromEntries(Object.entries(saved).filter(([, t]) => t.status === "done" || t.status === "failed"));
  } catch { return {}; }
}
function save() {
  try { sessionStorage.setItem("tasks", JSON.stringify(tasks.get()).length < 400_000 ? JSON.stringify(tasks.get()) : "{}"); } catch { /* full or blocked */ }
}
function put(key: string, t: Task) { tasks.set({ ...tasks.get(), [key]: t }); save(); }

/** Start (or restart) a task. Resolves with its result, or undefined when it failed. */
export async function runTask<T>(key: string, label: string, fn: () => Promise<T>, open?: NavTarget): Promise<T | undefined> {
  if (tasks.get()[key]?.status === "running") return undefined;
  put(key, { status: "running", label });
  try {
    const result = await fn();
    put(key, { status: "done", label, result });
    if (!watchers.get(key)) notify({ title: `Ready: ${label}`, tone: "done", action: open && { label: "Open", go: open } });
    return result;
  } catch (e) {
    put(key, { status: "failed", label, error: errText(e) });
    if (!watchers.get(key)) notify({ title: `Didn't finish: ${label}`, body: errText(e), tone: "failed", action: open && { label: "Open", go: open } });
    return undefined;
  }
}

export function clearTask(key: string) {
  const next = { ...tasks.get() };
  delete next[key];
  tasks.set(next); save();
}

/** A task's state for this page. While the page shows it, finishing doesn't raise a notice. */
export function useTask<T = any>(key: string): Task<T> {
  useEffect(() => {
    watchers.set(key, (watchers.get(key) ?? 0) + 1);
    return () => { watchers.set(key, (watchers.get(key) ?? 1) - 1); };
  }, [key]);
  return useSyncExternalStore(tasks.subscribe, () => tasks.get()[key] ?? IDLE) as Task<T>;
}

export const useRunningTasks = () => {
  const all = useSyncExternalStore(tasks.subscribe, tasks.get);
  return Object.entries(all).filter(([, t]) => t.status === "running").map(([key, t]) => ({ key, ...t }));
};

// ---------- jobs ----------
export type JobRow = Job & { title: string; kind?: string; created_at?: string };
const jobs = store<JobRow[]>([]);
const seenRunning = new Set<string>();
let polling = 0;
let timer = 0;

async function pollJobs() {
  try {
    const list = await api<JobRow[]>("/jobs?limit=20");
    for (const j of list) {
      const active = j.status === "running" || j.status === "queued";
      if (active) seenRunning.add(j.id);
      else if (seenRunning.delete(j.id)) announce(j);
    }
    jobs.set(list);
  } catch { /* engine restarting: try again next tick */ }
  timer = window.setTimeout(pollJobs, seenRunning.size ? 2000 : 5000);
}

// The page on screen: a job whose result is shown right there needs no notice.
let activePage = "";
export function setActivePage(page: string) { activePage = page; }

/** Counts finished jobs, so lists can refresh the moment something new lands in the lake. */
const finished = store(0);
export const useFinishedJobs = () => useSyncExternalStore(finished.subscribe, finished.get);

function announce(j: JobRow) {
  finished.set(finished.get() + 1);
  const r = j.result ?? {};
  const go: NavTarget | undefined = r.session ? { page: "Builder", params: { session: r.session } }
    : r.sheet_id ? { page: "Tables", params: { sheet: r.sheet_id } }
    : r.kb_id ? { page: "Collections" } : { page: "Jobs" };
  if (j.status === "done" && go.page === activePage) return;
  if (j.status === "done") notify({ title: `Done: ${j.title}`, body: j.message || undefined, tone: "done", action: { label: "Open", go } });
  else if (j.status === "failed") notify({ title: `Failed: ${j.title}`, body: j.message || undefined, tone: "failed", action: { label: "Details", go: { page: "Jobs" } } });
}

/** Every job the engine knows about, refreshed for all pages by one poller. */
export function useJobs(): JobRow[] {
  useEffect(() => {
    if (polling++ === 0) pollJobs();
    return () => { if (--polling === 0) window.clearTimeout(timer); };
  }, []);
  return useSyncExternalStore(jobs.subscribe, jobs.get);
}

/** Tell the watcher about a job started right now, so its end is announced even if it finishes before the next poll. */
export function trackJob(id: string) { seenRunning.add(id); }
