import { ArrowsClockwise, Globe, Plug, ShieldCheck } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText, isDesktop, KB } from "../api";
import { clearTask, runTask, useTask } from "../background";
import { Card, ErrorNote, PageHeader, usePoll } from "../ui";
import { CopyBox, HooksCard, KeysCard, McpCard, Net, Overview } from "./Connect";
import Share from "./settings/Share";
import { Devices } from "./Settings";

type NetFull = Net & { bind: string; allow: string[]; own_cert: boolean; upnp: { on: boolean; external_ip: string; error: string }; upnp_wanted: boolean };

function useAccess() {
  const [data, reload] = usePoll<Overview & { network: NetFull }>("/access", 8000);
  const [error, setError] = useState("");
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); };
  return { data, reload, error, setError, act };
}

/** API keys, MCP and the request log: who may read the lake, and what they did. */
export function KeysPage() {
  const { data, error, setError, act } = useAccess();
  const [kbs] = usePoll<KB[]>("/kbs", 60000);
  const [log] = usePoll<{ status: number; method: string; path: string; key: string; at: string }[]>("/access/log?limit=40", 10000);
  const base = data?.network.urls[0] ?? "https://127.0.0.1:8443";
  return (
    <div className="space-y-4">
      <PageHeader title="API keys" />
      <ErrorNote error={error} onClose={() => setError("")} />
      {data && !data.network.running && <p className="field-ok">Apps reach the lake once Sharing is turned on. Keys work the same on every address.</p>}
      <KeysCard data={data ?? undefined} kbs={kbs ?? []} act={act} base={base} />
      <McpCard base={base} running={!!data?.network.running} />
      <Card title="Recent requests">
        <div className="max-h-72 divide-y divide-line overflow-y-auto text-xs">
          {log?.map((r, i) => (
            <div key={i} className="flex gap-2 py-1.5"><span className={r.status < 400 ? "text-sage-500" : "text-coral-500"}>{r.status}</span>
              <span className="mono min-w-0 flex-1 truncate">{r.method} {r.path}</span><span className="text-text-muted">{r.key} · {new Date(r.at).toLocaleTimeString()}</span></div>
          ))}
          {log && !log.length && <p className="py-1.5 text-text-muted">No requests yet.</p>}
        </div>
      </Card>
    </div>
  );
}

export function WebhooksPage() {
  const { data, error, setError, act } = useAccess();
  return (
    <div className="space-y-4">
      <PageHeader title="Webhooks" />
      <ErrorNote error={error} onClose={() => setError("")} />
      <HooksCard data={data ?? undefined} act={act} />
    </div>
  );
}

