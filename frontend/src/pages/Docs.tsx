import { ArrowClockwise } from "@phosphor-icons/react";
import { Children, isValidElement, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import guide from "../../../docs/guide.md?raw";
import { safeUrl } from "../api";
import { Card, PageHeader, usePoll } from "../ui";
import { CopyBox } from "./Connect";

type Tool = { name: string; description: string; permission: string; read_only: boolean; required: string[];
  arguments: Record<string, { type?: string; description?: string }> };
type Log = { name: string; path: string; text: string };

const TABS = [["guide", "Guide"], ["mcp", "MCP tools"], ["api", "API"], ["logs", "Logs"]] as const;
const LOGS = [["engine", "Engine"], ["qdrant", "Vector database"], ["ollama", "Ollama"]] as const;

// Images and links to other files in the repository don't exist inside the app: leave them out.
// The page already has its own title, so the guide's "# ..." title line goes too.
const GUIDE = guide.replace(/!\[[^\]]*\]\((?!https?:)[^)]*\)\n?/g, "").replace(/^﻿?# [^\n]*\n/, "");

// Headings get GitHub's anchor ids, so the guide's table of contents jumps within the page.
const slug = (node: ReactNode): string => Children.toArray(node).map((c) => (typeof c === "string" || typeof c === "number" ? String(c)
  : isValidElement<{ children?: ReactNode }>(c) ? slug(c.props.children) : "")).join("").toLowerCase().trim().replace(/[^\w\- ]/g, "").replace(/ /g, "-");
const heading = (Tag: "h2" | "h3" | "h4") => ({ children }: { children?: ReactNode }) => <Tag id={slug(children)}>{children}</Tag>;
// Wide code blocks and tables scroll sideways, so they take keyboard focus to scroll with the arrow keys.
const GUIDE_PARTS = {
  h2: heading("h2"), h3: heading("h3"), h4: heading("h4"),
  pre: ({ children }: { children?: ReactNode }) => <pre tabIndex={0}>{children}</pre>,
  table: ({ children }: { children?: ReactNode }) => <table tabIndex={0}>{children}</table>,
};
const guideUrl = (u: string) => (u.startsWith("#") ? u : safeUrl(u) ?? "");

/** The user guide, the MCP and API reference, and the app's logs, all offline. */
export default function Docs() {
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("guide");
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <PageHeader title="Docs">
        <div className="seg" role="tablist" aria-label="Docs">
          {TABS.map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}
        </div>
      </PageHeader>
      {tab === "guide" && <Card><div className="md text-[15px] leading-relaxed"><Markdown remarkPlugins={[remarkGfm]} components={GUIDE_PARTS} urlTransform={guideUrl}>{GUIDE}</Markdown></div></Card>}
      {tab === "mcp" && <McpTools />}
      {tab === "api" && <ApiRef />}
      {tab === "logs" && <Logs />}
    </div>
  );
}

