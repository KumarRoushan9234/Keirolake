import { CheckCircle, Plugs, WarningCircle } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText } from "../api";
import { Card, ErrorNote, PageHeader, useNav, usePoll } from "../ui";
import { CopyBox } from "./Connect";

type Status = { apps: Record<string, string>; bridge: boolean; link: string; scopes: Record<string, string>;
  claude_desktop: { config: string; installed: boolean; connected: boolean } };
type Result = { raw_key: string; key: { name: string; scopes: string[] }; bridge: boolean; installed?: { path: string; restart: string };
  setups: { "claude-desktop": string; "claude-code": string; cursor: string; vscode: string; connector_url: string; http_url: string; remote?: string } };

const APPS: [string, string, string][] = [
  ["claude-desktop", "Claude Desktop", "Chat, one click"],
  ["claude-code", "Claude Code", "Terminal, IDE and the Code tab"],
  ["claude-web", "Claude.ai", "Over the internet link"],
  ["chatgpt", "ChatGPT", "Over the internet link"],
  ["cursor", "Cursor", "On this computer"],
  ["vscode", "VS Code", "On this computer"],
];
const WEB = new Set(["claude-web", "chatgpt"]);
const SCOPES = ["read", "ask", "write", "llm", "sql"];

/** Use the lake from Claude, ChatGPT, Cursor or VS Code: each app gets its own key and a ready setup. */
export default function AiApps() {
  const { go } = useNav();
  const [status, reload] = usePoll<Status>("/connect", 15000);
  const [app, setApp] = useState("claude-desktop");
  const [scopes, setScopes] = useState(["read", "ask", "write", "llm"]);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const web = WEB.has(app);
  const needsLink = web && !status?.link;
  const needsDesktop = !web && status && !status.bridge;

  const connect = async () => {
    setBusy(true); setError(""); setResult(null);
    try {
      setResult(await api<Result>("/connect", { method: "POST", json: { app, scopes, install: app === "claude-desktop" } }));
      reload();
    } catch (e) { setError(errText(e)); } finally { setBusy(false); }
  };
  const pick = (id: string) => { setApp(id); setResult(null); setError(""); };

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <PageHeader title="AI apps" />
      <ErrorNote error={error} onClose={() => setError("")} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3" role="radiogroup" aria-label="App">
        {APPS.map(([id, name, how]) => (
          <button key={id} role="radio" aria-checked={app === id} onClick={() => pick(id)}
            className={`card flex flex-col items-start gap-0.5 p-3 text-left transition hover:border-accent-400 ${app === id ? "border-accent-400 ring-2 ring-accent-100" : ""}`}>
            <span className="flex w-full items-center justify-between text-sm font-semibold">{name}
              {id === "claude-desktop" && status?.claude_desktop.connected && <CheckCircle size={16} weight="fill" className="text-sage-500" aria-label="Connected" />}</span>
            <span className="text-xs text-text-muted">{how}</span>
          </button>
        ))}
      </div>

      <Card>
        <p className="label">What it may do</p>
        <div className="flex flex-wrap gap-2">
          {SCOPES.map((s) => (
            <button key={s} title={status?.scopes[s]} className={scopes.includes(s) ? "chip-active" : "chip"} aria-pressed={scopes.includes(s)}
              onClick={() => setScopes(scopes.includes(s) ? scopes.filter((x) => x !== s) : [...scopes, s])}>{s}</button>
          ))}
        </div>
        <p className="mt-2 text-xs text-text-muted">{scopes.map((s) => status?.scopes[s]).filter(Boolean).join(" · ")}</p>
        {needsLink && (
          <p className="mt-3 flex items-center gap-2 text-sm text-text-secondary"><WarningCircle size={16} className="text-amber-500" />
            Turn on the internet link first, so {app === "chatgpt" ? "ChatGPT" : "Claude.ai"} can reach the lake.
            <button className="btn-ghost" onClick={() => go({ page: "Sharing" })}>Open Sharing</button></p>
        )}
        {needsDesktop && <p className="mt-3 text-sm text-text-secondary">This works from the KeiroLake desktop app on this computer.</p>}
        <button className="btn-primary mt-3" disabled={busy || !scopes.length || needsLink || !!needsDesktop} onClick={connect}>
          <Plugs size={16} /> {busy ? "Connecting…" : app === "claude-desktop" ? "Add to Claude Desktop" : "Make the setup"}
        </button>
      </Card>

      {result && <Setup app={app} r={result} />}
    </div>
  );
}

function Setup({ app, r }: { app: string; r: Result }) {
  const s = r.setups;
  return (
    <Card title={`Ready for ${r.key.name}`}>
      <div className="space-y-3 text-sm">
        {app === "claude-desktop" && r.installed && (
          <>
            <p className="field-ok" role="status">Added to Claude Desktop. {r.installed.restart}</p>
            <p className="text-xs text-text-muted">Config: <span className="mono">{r.installed.path}</span> (the previous one is kept as a backup next to it)</p>
            <p>Then, in a new Claude chat, ask something like <em>"What's in my KeiroLake?"</em></p>
            <p className="text-xs text-text-muted">This adds KeiroLake to Claude Desktop's chats. Claude Code (including the Code tab in Claude Desktop) keeps its own list: use the Claude Code tile for it.</p>
          </>
        )}
        {app === "claude-code" && (<><p>Run this in a terminal:</p><CopyBox text={s["claude-code"]} label="command" /></>)}
        {app === "cursor" && (<><p>Add this to <span className="mono">~/.cursor/mcp.json</span>:</p><CopyBox text={s.cursor} label="Cursor config" /></>)}
        {app === "vscode" && (<><p>Add this to <span className="mono">.vscode/mcp.json</span> (or run <em>MCP: Add Server</em>):</p><CopyBox text={s.vscode} label="VS Code config" /></>)}
        {app === "claude-web" && (
          <>
            <p>In Claude.ai: <strong>Settings → Connectors → Add custom connector</strong>, name it KeiroLake and paste:</p>
            <CopyBox text={s.connector_url} label="connector link" />
          </>
        )}
        {app === "chatgpt" && (
          <>
            <p>In ChatGPT: <strong>Settings → Apps & Connectors → Advanced → Developer mode</strong>, then <strong>Create</strong>. Paste the link and choose <strong>No authentication</strong>:</p>
            <CopyBox text={s.connector_url} label="connector link" />
          </>
        )}
        {WEB.has(app) && <p className="text-xs text-text-muted">The key is inside this link: treat it like a password. Revoke it in API keys to cut access.</p>}
        {!WEB.has(app) && app !== "claude-desktop" && <p className="text-xs text-text-muted">KeiroLake starts by itself in the tray when the app needs it.</p>}
        <details className="text-xs">
          <summary className="cursor-pointer text-text-secondary">The key</summary>
          <div className="mt-2"><CopyBox text={r.raw_key} label="API key" /></div>
          <p className="mt-1 text-text-muted">Shown once. Permissions: {r.key.scopes.join(", ")}.</p>
        </details>
      </div>
    </Card>
  );
}
