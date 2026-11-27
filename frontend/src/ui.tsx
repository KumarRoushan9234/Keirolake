import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, errText, Job, waitJob } from "./api";

export type Page = "Home" | "Ask" | "Map" | "Topics" | "Tables" | "Collections" | "Files" | "DataTools" | "Builder" | "Capture" | "Libraries" | "Vault" | "Keys" | "Gateway" | "Sharing"
  | "Webhooks" | "Models" | "Jobs" | "Settings" | "Troubleshoot" | "Reader" | "AiApps" | "Docs";
export type NavTarget = { page: Page | string; params?: Record<string, string> };

export const NavContext = createContext<{
  go: (t: NavTarget) => void; params: Record<string, string>; online: boolean; setCrumb: (parts: string[]) => void;
}>({ go: () => {}, params: {}, online: true, setCrumb: () => {} });
export const useNav = () => useContext(NavContext);

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card min-w-0 animate-fade-up ${className}`}>
      {(title || actions) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-sm font-semibold text-text-primary">{title}</h2>}
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Progress({ value, label }: { value: number; label?: string }) {
  return (
    <div className="space-y-1">
      <div className="progress-bar"><div className="progress-fill" style={{ transform: `scaleX(${Math.max(0.02, Math.min(1, value))})` }} /></div>
      {label && <p className="truncate text-xs text-text-muted">{label}</p>}
    </div>
  );
}

export function ErrorNote({ error, onClose }: { error: string; onClose?: () => void }) {
  if (!error) return null;
  return (
    <div className="field-error flex items-start justify-between gap-3">
      <span className="min-w-0 break-words">{error}</span>
      {onClose && <button className="text-xs font-semibold" onClick={onClose}>Dismiss</button>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 py-12 text-center">
      <p className="text-base font-semibold text-text-primary">{title}</p>
      {children && <div className="text-sm text-text-muted">{children}</div>}
    </div>
  );
}

/** Pages shown inside another page (Lake tabs) get a section heading instead of a second page title. */
export const Embedded = createContext(false);

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  const embedded = useContext(Embedded);
  const Heading = embedded ? "h2" : "h1";
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <Heading className={embedded ? "text-base font-semibold" : "text-xl font-semibold"}>{title}</Heading>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

// Page state that outlives the page: kept in memory for this window and in sessionStorage for reloads.
const kept = new Map<string, unknown>();
const MAX_SAVED_CHARS = 200_000;

function readKept<T>(key: string, initial: T): T {
  if (kept.has(key)) return kept.get(key) as T;
  try {
    const raw = sessionStorage.getItem("kept:" + key);
    if (raw !== null) return JSON.parse(raw) as T;
  } catch { /* storage blocked or bad JSON */ }
  return initial;
}

function writeKept(key: string, value: unknown) {
  kept.set(key, value);
  try {
    const raw = JSON.stringify(value);
    if (raw.length <= MAX_SAVED_CHARS) sessionStorage.setItem("kept:" + key, raw);
    else sessionStorage.removeItem("kept:" + key);
  } catch { /* storage blocked or full */ }
}

/** useState that keeps its value when you switch pages and come back. */
export function useKept<T>(key: string, initial: T): [T, (next: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => readKept(key, initial));
  const set = useCallback((next: T | ((prev: T) => T)) => {
    setValue((prev) => {
      const v = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
      writeKept(key, v);
      return v;
    });
  }, [key]);
  return [value, set];
}

/** Run a job-returning call, track its progress and surface errors.
 *  With a `key`, a job that is still running when you come back to the page is picked up again. */
export function useJobRunner(onDone?: () => void, key?: string) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const done = useRef(onDone);
  done.current = onDone;

  const track = useCallback(async (jobId: string): Promise<boolean> => {
    let ok = true;
    if (key) writeKept("job:" + key, jobId);
    try {
      await waitJob(jobId, setJob);
    } catch (e) {
      setError(errText(e));
      ok = false;
    }
    if (key) writeKept("job:" + key, "");
    setJob(null);
    done.current?.();
    return ok;
  }, [key]);

  useEffect(() => {
    const running = key ? readKept("job:" + key, "") : "";
    if (running) track(running as string);
  }, [key, track]);

  const run = useCallback(async (start: () => Promise<{ job_id: string }>): Promise<boolean> => {
    setError("");
    try {
      const { job_id } = await start();
      return await track(job_id);
    } catch (e) {
      setError(errText(e));
      done.current?.();
      return false;
    }
  }, [track]);
  return { job, error, setError, run };
}

/** Poll an endpoint while the component is mounted. */
export function usePoll<T>(path: string | null, ms: number, deps: unknown[] = []): [T | null, () => void] {
  const [data, setData] = useState<T | null>(null);
  const tick = useRef(0);
  const load = useCallback(() => {
    if (!path) return;
    const mine = ++tick.current;
    api<T>(path).then((d) => mine === tick.current && setData(d)).catch(() => {});
  }, [path]);
  useEffect(() => {
    load();
    const id = setInterval(load, ms);
    return () => clearInterval(id);
  }, [load, ms, ...deps]);
  return [data, load];
}
