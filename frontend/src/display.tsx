import { Eye, Minus, Moon, Plus, SlidersHorizontal, Sun, TextAa } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";

export const SIZES = [0.875, 1, 1.125, 1.25, 1.4] as const;
export type ThemeChoice = "system" | "light" | "dark";

export const ACCENTS = { blue: "#1d63ed", violet: "#6d28d9", green: "#047857", orange: "#c2410c", rose: "#be123c" } as const;
export type Accent = keyof typeof ACCENTS;
type Display = { size: number; theme: ThemeChoice; contrast: boolean; calm: boolean; collapsed: boolean; accent: Accent };
const DEFAULTS: Display = { size: 1, theme: "light", contrast: false, calm: false, collapsed: false, accent: "blue" };
const KEY = "display";

function load(): Display {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    // 0.7 made light the default. Older saves stored "system" without anyone choosing it, so they start light once.
    if (saved.v !== 2) saved.theme = "light";
    return { ...DEFAULTS, ...saved };
  } catch { return DEFAULTS; }
}

/** Display options kept per user: text size, theme, contrast, reduced motion and sidebar state. */
export function useDisplay() {
  const [d, setD] = useState<Display>(load);
  const apply = useCallback((v: Display) => {
    const root = document.documentElement;
    root.style.setProperty("--text-scale", String(v.size));
    const dark = v.theme === "dark" || (v.theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    root.classList.toggle("dark", dark);
    root.classList.toggle("hc", v.contrast);
    root.classList.toggle("calm", v.calm);
    if (v.accent === "blue") delete root.dataset.accent; else root.dataset.accent = v.accent;
    if ("__TAURI_INTERNALS__" in window) {  // the Windows title bar follows the theme instead of the accent colour
      (window as unknown as { __TAURI_INTERNALS__: { invoke: (c: string, a: unknown) => Promise<unknown> } }).__TAURI_INTERNALS__
        .invoke("window_theme", { dark }).catch(() => {});
    }
  }, []);
  useEffect(() => {
    apply(d);
    try { localStorage.setItem(KEY, JSON.stringify({ ...d, v: 2 })); } catch { /* private mode */ }
  }, [d, apply]);
  useEffect(() => {
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => d.theme === "system" && apply(d);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [d, apply]);

  const set = useCallback((patch: Partial<Display>) => setD((v) => ({ ...v, ...patch })), []);
  const step = useCallback((dir: 1 | -1 | 0) => setD((v) => {
    if (dir === 0) return { ...v, size: 1 };
    const i = SIZES.findIndex((s) => s === v.size);
    const next = SIZES[Math.min(SIZES.length - 1, Math.max(0, (i < 0 ? 1 : i) + dir))];
    return { ...v, size: next };
  }), []);
  return { ...d, set, step };
}
export type DisplayApi = ReturnType<typeof useDisplay>;

/** The Display popover: text size, theme, contrast and motion. */
export function DisplayMenu({ api, up = false }: { api: DisplayApi; up?: boolean }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  const pct = Math.round(api.size * 100);
  return (
    <div className="relative" ref={box}>
      <button className="icon-btn" aria-label="Display settings" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}>
        <SlidersHorizontal size={18} />
      </button>
      {open && (
        <div role="dialog" aria-label="Display settings"
          className={`absolute z-40 w-72 max-w-[calc(100vw-2rem)] rounded-2xl border border-line bg-surface-primary p-4 shadow-xl ${up ? "bottom-full mb-2 left-0" : "right-0 top-full mt-2"}`}>
          <p className="label flex items-center gap-2"><TextAa size={14} /> Text size</p>
          <div className="mb-4 flex items-center gap-2">
            <button className="icon-btn border border-line" aria-label="Smaller text" onClick={() => api.step(-1)} disabled={api.size <= SIZES[0]}><Minus size={14} /></button>
            <button className="btn-ghost flex-1 tabular-nums" aria-label="Reset text size" onClick={() => api.step(0)}>{pct}%</button>
            <button className="icon-btn border border-line" aria-label="Larger text" onClick={() => api.step(1)} disabled={api.size >= SIZES[SIZES.length - 1]}><Plus size={14} /></button>
          </div>
          <p className="label">Theme</p>
          <div className="seg mb-4 w-full">
            {(["system", "light", "dark"] as const).map((t) => (
              <button key={t} className="flex-1 capitalize" aria-pressed={api.theme === t} onClick={() => api.set({ theme: t })}>
                {t === "light" ? <Sun size={14} className="mr-1 inline" /> : t === "dark" ? <Moon size={14} className="mr-1 inline" /> : null}{t}
              </button>
            ))}
          </div>
          <p className="label">Accent</p>
          <div className="mb-4 flex gap-2" role="radiogroup" aria-label="Accent colour">
            {(Object.keys(ACCENTS) as Accent[]).map((a) => (
              <button key={a} role="radio" aria-checked={api.accent === a} aria-label={a} title={a} onClick={() => api.set({ accent: a })}
                className={`h-7 w-7 rounded-full ring-offset-2 ring-offset-surface-primary transition ${api.accent === a ? "ring-2 ring-text-primary" : "hover:scale-110"}`}
                style={{ background: ACCENTS[a] }} />
            ))}
          </div>
          <Toggle icon={<Eye size={14} />} label="High contrast" checked={api.contrast} onChange={(v) => api.set({ contrast: v })} />
          <Toggle label="Reduce motion" checked={api.calm} onChange={(v) => api.set({ calm: v })} />
          <p className="mt-3 text-xs text-text-muted">Shortcuts: Ctrl + / − / 0 for text size, Ctrl K for the command palette, Ctrl B for the sidebar.</p>
        </div>
      )}
    </div>
  );
}

function Toggle({ label, checked, onChange, icon }: { label: string; checked: boolean; onChange: (v: boolean) => void; icon?: React.ReactNode }) {
  return (
    <label className="flex cursor-pointer items-center justify-between py-1.5 text-sm">
      <span className="flex items-center gap-2">{icon}{label}</span>
      <input type="checkbox" role="switch" className="h-4 w-8 cursor-pointer accent-accent-400" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}
