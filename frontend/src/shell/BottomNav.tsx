import { ChatCircleText, DotsThreeOutline, PlusCircle, Sparkle, Table } from "@phosphor-icons/react";

const TABS = [
  { id: "Ask", label: "Ask", icon: ChatCircleText },
  { id: "Capture", label: "Capture", icon: PlusCircle },
  { id: "Builder", label: "Build", icon: Sparkle },
  { id: "Tables", label: "Tables", icon: Table },
] as const;

/** Phone navigation: the four things people do most, one thumb away. "More" opens the full menu. */
export default function BottomNav({ current, onGo, onMore }: { current: string; onGo: (page: string) => void; onMore: () => void }) {
  return (
    <nav aria-label="Quick" className="flex shrink-0 border-t border-line bg-surface-primary pb-[env(safe-area-inset-bottom)] md:hidden">
      {TABS.map(({ id, label, icon: Icon }) => {
        const on = current === id;
        return (
          <button key={id} className={`flex min-h-[3.5rem] flex-1 flex-col items-center justify-center gap-0.5 text-[11px] ${on ? "font-semibold text-accent-600" : "text-text-muted"}`}
            aria-current={on ? "page" : undefined} onClick={() => onGo(id)}>
            <Icon size={22} weight={on ? "fill" : "regular"} />{label}
          </button>
        );
      })}
      <button className="flex min-h-[3.5rem] flex-1 flex-col items-center justify-center gap-0.5 text-[11px] text-text-muted" onClick={onMore} aria-label="More pages">
        <DotsThreeOutline size={22} />More
      </button>
    </nav>
  );
}
