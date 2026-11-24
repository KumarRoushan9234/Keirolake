import { CaretDown, Icon } from "@phosphor-icons/react";
import { useEffect, useState } from "react";

export type NavPage = { id: string; label: string; icon: Icon };
export type NavSection = { id: string; label: string; icon: Icon; pages: NavPage[] };

/** Sections that open like dropdowns: opening one closes the others, and the section of the current page opens by itself. */
export default function Sidebar({ sections, current, onGo, slim }: { sections: NavSection[]; current: string; onGo: (id: string) => void; slim: boolean }) {
  const home = sections.find((s) => s.pages.some((p) => p.id === current))?.id ?? sections[0].id;
  const [open, setOpen] = useState(home);
  useEffect(() => setOpen(home), [home]);

  if (slim) {  // collapsed: every page as an icon, sections divided by a hairline
    return (
      <nav aria-label="Pages" className="flex flex-col items-center gap-0.5">
        {sections.map((s, i) => (
          <div key={s.id} className={`flex w-full flex-col items-center gap-0.5 ${i ? "mt-1.5 border-t border-line pt-1.5" : ""}`} role="group" aria-label={s.label}>
            {s.pages.map(({ id, label, icon: I }) => (
              <button key={id} title={label} aria-label={label} aria-current={id === current ? "page" : undefined}
                className={`${id === current ? "nav-item-active" : "nav-item"} w-full justify-center px-0`} onClick={() => onGo(id)}>
                <I size={18} weight={id === current ? "fill" : "regular"} />
              </button>
            ))}
          </div>
        ))}
      </nav>
    );
  }
  return (
    <nav aria-label="Pages" className="space-y-1">
      {sections.map((s) => {
        const expanded = open === s.id;
        const SIcon = s.icon;
        return (
          <div key={s.id}>
            <button className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-xs font-medium text-text-muted hover:bg-surface-tertiary hover:text-text-primary"
              aria-expanded={expanded} aria-controls={`nav-${s.id}`} onClick={() => setOpen(expanded ? "" : s.id)}>
              <SIcon size={14} /><span className="flex-1 text-left">{s.label}</span>
              <CaretDown size={12} className={`transition-transform ${expanded ? "" : "-rotate-90"}`} />
            </button>
            {expanded && (
              <div id={`nav-${s.id}`} className="mb-1 mt-0.5 space-y-0.5">
                {s.pages.map(({ id, label, icon: I }) => (
                  <button key={id} aria-current={id === current ? "page" : undefined} className={`${id === current ? "nav-item-active" : "nav-item"} w-full pl-5`} onClick={() => onGo(id)}>
                    <I size={17} weight={id === current ? "fill" : "regular"} />{label}
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}
