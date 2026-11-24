import { MagnifyingGlass } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";

export type Command = { id: string; label: string; hint?: string; run: () => void };

/** Ctrl+K command palette: type to filter, arrows to move, Enter to run, Escape to close. */
export default function Palette({ commands, onClose }: { commands: Command[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const shown = useMemo(() => {
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return commands.filter((c) => terms.every((t) => `${c.label} ${c.hint ?? ""}`.toLowerCase().includes(t)));
  }, [q, commands]);

  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => { setIndex(0); }, [q]);
  useEffect(() => { list.current?.children[index]?.scrollIntoView({ block: "nearest" }); }, [index]);

  const run = (c?: Command) => { if (!c) return; onClose(); c.run(); };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setIndex((i) => Math.min(shown.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIndex((i) => Math.max(0, i - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); run(shown[index]); }
    else if (e.key === "Escape") { e.preventDefault(); onClose(); }
    else if (e.key === "Tab") { e.preventDefault(); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[12vh]" onMouseDown={onClose}>
      <div role="dialog" aria-modal="true" aria-label="Command palette" onKeyDown={onKey} onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-2xl border border-line bg-surface-primary shadow-2xl">
        <div className="flex items-center gap-3 border-b border-line px-4">
          <MagnifyingGlass size={16} className="text-text-muted" />
          <input ref={input} role="combobox" aria-expanded="true" aria-controls="palette-list" aria-activedescendant={shown[index] ? `cmd-${shown[index].id}` : undefined}
            className="w-full bg-transparent py-3.5 text-sm outline-none placeholder:text-text-muted" placeholder="Go to a page or run a command"
            value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <ul id="palette-list" role="listbox" ref={list} className="max-h-80 overflow-y-auto p-2">
          {shown.map((c, i) => (
            <li key={c.id} id={`cmd-${c.id}`} role="option" aria-selected={i === index}
              className={`flex cursor-pointer items-center justify-between rounded-xl px-3 py-2 text-sm ${i === index ? "bg-accent-50 text-accent-600" : "text-text-secondary"}`}
              onMouseEnter={() => setIndex(i)} onClick={() => run(c)}>
              <span>{c.label}</span>
              {c.hint && <span className="text-xs text-text-muted">{c.hint}</span>}
            </li>
          ))}
          {!shown.length && <li className="px-3 py-6 text-center text-sm text-text-muted">No match</li>}
        </ul>
      </div>
    </div>
  );
}
