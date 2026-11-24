import { Cloud, Cpu, Database, Globe, HardDrives, Lightning, ShieldCheck, WifiSlash } from "@phosphor-icons/react";
import { gb } from "../api";
import { usePoll } from "../ui";

export type Bar = {
  engine: { ok: boolean; ram_mb: number; cpu: number };
  vectors: { ok: boolean };
  local_model: { running: boolean; model: string };
  mode: string; offline: boolean;
  usage: { cloud_calls: number; cloud_limit: number; cloud_left: number | null; local_calls: number };
  keiro: { configured: boolean; credits: number | null; zdr: boolean };
  network: "off" | "local" | "lan";
  disk: { lake_bytes: number; free_bytes: number };
  jobs_running: number;
};

const NET = { off: "Data API off", local: "Data API: this computer", lan: "Data API: home network" } as const;

function Item({ icon, children, title }: { icon: React.ReactNode; children: React.ReactNode; title?: string }) {
  return <span className="flex shrink-0 items-center gap-1.5" title={title}>{icon}{children}</span>;
}

/** Always-visible engine state, like Docker Desktop's footer. */
export default function StatusBar() {
  const [b] = usePoll<Bar>("/statusbar", 5000);
  const dot = (ok?: boolean) => <span className={`status-dot ${ok ? "bg-sage-400" : ok === false ? "bg-coral-400" : "bg-text-muted"}`} />;
  const cloud = !b ? "" : b.mode === "local" || b.offline ? "Cloud off" : b.usage.cloud_left === null ? `${b.usage.cloud_calls} cloud calls` : `${b.usage.cloud_left} cloud calls left`;
  return (
    <div role="status" aria-label="Engine status"
      className="flex h-8 shrink-0 items-center gap-4 overflow-hidden border-t border-line bg-surface-primary px-3 text-xs text-text-secondary">
      <Item icon={dot(b ? b.engine.ok && b.vectors.ok : undefined)}>{!b ? "Connecting…" : b.vectors.ok ? "Engine running" : "Vector store down"}</Item>
      {b && (
        <>
          {b.offline && <Item icon={<WifiSlash size={13} className="text-cream-500" />}><span className="font-medium text-cream-500">Offline mode</span></Item>}
          <Item icon={<Cpu size={13} />} title="Local model">{b.local_model.running ? b.local_model.model || "Local model ready" : "Local model stopped"}</Item>
          <Item icon={<Cloud size={13} />}>{cloud}</Item>
          {b.keiro.configured && (
            <Item icon={<ShieldCheck size={13} className={b.keiro.zdr ? "text-sage-500" : ""} />} title="KeiroLabs">
              KeiroLabs{b.keiro.zdr ? " · ZDR" : ""}{b.keiro.credits != null ? ` · ${b.keiro.credits.toLocaleString()} credits` : ""}
            </Item>
          )}
          <Item icon={<Globe size={13} />}>{NET[b.network]}</Item>
          {b.jobs_running > 0 && <Item icon={<Lightning size={13} className="text-accent-600" />}>{b.jobs_running} running</Item>}
          <span className="ml-auto hidden shrink-0 items-center gap-4 md:flex">
            <Item icon={<Database size={13} />} title="Lake size">{gb(b.disk.lake_bytes)}</Item>
            <Item icon={<HardDrives size={13} />} title="Free disk">{gb(b.disk.free_bytes)} free</Item>
            <Item icon={<Cpu size={13} />} title="Engine memory">RAM {b.engine.ram_mb} MB</Item>
          </span>
        </>
      )}
    </div>
  );
}
