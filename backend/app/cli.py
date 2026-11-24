"""keirolake CLI: run the engine without a window (`keirolake serve`) and drive it from scripts.

The other commands talk to a running engine: the one `keirolake serve` started (found through its serve.json),
or KEIROLAKE_URL (and KEIROLAKE_TOKEN) for any other, such as the Docker stack on http://localhost:8000.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
import typer

app = typer.Typer(help="KeiroLake: your private data lake, local AI and data API, from the command line.", no_args_is_help=True)


def _target() -> tuple[str, str]:
    """Where the engine is, and its token: KEIROLAKE_URL, else the engine `keirolake serve` started, else Docker's."""
    if os.getenv("KEIROLAKE_URL"):
        return os.environ["KEIROLAKE_URL"].rstrip("/"), os.getenv("KEIROLAKE_TOKEN", "")
    from app.serve import read_info

    info = read_info()
    if info:
        return info["url"], info["token"]
    return "http://localhost:8000", ""


BASE, TOKEN = "", ""


def _base() -> str:
    global BASE, TOKEN
    if not BASE:
        url, TOKEN = _target()
        BASE = url + "/api"
    return BASE


def _headers() -> dict:
    _base()
    return {"X-KeiroLake-Token": TOKEN} if TOKEN else {}


def call(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        resp = httpx.request(method, _base() + path, timeout=kwargs.pop("timeout", 120), headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        typer.secho(f"Cannot reach {_base()}: {exc}\nStart it with: keirolake serve   (or docker compose up -d)", fg="red", err=True)
        raise typer.Exit(1) from exc
    if resp.status_code >= 400:
        detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        typer.secho(f"{resp.status_code}: {detail}", fg="red", err=True)
        raise typer.Exit(1)
    return resp


def kb_id_for(name: str) -> str:
    for kb in call("GET", "/kbs").json():
        if name in (kb["name"], kb["slug"], kb["id"]):
            return kb["id"]
    typer.secho(f"Knowledge base {name!r} not found. Create it with: keirolake kb create {name}", fg="red", err=True)
    raise typer.Exit(1)


def wait_job(job_id: str) -> dict:
    last = ""
    while True:
        job = call("GET", f"/jobs/{job_id}").json()
        line = f"[{job['status']}] {int(job['progress'] * 100):3d}%  {job['message']}"
        if line != last:
            typer.echo(line)
            last = line
        if job["status"] in {"done", "failed"}:
            if job["status"] == "failed":
                raise typer.Exit(1)
            return job
        time.sleep(1)


kb_app = typer.Typer(help="Manage knowledge bases")
app.add_typer(kb_app, name="kb")


@kb_app.command("create")
def kb_create(name: str, description: str = ""):
    typer.echo(json.dumps(call("POST", "/kbs", json={"name": name, "description": description}).json(), indent=2))


@kb_app.command("list")
def kb_list():
    for kb in call("GET", "/kbs").json():
        typer.echo(f"{kb['name']:<24} {kb['documents']:>4} docs  {kb['chunks']:>6} chunks  {', '.join(m['model'] for m in kb['models']) or '-'}")


@kb_app.command("reembed")
def kb_reembed(name: str):
    wait_job(call("POST", f"/kbs/{kb_id_for(name)}/reembed").json()["job_id"])


@app.command()
def ingest(kb: str, paths: list[Path] = typer.Argument(None, help="Files to ingest"),
           url: list[str] = typer.Option([], "--url", help="URL to fetch via KeiroLabs (repeatable)"),
           web: str = typer.Option("", "--web", help="Keiro web search query to ingest"),
           mode: str = typer.Option("content", help="Web mode: lite | fast | content"),
           n: int = typer.Option(5, "--results", help="Web results"),
           research: str = typer.Option("", "--research", help="Question for a KeiroLabs deep research dossier"),
           site: str = typer.Option("", "--site", help="Website whose newest pages (from its sitemap) to index")):
    """Ingest files, URLs, a web search, a research question or a website into a knowledge base."""
    kid = kb_id_for(kb)
    jobs = []
    if paths:
        files = [("files", (p.name, p.read_bytes())) for p in paths]
        jobs.append(call("POST", f"/kbs/{kid}/ingest/files", files=files).json()["job_id"])
    if url:
        jobs.append(call("POST", f"/kbs/{kid}/ingest/urls", json={"urls": url}).json()["job_id"])
    if web:
        jobs.append(call("POST", f"/kbs/{kid}/ingest/web-search", json={"query": web, "mode": mode, "max_results": n}).json()["job_id"])
    if research:
        jobs.append(call("POST", f"/kbs/{kid}/ingest/research", json={"question": research}).json()["job_id"])
    if site:
        jobs.append(call("POST", f"/kbs/{kid}/ingest/site", json={"site": site, "limit": n * 4}).json()["job_id"])
    if not jobs:
        typer.secho("Nothing to ingest: pass files, --url, --web, --research or --site", fg="yellow")
        raise typer.Exit(1)
    for job_id in jobs:
        typer.echo(json.dumps(wait_job(job_id)["result"], indent=2))


@app.command()
def search(kb: str, query: str, k: int = 5):
    """Hybrid (vector + keyword) search."""
    data = call("POST", f"/kbs/{kb_id_for(kb)}/search", json={"query": query, "top_k": k}).json()
    for w in data["warnings"]:
        typer.secho(f"! {w}", fg="yellow")
    for i, h in enumerate(data["hits"], 1):
        typer.secho(f"[{i}] {h['title']}  (rrf {h['score']}, cosine {h['cosine']}, {', '.join(h['matched_by'])})", bold=True)
        typer.echo("    " + h["text"][:240].replace("\n", " "))


@app.command()
def chat(kb: str, question: str, web: str = typer.Option("auto", help="auto | always | never")):
    """Ask a question; the answer streams with citations."""
    body = {"kb_id": kb_id_for(kb), "question": question, "web": web}
    sources = {}
    with httpx.stream("POST", _base() + "/chat", json=body, timeout=300, headers=_headers()) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:])
            kind = event["type"]
            if kind == "token":
                typer.echo(event["text"], nl=False)
            elif kind == "status":
                typer.secho(f"({event['message']})", fg="yellow", err=True)
            elif kind == "meta":
                typer.secho(f"model: {event['model']}", fg="cyan", err=True)
            elif kind == "sources":
                sources = event
            elif kind == "error":
                typer.secho(event["message"], fg="red", err=True)
                raise typer.Exit(1)
    typer.echo("\n\nSources:")
    for i, h in enumerate(sources.get("local", []), 1):
        typer.echo(f"  [{i}] {h['title']} {h.get('url') or ''}")
    for i, w in enumerate(sources.get("web", []), 1):
        typer.echo(f"  [W{i}] {w['title']} {w['url']}")