/** The OpenAI-compatible gateway: any OpenAI SDK or tool can use local models and your lake through it. */
export function GatewayPage() {
  const [g] = usePoll<{ base_urls: string[]; models: string[]; listener: NetFull }>("/gateway", 15000);
  const [model, setModel] = useState("auto");
  const base = g?.base_urls[0] ?? "https://127.0.0.1:8443/v1";
  const py = `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="klk_...")  # a key with the llm scope\nreply = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "What does my data say about solar prices?"}],\n)\nprint(reply.choices[0].message.content)`;
  const js = `import OpenAI from "openai";\n\nconst client = new OpenAI({ baseURL: "${base}", apiKey: "klk_..." });\nconst reply = await client.chat.completions.create({ model: "${model}", messages: [{ role: "user", content: "Hello" }] });\nconsole.log(reply.choices[0].message.content);`;
  const curl = `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer klk_..." -H "Content-Type: application/json" \\\n  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello"}]}'`;
  return (
    <div className="space-y-4">
      <PageHeader title="LLM gateway" />
      <Card title={<span className="flex items-center gap-2"><Plug size={16} className="text-accent-600" />Use KeiroLake as your app's OpenAI</span>}>
        <p className="text-sm text-text-secondary">Point any OpenAI SDK, agent framework or tool at this address. Answers come from local models first; cloud models only within your daily limit. Every call is logged under its key.</p>
        <div className="mt-3 space-y-2">{(g?.base_urls.length ? g.base_urls : [base]).map((u) => <CopyBox key={u} text={u} label="base URL" />)}</div>
        {g && !g.listener.running && <p className="mt-2 text-xs text-text-muted">Turn on Sharing (this computer, Wi-Fi or open port) so apps can reach this address.</p>}
        <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-3">
          <div className="well p-3"><dt className="font-semibold mono">auto</dt><dd className="text-xs text-text-muted">The app's choice, local first</dd></div>
          <div className="well p-3"><dt className="font-semibold mono">lake</dt><dd className="text-xs text-text-muted">Grounded in every collection; sources returned with the answer</dd></div>
          <div className="well p-3"><dt className="font-semibold mono">lake:&lt;id&gt;</dt><dd className="text-xs text-text-muted">Grounded in one collection or topic</dd></div>
        </dl>
      </Card>
      <Playground models={g?.models ?? ["auto", "lake"]} />
      <Card title="Code">
        <label htmlFor="gm" className="label">Model</label>
        <select id="gm" className="input-field mb-3 max-w-sm" value={model} onChange={(e) => setModel(e.target.value)}>{(g?.models ?? ["auto", "lake"]).map((m) => <option key={m}>{m}</option>)}</select>
        <div className="grid gap-3 lg:grid-cols-3">
          <div><p className="label">Python</p><CopyBox text={py} label="Python" /></div>
          <div><p className="label">JavaScript</p><CopyBox text={js} label="JavaScript" /></div>
          <div><p className="label">curl</p><CopyBox text={curl} label="curl" /></div>
        </div>
        <p className="mt-3 text-xs text-text-muted">Also: POST {base}/embeddings and GET {base}/models. Make a key with the llm permission (and read, for grounded models) in API keys.</p>
      </Card>
    </div>
  );
}

function Playground({ models }: { models: string[] }) {
  const [model, setModel] = useState("auto");
  const [prompt, setPrompt] = useState("");
  const task = useTask<{ model: string; content: string; sources: { n: number; title: string; url: string | null }[] }>("gateway:try");
  const out = task.result ?? null, busy = task.status === "running", error = task.error ?? "";
  const setError = (_: string) => clearTask("gateway:try");
  const send = () => runTask("gateway:try", `Gateway: ${prompt.slice(0, 40)}`, () => api("/gateway/try", { method: "POST", json: { model, prompt } }), { page: "Gateway" });
  return (
    <Card title="Try it">
      <div className="flex flex-wrap gap-2">
        <select aria-label="Model to try" className="input-field w-auto" value={model} onChange={(e) => setModel(e.target.value)}>{models.map((m) => <option key={m}>{m}</option>)}</select>
        <input aria-label="Message" className="input-field min-w-0 flex-1" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="What does my data say about solar prices?" onKeyDown={(e) => e.key === "Enter" && prompt.trim() && send()} />
        <button className="btn-primary" disabled={busy || !prompt.trim()} onClick={send}>{busy ? "Thinking…" : "Send"}</button>
      </div>
      <ErrorNote error={error} onClose={() => setError("")} />
      {out && (
        <div className="mt-3 space-y-2 text-sm">
          <p className="whitespace-pre-wrap">{out.content}</p>
          {!!out.sources.length && <ol className="list-decimal pl-5 text-xs text-text-secondary">{out.sources.map((s) => <li key={s.n}>{s.url ? <a className="text-accent-600 hover:underline" href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}</li>)}</ol>}
          <p className="text-xs text-text-muted">Answered by {out.model}</p>
        </div>
      )}
    </Card>
  );
}

/** Who can reach the lake: this computer, home Wi-Fi, an internet link, or a port you open yourself. */
export function SharingPage() {
  const { data, error, setError, act } = useAccess();
  return (
    <div className="space-y-4">
      <PageHeader title="Sharing" />
      <ErrorNote error={error} onClose={() => setError("")} />
      <Share />
      {isDesktop && <OwnAddress />}
      {isDesktop && <StayReachable />}
      {isDesktop && data && <OpenPort net={data.network} act={act} />}
      {isDesktop && <Devices />}
    </div>
  );
}

function OpenPort({ net, act }: { net: NetFull; act: (fn: () => Promise<unknown>) => Promise<void> }) {
  const open = net.mode === "open";
  const [port, setPort] = useState(String(net.port));
  const [bind, setBind] = useState(net.bind === "0.0.0.0" ? "" : net.bind);
  const [allow, setAllow] = useState(net.allow.join(", "));
  const [cert, setCert] = useState("");
  const [key, setKey] = useState("");
  const [upnp, setUpnp] = useState(net.upnp_wanted);
  const [pub, setPub] = useState<{ ip: string; cgnat: boolean } | null>(null);
  const [check, setCheck] = useState<{ reachable: boolean; ip: string } | null>(null);
  const [fw, setFw] = useState("");
  const body = (mode: string) => ({ mode, port: +port || 8443, bind, allow: allow.split(/[\s,]+/).filter(Boolean), upnp,
    ...(cert || key ? { cert, key } : {}) });
  const apply = (mode: string) => act(() => api("/access/network", { method: "PUT", json: body(mode) }));
  return (
    <Card title={<span className="flex items-center gap-2"><Globe size={16} className="text-accent-600" />Open a port</span>}
      actions={open ? <span className="badge-ok">Open on port {net.port}</span> : undefined}>
      <p className="text-sm text-text-secondary">For a fixed address you control: the lake listens on a port you choose, always over HTTPS, and your router forwards it. Needs a password, like the internet link.</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <div><label htmlFor="op-port" className="label">Port</label><input id="op-port" className="input-field" inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} /></div>
        <div><label htmlFor="op-bind" className="label">Listen on</label>
          <select id="op-bind" className="input-field" value={bind} onChange={(e) => setBind(e.target.value)}>
            <option value="">Every network adapter</option>{net.addresses.map((a) => <option key={a} value={a}>{a}</option>)}
          </select></div>
        <div><label htmlFor="op-allow" className="label">Only these addresses (optional)</label><input id="op-allow" className="input-field mono" value={allow} onChange={(e) => setAllow(e.target.value)} placeholder="203.0.113.0/24" /></div>
      </div>
      <details className="mt-3 text-sm">
        <summary className="cursor-pointer text-text-secondary">Your own certificate (optional)</summary>
        <p className="mt-1 text-xs text-text-muted">Without one, a certificate made on this computer is used and browsers ask you to trust it once.{net.own_cert ? " Your own certificate is in use." : ""}</p>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <input aria-label="Certificate file" className="input-field mono" placeholder="C:\certs\fullchain.pem" value={cert} onChange={(e) => setCert(e.target.value)} />
          <input aria-label="Key file" className="input-field mono" placeholder="C:\certs\privkey.pem" value={key} onChange={(e) => setKey(e.target.value)} />
        </div>
      </details>
      <label className="mt-3 flex items-center gap-2 text-sm"><input type="checkbox" checked={upnp} onChange={(e) => setUpnp(e.target.checked)} />Ask my router to forward the port (UPnP)</label>
      {net.upnp.error && <p className="mt-1 text-xs text-text-muted">Router: {net.upnp.error}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button className="btn-primary" onClick={() => apply("open")}>{open ? "Apply changes" : "Open the port"}</button>
        {open && <button className="btn-secondary" onClick={() => apply("off")}>Close it</button>}
      </div>
      {net.error && <p className="field-error mt-3">{net.error}</p>}
      {open && net.running && (
        <div className="mt-4 space-y-3 border-t border-line pt-4 text-sm">
          {net.urls.map((u) => <CopyBox key={u} text={u} label="address" />)}
          {net.fingerprint && <p className="break-all text-xs text-text-muted"><ShieldCheck size={12} className="mr-1 inline" />Certificate SHA-256 {net.fingerprint}</p>}
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" onClick={() => act(async () => setPub(await api("/access/network/public-ip")))}>Show my public address</button>
            <button className="btn-secondary" onClick={() => act(async () => setCheck(await api("/access/network/check", { method: "POST" })))}><ArrowsClockwise size={14} />Check from outside</button>
            <button className="btn-ghost" onClick={() => act(async () => { const r = await api<{ done: boolean; manual?: string; rule?: string }>("/access/network/firewall", { method: "POST", json: { action: "add" } }); setFw(r.done ? `Firewall rule added: ${r.rule}` : `Run: ${r.manual}`); })}>Allow through the firewall</button>
          </div>
          {pub && <p>Public address <span className="mono">{pub.ip}</span>{pub.cgnat ? ". Your provider shares this address between customers (carrier-grade NAT), so an open port can't be reached from outside. Use the internet link instead." : "."}</p>}
          {check && <p className={check.reachable ? "field-ok" : "field-error"}>{check.reachable ? `Reachable from the internet at ${check.ip}:${net.port}.` : `Not reachable from outside yet. Forward TCP ${net.port} to this computer in your router, or use the internet link.`}</p>}
          {fw && <p className="text-xs text-text-muted">{fw}</p>}
          <p className="text-xs text-text-muted">The address check asks ifconfig.co, which sees your public address and port. The public address lookup uses ipify.org.</p>
        </div>
      )}
    </Card>
  );
}

type TunnelSt = { running: boolean; url: string; named: boolean; hostname: string; port: number; error: string };

/** A fixed, custom address (lake.yourdomain.com) through your own Cloudflare account, instead of a new random link each time. */
function OwnAddress() {
  const [t, reload] = usePoll<TunnelSt>("/network/tunnel", 5000);
  const [token, setToken] = useState("");
  const [host, setHost] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async (values: Record<string, unknown>) => {
    setBusy(true); setError("");
    try { await api("/network/tunnel/config", { method: "PUT", json: values }); setToken(""); } catch (e) { setError(errText(e)); }
    setBusy(false); reload();
  };
  return (
    <Card title="Your own internet address">
      <p className="text-sm text-text-secondary">The free link is a new random name each time. For a fixed name such as <span className="mono">lake.yourdomain.com</span>, connect a Cloudflare tunnel from your own Cloudflare account (free; you need a domain on Cloudflare).</p>
      {t?.named ? (
        <div className="mt-3 space-y-2 text-sm">
          <p>Using your Cloudflare tunnel{t.hostname ? <> at <span className="mono">{t.hostname}</span></> : ""}. {t.running ? "It's on." : "Turn it on with Anywhere above."}</p>
          <button className="btn-ghost" disabled={busy} onClick={() => save({ token: "", hostname: "" })}>Go back to the free random link</button>
        </div>
      ) : (
        <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm">
          <li>In Cloudflare Zero Trust, open Networks, Tunnels, and create a tunnel (choose cloudflared). Copy the token from its install command.</li>
          <li>Under Public hostname, add your name (for example <span className="mono">lake</span> on your domain) with service <span className="mono">http://localhost:{t?.port ?? 8442}</span>.</li>
          <li>Paste both here:
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              <input aria-label="Tunnel token" type="password" autoComplete="off" className="input-field mono" placeholder="Tunnel token" value={token} onChange={(e) => setToken(e.target.value)} />
              <input aria-label="Hostname" className="input-field mono" placeholder="lake.yourdomain.com" value={host} onChange={(e) => setHost(e.target.value)} />
            </div>
            <button className="btn-primary mt-2" disabled={busy || token.length < 40 || !host} onClick={() => save({ token, hostname: host })}>Use my address</button></li>
        </ol>
      )}
      <ErrorNote error={error || t?.error || ""} onClose={() => setError("")} />
    </Card>
  );
}

type Power = { platform: string; awake: boolean; keep_awake: boolean; lid_changed: boolean; lid?: { ac: number; dc: number | null }; lid_error?: string };

/** Keep answering while you're away: no sleep while KeiroLake runs, and (optionally) stay on with the lid closed. */
function StayReachable() {
  const [p, reload] = usePoll<Power>("/power", 15000);
  const [error, setError] = useState("");
  const set = async (values: Record<string, unknown>) => {
    setError("");
    try { await api("/power", { method: "PUT", json: values }); } catch (e) { setError(errText(e)); }
    reload();
  };
  const win = p?.platform === "win32";
  return (
    <Card title="Stay reachable when you're away">
      <div className="space-y-3 text-sm">
        <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={!!p?.keep_awake} onChange={(e) => set({ keep_awake: e.target.checked })} />
          <span><span className="font-medium">Keep this computer awake while KeiroLake runs</span>
            <span className="block text-xs text-text-muted">The screen can still turn off and lock. A locked computer keeps answering; a sleeping one can't.</span></span></label>
        <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={!!p?.lid_changed} disabled={!p}
          onChange={(e) => set({ lid: e.target.checked })} />
          <span><span className="font-medium">Stay on when the lid is closed (while plugged in)</span>
            <span className="block text-xs text-text-muted">{win ? "Changes your power plan's lid action to Do nothing when plugged in. Turning this off puts your old setting back." : "On Linux this is a system setting: set HandleLidSwitch=ignore in /etc/systemd/logind.conf."}
              {" "}Keep the laptop ventilated; closed laptops get warm.</span></span></label>
        {p?.lid && <p className="text-xs text-text-muted">Lid closed now does: plugged in, {["nothing", "sleep", "hibernate", "shut down"][p.lid.ac] ?? p.lid.ac}{p.lid.dc !== null ? `; on battery, ${["nothing", "sleep", "hibernate", "shut down"][p.lid.dc] ?? p.lid.dc}` : ""}.</p>}
        <ErrorNote error={error || p?.lid_error || ""} onClose={() => setError("")} />
      </div>
    </Card>
  );
}
