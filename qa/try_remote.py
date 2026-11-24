"""Try your lake from anywhere: the data API, the LLM gateway and MCP, through your internet link (or any address).

    python qa/try_remote.py https://your-link.trycloudflare.com klk_your_key
    python qa/try_remote.py https://192.168.1.20:8443 klk_your_key --insecure   (home Wi-Fi: a certificate made on your PC)

Make the key in KeiroLake under Serve > API keys, with the permissions you want to try (read, llm, sql). Nothing is
written: it only lists, reads and asks one short question.
"""
import json
import socket
import sys
import time

import httpx

args = [a for a in sys.argv[1:] if a != "--insecure"]
if len(args) != 2:
    sys.exit(__doc__)
base, key = args[0].rstrip("/"), args[1]
host = base.split("//")[-1].split("/")[0].split(":")[0]
for _ in range(40):  # a brand-new internet link can take a minute to appear in DNS
    try:
        socket.gethostbyname(host)
        break
    except OSError:
        print("Waiting for the link's name to resolve…")
        time.sleep(3)
c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=120, verify="--insecure" not in sys.argv)


def show(name, resp, pick=lambda b: b):
    ok = resp.status_code < 400
    try:
        body = pick(resp.json()) if ok else resp.json().get("detail", resp.text[:120])
    except ValueError:
        body = resp.text[:120]
    print(f"{'OK  ' if ok else 'FAIL'} {resp.status_code}  {name}: {json.dumps(body, ensure_ascii=False)[:160]}")
    return ok


show("who am I", c.get("/v1/me"), lambda b: {"key": b["name"], "scopes": b["scopes"]})
show("tables", c.get("/v1/tables"), lambda b: [f"{t['name']} ({t['rows']} rows)" for t in b][:5])
show("collections", c.get("/v1/kbs"), lambda b: [k["name"] for k in b][:5])
show("models (llm)", c.get("/v1/models"), lambda b: [m["id"] for m in b["data"]][:6])
show("chat (llm)", c.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "Say hello in five words."}], "max_tokens": 30}),
     lambda b: b["choices"][0]["message"]["content"])
mcp = lambda method, params=None: c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})  # noqa: E731
show("mcp initialize", mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "try_remote", "version": "1"}}),
     lambda b: b["result"]["serverInfo"])
show("mcp tools", mcp("tools/list"), lambda b: [t["name"] for t in b["result"]["tools"]])
show("mcp list_tables", mcp("tools/call", {"name": "list_tables", "arguments": {}}), lambda b: b["result"]["content"][0]["text"][:150])
