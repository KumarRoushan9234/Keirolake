import { Bell } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { useJobs, useRunningTasks } from "../background";
import { Progress, useNav } from "../ui";

/** The bell in the top bar: everything running now (engine jobs and requests started from a page), and a way into Jobs. */
export default function JobsBell() {
  const { go } = useNav();
  const jobs = useJobs();
  const tasks = useRunningTasks();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const running = jobs.filter((j) => j.status === "running" || j.status === "queued");
  const recent = jobs.filter((j) => j.status !== "running" && j.status !== "queued").slice(0, 5);
  const count = running.length + tasks.length;

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);

  return (
    <div ref={box} className="relative">
      <button className="icon-btn relative" aria-label={`Jobs${count ? `, ${count} running` : ""}`} aria-expanded={open} onClick={() => setOpen(!open)}>
        <Bell size={18} />
        {count > 0 && <span className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-accent-600 px-1 text-[10px] font-semibold text-white">{count}</span>}
      </button>
      {open && (
        <div className="absolute right-0 top-10 z-40 w-80 rounded-md border border-line bg-surface-primary p-2 shadow-lg" role="dialog" aria-label="Running jobs">
          <p className="px-2 py-1 text-xs font-medium text-text-muted">{count ? `${count} running` : "Nothing running"}</p>
          {tasks.map((t) => (
            <div key={t.key} className="px-2 py-1.5"><p className="truncate text-sm">{t.label}</p><div className="progress-bar mt-1"><div className="progress-fill progress-indeterminate" /></div></div>
          ))}
          {running.map((j) => (
            <div key={j.id} className="px-2 py-1.5"><p className="truncate text-sm">{j.title}</p><Progress value={j.progress} label={j.message || "Queued"} /></div>
          ))}
          {recent.length > 0 && <p className="mt-1 border-t border-line px-2 pb-1 pt-2 text-xs font-medium text-text-muted">Recent</p>}
          {recent.map((j) => (
            <div key={j.id} className="flex items-center gap-2 px-2 py-1 text-sm">
              <span className={`status-dot ${j.status === "done" ? "bg-sage-400" : "bg-coral-400"}`} />
              <span className="min-w-0 flex-1 truncate">{j.title}</span>
            </div>
          ))}
          <button className="btn-ghost mt-1 w-full" onClick={() => { setOpen(false); go({ page: "Jobs" }); }}>Open Jobs</button>
        </div>
      )}
    </div>
  );
}
