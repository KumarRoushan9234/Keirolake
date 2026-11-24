export type Column = { name: string; type: "text" | "number" | "date" | "boolean" | "url"; kind: "plain" | "ai" | "web"; prompt: string };
export type Build = { id: string; goal: string; interval_minutes: number; enabled: boolean; last_run_at: string | null; last_result: Record<string, any> | null };
export type Sheet = {
  id: string; name: string; table: string; description: string; source: string; attribution: string; rows: number | null;
  columns: Column[]; build: Build | null; created_at: string; updated_at: string;
};
export type CellMeta = { sources: string[]; confidence: number | null; model: string };
export type Row = { _id: number; _meta?: Record<string, CellMeta>; [col: string]: any };

export const INTERVALS = [[0, "Manual"], [60, "Hourly"], [360, "Every 6 hours"], [1440, "Daily"], [10080, "Weekly"]] as const;
export const intervalLabel = (m: number) => INTERVALS.find(([v]) => v === m)?.[1] ?? `Every ${m} min`;
export const SOURCE_LABEL: Record<string, string> = { manual: "Manual", import: "Imported", build: "Built", dataset: "Open data" };