@app.command()
def sql(query: str):
    """Run read-only SQL against the SQLite warehouse."""
    data = call("POST", "/warehouse/query", json={"sql": query}).json()
    typer.echo(" | ".join(data["columns"]))
    for row in data["rows"]:
        typer.echo(" | ".join("" if v is None else str(v) for v in row))
    typer.secho(f"{data['row_count']} row(s) in {data['elapsed_ms']} ms", fg="green", err=True)


@app.command()
def dataset(path: Path, name: str = ""):
    """Load a CSV / JSON / Excel file into the warehouse."""
    resp = call("POST", "/datasets", files={"file": (path.name, path.read_bytes())}, data={"name": name})
    typer.echo(json.dumps(resp.json(), indent=2))


@app.command()
def providers(probe: bool = typer.Option(False, help="Call every model once")):
    typer.echo(json.dumps(call("GET", "/providers", params={"probe": probe}, timeout=300).json(), indent=2))


# ---------- run it without a window ----------

@app.command()
def serve(share: str = typer.Option("local", help="local (this computer) | wifi (your network, HTTPS) | internet (adds a private link) | open (a port you open)"),
          port: int = typer.Option(8443, help="Port for the web app, /v1 and /mcp"),
          data: Path = typer.Option(None, help="Data folder (default: your user data folder, keirolake-server)"),
          password_file: Path = typer.Option(None, help="File holding the sign-in password (or set KEIROLAKE_PASSWORD)"),
          keep_awake: bool = typer.Option(False, help="Stop this computer sleeping while it runs")):
    """Run KeiroLake headless: the full engine, web app, API, MCP and local models, with no desktop window."""
    from app import serve as _serve

    folder = data or _serve.default_data_dir()
    password = password_file.read_text(encoding="utf-8").strip() if password_file else os.getenv("KEIROLAKE_PASSWORD")
    if not password and sys.stdin.isatty() and not (folder / "keirolake.db").exists():
        import getpass

        password = getpass.getpass("Choose a sign-in password (10+ characters): ")
    _serve.run(folder, share, port, password or None, keep_awake)


def call_ok(path: str) -> bool:
    try:
        return httpx.get(_base() + path, headers=_headers(), timeout=10).status_code < 400
    except httpx.HTTPError:
        return False


@app.command()
def status():
    """What's running: version, sharing addresses, the internet link and running jobs."""
    about = call("GET", "/about").json()
    net = call("GET", "/access").json().get("network", {}) if call_ok("/access") else {}
    tunnel = call("GET", "/network/tunnel").json()
    typer.secho(f"KeiroLake {about['version']} · data {about['data_dir']}", bold=True)
    for url in net.get("urls", []):
        typer.echo(f"  open:          {url}")
    typer.echo(f"  internet link: {tunnel.get('url') or ('starting' if tunnel.get('running') else 'off')}")
    jobs = [j for j in call("GET", "/jobs", params={"limit": 20}).json() if j["status"] in ("running", "queued")]
    typer.echo(f"  running jobs:  {len(jobs)}")
    for j in jobs:
        typer.echo(f"    {j['title']} {int(j['progress'] * 100)}%")