function McpTools() {
  const [tools] = usePoll<Tool[]>("/connect/tools", 600000);
  return (
    <Card title={`${tools?.length ?? ""} tools at /mcp`}>
      <p className="mb-3 text-sm text-text-secondary">Any MCP client (Claude, ChatGPT, Cursor, VS Code) sees the tools its key's permissions allow. Connect one in Serve → AI apps.</p>
      <div className="divide-y divide-line">
        {tools?.map((t) => (
          <div key={t.name} className="py-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="mono text-sm font-semibold">{t.name}</span>
              <span className="badge">{t.permission}</span>
              {!t.read_only && <span className="badge badge-warn">changes data</span>}
            </div>
            <p className="mt-1 text-sm text-text-secondary">{t.description}</p>
            {Object.keys(t.arguments).length > 0 && (
              <p className="mt-1 text-xs text-text-muted">{Object.entries(t.arguments).map(([k, v]) => `${k}${t.required.includes(k) ? "" : "?"}: ${v.type ?? "any"}`).join(" · ")}</p>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

const ENDPOINTS: [string, string, string][] = [
  ["GET", "/v1/me", "The key's name and permissions"],
  ["POST", "/v1/chat/completions", "OpenAI-compatible chat; model \"lake\" answers from your data with sources (llm)"],
  ["GET", "/v1/models", "Models you can use (llm)"],
  ["POST", "/v1/embeddings", "Embeddings from your local model (llm)"],
  ["GET", "/v1/kbs", "Collections (read)"],
  ["POST", "/v1/kbs/{id}/search", "Search a collection by meaning and keywords (read)"],
  ["POST", "/v1/ask", "An answer with sources (ask)"],
  ["POST", "/v1/kbs/{id}/documents", "Add a document (write)"],
  ["GET", "/v1/tables", "Tables with their columns (read)"],
  ["GET", "/v1/tables/{id}/rows", "Rows, with where=column:op:value, order=-column, limit (read)"],
  ["GET", "/v1/tables/{id}/search?q=", "Rows by meaning (read)"],
  ["POST", "/v1/sql", "One read-only SELECT (sql)"],
  ["GET", "/v1/views/{name}?format=csv", "A saved view as JSON, JSONL or CSV (read)"],
  ["GET", "/v1/jobs/{id}", "Progress of a job this key started"],
  ["POST", "/mcp", "Model Context Protocol (Streamable HTTP)"],
];

function ApiRef() {
  return (
    <Card title="Data API">
      <p className="mb-3 text-sm text-text-secondary">Every call needs <span className="mono">Authorization: Bearer klk_…</span> from Serve → API keys. Calls are limited to 120 a minute per key and logged.</p>
      <div className="divide-y divide-line text-sm">
        {ENDPOINTS.map(([m, p, what]) => (
          <div key={m + p} className="flex flex-wrap gap-x-3 py-2"><span className="mono w-12 text-xs font-semibold text-accent-600">{m}</span>
            <span className="mono min-w-0 flex-1 break-all text-xs">{p}</span><span className="w-full text-xs text-text-muted sm:w-auto sm:max-w-[45%]">{what}</span></div>
        ))}
      </div>
      <p className="mb-2 mt-4 text-sm font-semibold">Python</p>
      <CopyBox label="Python example" text={`from openai import OpenAI

lake = OpenAI(base_url="https://<your-address>/v1", api_key="klk_...")
reply = lake.chat.completions.create(model="lake", messages=[{"role": "user", "content": "What do my notes say about pricing?"}])
print(reply.choices[0].message.content)`} />
    </Card>
  );
}

function Logs() {
  const [name, setName] = useState<string>("engine");
  const [lines, setLines] = useState(400);
  const [filter, setFilter] = useState("");
  const [log, reload] = usePoll<Log>(`/connect/logs/${name}?lines=${lines}`, 5000, [name, lines]);
  const box = useRef<HTMLPreElement>(null);
  const shown = useMemo(() => {
    const all = (log?.text ?? "").split("\n");
    const f = filter.trim().toLowerCase();
    return (f ? all.filter((l) => l.toLowerCase().includes(f)) : all).join("\n");
  }, [log, filter]);
  useEffect(() => { if (box.current && !filter) box.current.scrollTop = box.current.scrollHeight; }, [shown, filter]);
  return (
    <Card title={<div className="seg" role="tablist" aria-label="Log">{LOGS.map(([id, label]) => <button key={id} role="tab" aria-selected={name === id} onClick={() => setName(id)}>{label}</button>)}</div>}
      actions={<button className="icon-btn" aria-label="Refresh" onClick={reload}><ArrowClockwise size={16} /></button>}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <input className="input-field max-w-xs" placeholder="Filter, such as error" aria-label="Filter the log" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <select className="input-field w-auto" aria-label="Lines" value={lines} onChange={(e) => setLines(Number(e.target.value))}>
          {[200, 400, 1000, 4000].map((n) => <option key={n} value={n}>Last {n} lines</option>)}
        </select>
      </div>
      <pre ref={box} tabIndex={0} aria-label={`${name} log`} className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-xl bg-surface-secondary p-3 font-mono text-[11px] leading-relaxed">{shown || "Empty."}</pre>
      <p className="mt-2 text-xs text-text-muted">Keys and tokens are removed. File: <span className="mono">{log?.path}</span></p>
    </Card>
  );
}
