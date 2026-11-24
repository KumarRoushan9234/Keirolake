export type KB = {
  id: string; name: string; description: string; documents: number; chunks: number;
  models: { model: string; points: number }[];
};
export type Job = { id: string; title?: string; status: string; progress: number; message: string; result: Record<string, any> };
export type Library = {
  id: string; name: string; title: string; summary: string; filename: string; path: string; size_bytes: number;
  articles: number; language: string; issued: string; status: string; message: string; enabled: boolean;
  index_tier: number; indexed: number; index_model: string; url: string;
};

/** Running inside the desktop shell (Tauri) rather than a browser. */
export const isDesktop = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

let base = "";
let token = "";

/** In the desktop app the engine runs on a random port with a per-launch token; ask the shell for both. */
export async function connect(): Promise<void> {
  if (!isDesktop) return;
  const { invoke } = await import("@tauri-apps/api/core");
  for (let i = 0; i < 240; i++) {
    const info = await invoke<{ url: string; token: string; ready: boolean; error?: string }>("engine_info");
    if (info.error) throw new Error(info.error);
    base = info.url;
    token = info.token;
    if (info.ready) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("The engine did not start");
}

const headers = (json: boolean): Record<string, string> => ({
  ...(json ? { "Content-Type": "application/json" } : {}),
  ...(token ? { "X-KeiroLake-Token": token } : {}),
});

/** Only http(s) URLs become links: web results are untrusted and could carry javascript: URLs. */
export const safeUrl = (url?: string | null) => (url && /^https?:\/\//i.test(url) ? url : undefined);

/** fetch that waits out a short engine restart (connection refused) instead of failing at once. */
async function call(url: string, init: RequestInit, tries = 6): Promise<Response> {
  for (let i = 0; ; i++) {
    try { return await fetch(url, init); }
    catch (e) {
      if (i >= tries - 1 || init.method && init.method !== "GET") throw new Error("Can't reach the KeiroLake engine. It may be restarting; try again in a few seconds.");
      await new Promise((r) => setTimeout(r, 500 * 2 ** Math.min(i, 3)));
    }
  }
}

export async function api<T = any>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  const res = await call(base + "/api" + path, {
    ...rest,
    headers: headers(json !== undefined),
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (res.status === 401 && !isDesktop) window.dispatchEvent(new Event("keirolake:signin"));
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d: any) => d.msg).join("; ") : body.detail;
    throw new Error(detail ?? `${res.status} ${res.statusText}`);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

/** URL of a page inside an offline library, for the Reader iframe. */
export const zimUrl = (libId: string, path = "") =>
  `${base}/api/zim/${token || "-"}/${libId}/${path.split("/").map(encodeURIComponent).join("/")}`;

/** Poll a background job until it finishes. Throws if it failed. */
export async function waitJob(id: string, onUpdate?: (job: Job) => void): Promise<Job> {
  for (;;) {
    const job = await api<Job>(`/jobs/${id}`);
    onUpdate?.(job);
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error(job.message || "Job failed");
    await new Promise((r) => setTimeout(r, 1000));
  }
}

/** Download an engine file (sent with the auth header) and save it through the browser. Returns the file name. */
export async function base64Download(path: string): Promise<string> {
  const res = await call(base + "/api" + path, { headers: headers(false) });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "")?.[1] ?? "download";
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  return name;
}

export const errText = (e: unknown) => String(e instanceof Error ? e.message : e);

export const gb = (bytes: number) => (bytes >= 2 ** 30 ? `${(bytes / 2 ** 30).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 2 ** 20))} MB`);

export type ChatEvent =
  | { type: "status"; message: string }
  | { type: "meta"; model: string }
  | { type: "token"; text: string }
  | { type: "sources"; local: any[]; web: any[]; library: any[]; private: any[] }
  | { type: "safety"; category: string; message: string }
  | { type: "error"; message: string }
  | { type: "done" };

/** POST /chat and yield server-sent events as they arrive. */
export async function* chat(body: unknown): AsyncGenerator<ChatEvent> {
  const res = await fetch(base + "/api/chat", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
  if (!res.ok || !res.body) throw new Error(`Chat failed: ${res.status}`);
  yield* sse(res);
}

/** Follow a background answer: replays what was already said, then streams live until it ends. */
export async function* follow(messageId: string, after = 0, signal?: AbortSignal): AsyncGenerator<any> {
  const res = await call(`${base}/api/conversations/messages/${messageId}/events?after=${after}`, { headers: headers(false), signal });
  if (!res.ok || !res.body) throw new Error(`Couldn't follow the answer: ${res.status}`);
  yield* sse(res);
}

async function* sse(res: Response): AsyncGenerator<any> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const data = frame.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("");
      if (data) yield JSON.parse(data);
    }
  }
}

/** Desktop-only shell commands (no-ops in the browser). */
export async function shell<T = any>(cmd: string, args?: Record<string, unknown>): Promise<T | undefined> {
  if (!isDesktop) return undefined;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(cmd, args);
}

export async function onShellEvent<T>(name: string, fn: (payload: T) => void): Promise<() => void> {
  if (!isDesktop) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return listen<T>(name, (e) => fn(e.payload));
}


export type AuthState = { network: boolean; password_set: boolean; signed_in: boolean };

/** Browser tabs (not the desktop window) may need to sign in; a #code=... link from the tray signs in without a password. */
export async function authState(): Promise<AuthState> {
  const code = /[#&]code=([^&]+)/.exec(location.hash)?.[1];
  if (code) {
    history.replaceState(null, "", location.pathname + location.search);
    await fetch(base + "/api/auth/exchange", { method: "POST", headers: headers(true), body: JSON.stringify({ code: decodeURIComponent(code) }) });
  }
  const res = await fetch(base + "/api/auth/status", { headers: headers(false) });
  return res.json();
}

export async function signIn(password: string): Promise<void> {
  const res = await fetch(base + "/api/auth/login", { method: "POST", headers: headers(true), body: JSON.stringify({ password }) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? "Couldn't sign in");
}
