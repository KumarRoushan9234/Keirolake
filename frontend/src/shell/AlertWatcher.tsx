import { useEffect } from "react";
import { api } from "../api";
import { notify } from "../background";

type Alert = { id: string; title: string; body: string; severity: string; scope: string };

/** Tells you about new monitor alerts (a disaster, a critical vulnerability, a big price move) wherever you are in the app. */
export function AlertWatcher() {
  useEffect(() => {
    let stopped = false;
    const shown = new Set<string>();
    const check = async () => {
      try {
        const unseen = await api<Alert[]>("/monitor/alerts?unseen=true&limit=10");
        const fresh = unseen.filter((a) => !shown.has(a.id));
        if (stopped || !fresh.length) return;
        fresh.forEach((a) => shown.add(a.id));
        const first = fresh[0];
        notify({ title: fresh.length > 1 ? `${fresh.length} new alerts` : first.title, body: fresh.length > 1 ? first.title : first.body.slice(0, 140), tone: first.severity === "critical" || first.severity === "high" ? "failed" : "info",
          action: { label: "Open Monitor", go: { page: "Monitor" } } }, 15000);
      } catch { /* the engine may be restarting: try again next time */ }
    };
    const first = window.setTimeout(check, 8000);
    const every = window.setInterval(check, 60000);
    return () => { stopped = true; window.clearTimeout(first); window.clearInterval(every); };
  }, []);
  return null;
}
