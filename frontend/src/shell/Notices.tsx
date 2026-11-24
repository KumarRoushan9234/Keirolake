import { CheckCircle, Info, WarningCircle, X } from "@phosphor-icons/react";
import { dismiss, useJobs, useNotices } from "../background";
import { useNav } from "../ui";

/** Messages in the corner when background work finishes, wherever you are in the app. */
export default function Notices() {
  useJobs();  // keeps the shared job watcher running for the whole app, so finished jobs are announced on any page
  const list = useNotices();
  const { go } = useNav();
  return (
    <div className="pointer-events-none fixed bottom-12 right-4 z-50 flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2" role="status" aria-live="polite">
      {list.map((n) => (
        <div key={n.id} className="pointer-events-auto flex items-start gap-3 rounded-lg border border-line bg-surface-primary p-3 shadow-lg animate-fade-up">
          {n.tone === "done" ? <CheckCircle size={18} weight="fill" className="mt-0.5 shrink-0 text-sage-500" />
            : n.tone === "failed" ? <WarningCircle size={18} weight="fill" className="mt-0.5 shrink-0 text-coral-500" />
            : <Info size={18} className="mt-0.5 shrink-0 text-accent-600" />}
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">{n.title}</p>
            {n.body && <p className="mt-0.5 line-clamp-2 text-xs text-text-muted">{n.body}</p>}
            {n.action && <button className="mt-1 text-xs font-semibold text-accent-600" onClick={() => { dismiss(n.id); go(n.action!.go); }}>{n.action.label}</button>}
          </div>
          <button className="icon-btn -m-1 h-7 w-7" aria-label="Dismiss" onClick={() => dismiss(n.id)}><X size={14} /></button>
        </div>
      ))}
    </div>
  );
}
