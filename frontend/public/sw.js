// KeiroLake web app: keeps the app's own files on the device so it opens instantly and shows a clear message when the
// lake can't be reached. Your data is never cached here: /api, /v1 and /mcp always go to the network.
const CACHE = "keirolake-shell-v2";
const SHELL = ["/", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (/^\/(api|v1|mcp)(\/|$)/.test(url.pathname)) return;  // live data only

  if (e.request.mode === "navigate") {  // the page: newest when online, the saved copy when not
    e.respondWith(fetch(e.request).then((r) => { caches.open(CACHE).then((c) => c.put("/", r.clone())); return r; })
      .catch(() => caches.match("/")));
    return;
  }
  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/icons/")) {  // fingerprinted files never change
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request).then((r) => {
      if (r.ok) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
      return r;
    })));
  }
});

// "Share to KeiroLake" (Android): the share sheet POSTs here. Items go into the same on-device queue the app uses
// (IndexedDB "keirolake" → "queue"), then the Capture page opens and sends them, or keeps them until the lake is reachable.
const QUEUE_DB = "keirolake", QUEUE = "queue";
function openQueue() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(QUEUE_DB, 1);
    req.onupgradeneeded = () => { if (!req.result.objectStoreNames.contains(QUEUE)) req.result.createObjectStore(QUEUE, { keyPath: "id" }); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function queueShared(form) {
  const db = await openQueue();
  const items = [];
  const now = Date.now();
  for (const f of form.getAll("files")) {
    if (!(f instanceof File) || !f.size) continue;
    const kind = f.type.startsWith("image/") ? "image" : f.type.startsWith("audio/") ? "audio" : "file";
    items.push({ id: crypto.randomUUID(), kind, name: f.name || "shared", blob: f, note: form.get("title") || "", created: now });
  }
  // A shared link is saved as its readable page (apps often put the link in "text" instead of "url").
  const shared = String(form.get("text") || "").trim();
  const text = String(form.get("url") || "").trim() || (shared.match(/https?:\/\/\S+/) || [shared])[0];
  if (text) items.push({ id: crypto.randomUUID(), kind: "text", name: "Shared text", text, note: form.get("title") || "", created: now });
  await new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE, "readwrite");
    for (const it of items) tx.objectStore(QUEUE).put(it);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
  return items.length;
}
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === "POST" && url.origin === location.origin && url.pathname === "/share-target") {
    e.respondWith(e.request.formData().then(queueShared).catch(() => 0).then(() => Response.redirect("/?page=Capture&shared=1", 303)));
  }
});