@app.command()
def link(action: str = typer.Argument("show", help="show | on | off")):
    """The private internet link: show it, or turn it on or off."""
    if action in ("on", "off"):
        call("POST", f"/network/tunnel/{'start' if action == 'on' else 'stop'}", timeout=180)
    st = call("GET", "/network/tunnel").json()
    typer.echo(st.get("url") or ("Starting..." if st.get("running") else st.get("error") or "Off"))


keys_app = typer.Typer(help="API keys for your apps and AI tools")
app.add_typer(keys_app, name="keys")


@keys_app.command("create")
def keys_create(name: str, scope: list[str] = typer.Option(["read", "llm"], help="read | ask | write | sql | llm (repeatable)"),
                days: int = typer.Option(0, help="Expire after this many days (0: never)")):
    """Make a key; it is printed once."""
    out = call("POST", "/access/keys", json={"name": name, "scopes": scope, "expires_days": days or None}).json()
    typer.secho("Copy this key now; it isn't shown again:", fg="yellow", err=True)
    typer.echo(out.get("raw") or out.get("token") or out.get("secret"))


@keys_app.command("list")
def keys_list():
    for k in call("GET", "/access").json()["keys"]:
        typer.echo(f"{k['id'][:8]}  {k['name']:<24} {','.join(k['scopes']):<20} {k['prefix']}...  {'revoked' if k.get('revoked') else 'active'}")


@keys_app.command("revoke")
def keys_revoke(key_id: str):
    match = [k for k in call("GET", "/access").json()["keys"] if k["id"].startswith(key_id)]
    if len(match) != 1:
        typer.secho("Give enough of the id to match exactly one key (see keirolake keys list)", fg="red", err=True)
        raise typer.Exit(1)
    call("POST", f"/access/keys/{match[0]['id']}/revoke")
    typer.secho(f"Revoked {match[0]['name']}", fg="green")


@app.command()
def tables():
    """Your tables, with row counts."""
    for t in call("GET", "/sheets").json():
        typer.echo(f"{t['id'][:8]}  {t['name']:<40} {t.get('rows') or 0:>7} rows")


@app.command()
def build(request: str, rows: int = typer.Option(20, help="How many rows to write"),
          library: bool = typer.Option(False, "--library", help="Write an offline library of notes instead of a table"),
          web: bool = typer.Option(False, help="Use web search for sourced values"),
          local: bool = typer.Option(True, help="Only models on this computer")):
    """Build a table (or a library) from one sentence: the AI plans it, then fills it."""
    s = call("POST", "/builder/sessions", json={"kind": "collection" if library else "table"}).json()
    call("PUT", f"/builder/sessions/{s['id']}/options", json={"web": web, "local_only": local})
    wait_job(call("POST", f"/builder/sessions/{s['id']}/say", json={"text": request + " Use your best judgement and go ahead."}).json()["job_id"])
    s = call("GET", f"/builder/sessions/{s['id']}").json()
    typer.echo(s["messages"][-1]["content"])
    for _ in range(2):  # the model often asks to confirm: on the command line, the answer is yes
        if s["ready"]:
            break
        d = s.get("draft") or {}
        if d.get("name") and (d.get("sections") if library else d.get("columns")):
            s = call("PUT", f"/builder/sessions/{s['id']}/draft", json=d).json()  # a complete plan counts as agreed
            break
        wait_job(call("POST", f"/builder/sessions/{s['id']}/say", json={"text": "Yes, that's right. Go ahead with your plan."}).json()["job_id"])
        s = call("GET", f"/builder/sessions/{s['id']}").json()
    if not s["ready"]:
        typer.secho("The plan isn't complete yet. Continue in the app under Build with AI.", fg="yellow")
        raise typer.Exit(1)
    d = s["draft"]
    typer.secho(f"\n{d['name']}: " + (f"{len(d['sections'])} sections" if library else ", ".join(c["name"] for c in d["columns"])), bold=True)
    wait_job(call("POST", f"/builder/sessions/{s['id']}/fill", json={"count": rows}).json()["job_id"])
    typer.echo(call("GET", f"/builder/sessions/{s['id']}").json()["messages"][-1]["content"])


@app.command()
def update():
    """Is there a newer KeiroLake on GitHub?"""
    u = call("GET", "/updates", params={"force": True}).json()
    if u["available"]:
        typer.secho(f"KeiroLake {u['version']} is available (you have {u['current']}): {u['url']}", fg="green")
    else:
        typer.echo(u.get("error") or f"KeiroLake {u['current']} is the newest version.")


if __name__ == "__main__":
    app()

