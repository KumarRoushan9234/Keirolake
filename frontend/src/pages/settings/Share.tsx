import { Globe, House, Lock, ShieldCheck } from "@phosphor-icons/react";
import { useState } from "react";
import { api, errText, isDesktop } from "../../api";
import { Card, ErrorNote, usePoll } from "../../ui";
import { CopyBox, Net } from "../Connect";

type Tunnel = { supported: boolean; installed: boolean; running: boolean; url: string; error: string; password_set: boolean; provider: string };

/** Reach your lake from other devices in three steps: a password, a way in, then a link. */
export default function Share({ compact = false }: { compact?: boolean }) {
  const [t, reloadT] = usePoll<Tunnel>(isDesktop ? "/network/tunnel" : null, 5000);
  const [data, reloadN] = usePoll<{ network: Net }>(isDesktop ? "/access" : null, 5000);
  const [auth, reloadA] = usePoll<{ password_set: boolean }>("/auth/status", 8000);
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const net = data?.network;
  const act = async (label: string, fn: () => Promise<unknown>) => { setError(""); setBusy(label); try { await fn(); } catch (e) { setError(errText(e)); } setBusy(""); reloadT(); reloadN(); reloadA(); };
  const setMode = (mode: string) => act(mode, () => api("/access/network", { method: "PUT", json: { mode, port: net?.port ?? 8443 } }));

  if (!isDesktop) return <Card title="Share your lake"><p className="text-sm text-text-secondary">Sharing is changed on the computer that runs KeiroLake. You are connected to it now.</p></Card>;
  const passwordSet = !!auth?.password_set;
  const wifi = net?.running && net.mode === "lan";
  return (
    <Card title={<span className="flex items-center gap-2"><Globe size={16} className="text-accent-600" />Use your lake from other devices</span>}>
      <ol className="space-y-5 text-sm">
        <li>
          <p className="font-semibold">1. Choose a password</p>
          {passwordSet ? <p className="mt-1 flex items-center gap-1.5 text-text-secondary"><Lock size={14} />A password is set. Other devices sign in with it.</p> : (
            <div className="mt-1 flex flex-wrap gap-2">
              <input className="input-field min-w-0 flex-1" type="password" autoComplete="new-password" aria-label="Password for other devices" placeholder="At least 10 characters" value={pw} onChange={(e) => setPw(e.target.value)} />
              <button className="btn-primary" disabled={pw.length < 10 || !!busy} onClick={() => act("pw", async () => { await api("/auth/password", { method: "PUT", json: { password: pw } }); setPw(""); })}>Set password</button>
            </div>
          )}
        </li>

        <li className={passwordSet ? "" : "pointer-events-none opacity-50"} aria-disabled={!passwordSet}>
          <p className="font-semibold">2. Choose how far it reaches</p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <div className={`rounded-md border p-3 ${wifi ? "border-accent-600" : "border-line"}`}>
              <p className="flex items-center gap-2 font-medium"><House size={16} className="text-accent-600" />Home Wi-Fi</p>
              <p className="mt-1 text-xs text-text-muted">Devices on the same network open a local address.</p>
              <button className="btn-secondary mt-2" disabled={!!busy} onClick={() => setMode(wifi ? "off" : "lan")}>{wifi ? "Turn off" : "Turn on"}</button>
            </div>
            <div className={`rounded-md border p-3 ${t?.running ? "border-accent-600" : "border-line"}`}>
              <p className="flex items-center gap-2 font-medium"><Globe size={16} className="text-accent-600" />Anywhere</p>
              <p className="mt-1 text-xs text-text-muted">A private link that works on any network, such as mobile data.</p>
              {t && !t.supported ? <p className="mt-2 text-xs text-text-muted">Not available on this system. Use Tailscale.</p> : (
                <button className={t?.running ? "btn-secondary mt-2" : "btn-primary mt-2"} disabled={!!busy || !t}
                  onClick={() => act("tunnel", () => api(t?.running ? "/network/tunnel/stop" : "/network/tunnel/start", { method: "POST" }))}>
                  {busy === "tunnel" ? (t?.running ? "Turning off…" : t?.installed ? "Connecting…" : "Downloading and connecting…") : t?.running ? "Turn off" : "Turn on"}</button>
              )}
            </div>
          </div>
        </li>

        {(t?.running || wifi) && (
          <li>
            <p className="font-semibold">3. Open the link on your other device</p>
            <div className="mt-2 space-y-2">
              {t?.running && <CopyBox text={t.url} label="internet link" />}
              {wifi && net!.urls.map((u) => <CopyBox key={u} text={u} label="home Wi-Fi address" />)}
            </div>
            <p className="mt-2 text-xs text-text-muted">Sign in with your password. {t?.running ? "A new internet link can take up to a minute to start working. It changes each time you turn it on, and works only while this computer and KeiroLake are running." : ""}</p>
          </li>
        )}
      </ol>
      <ErrorNote error={error || t?.error || ""} onClose={() => setError("")} />
      {!compact && (
        <p className="mt-4 flex items-start gap-1.5 text-xs text-text-muted"><ShieldCheck size={14} className="mt-0.5 shrink-0" />
          Anyone with the link can see the sign-in page, so use a long password. Five wrong tries lock that visitor out for five minutes. The internet link uses Cloudflare's free tunnel program, downloaded once and checked against its published checksum. For a permanent address, use Tailscale.</p>
      )}
    </Card>
  );
}
