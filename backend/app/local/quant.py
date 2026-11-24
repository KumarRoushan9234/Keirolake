"""Quantized local models: choose the precision, check it exists, download it, re-quantize, tune the runtime.

Everything here is data, never code. Models are GGUF files (Ollama's own format) or safetensors; the app
never loads pickles, never passes trust_remote_code, and never runs scripts that ship with a model repo.
Ollama does the loading, so a downloaded model can't execute anything on this computer.

A "quant" is how many bits each weight keeps: fewer bits make the model smaller and faster and a little less
accurate. Q4_K_M is the usual sweet spot; Q8_0 is almost lossless; Q2/Q3 are for very small memory budgets.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import httpx

from app import http
from app.config import get_settings
from app.local import ollama

log = logging.getLogger(__name__)

# id, Ollama tag suffix, approximate bits per weight, one-line note
QUANTS = [
    {"id": "Q2_K", "tag": "q2_K", "bpw": 2.96, "note": "Smallest, noticeably less accurate. Only for tight memory."},
    {"id": "Q3_K_M", "tag": "q3_K_M", "bpw": 3.91, "note": "Small, some quality loss."},
    {"id": "Q4_0", "tag": "q4_0", "bpw": 4.55, "note": "Older 4-bit, fast."},
    {"id": "Q4_K_M", "tag": "q4_K_M", "bpw": 4.85, "note": "Best balance of size and quality. The usual choice."},
    {"id": "Q5_K_M", "tag": "q5_K_M", "bpw": 5.69, "note": "Closer to the original."},
    {"id": "Q6_K", "tag": "q6_K", "bpw": 6.59, "note": "Very close to the original."},
    {"id": "Q8_0", "tag": "q8_0", "bpw": 8.5, "note": "Nearly lossless, about half the size of F16."},
    {"id": "F16", "tag": "fp16", "bpw": 16.0, "note": "Full 16-bit precision. The source for re-quantizing."},
]
BY_ID = {q["id"]: q for q in QUANTS}
SAFE_EXTENSIONS = {".gguf", ".safetensors", ".json", ".txt", ".md", ".model", ".jinja", ".tiktoken"}
UNSAFE_EXTENSIONS = {".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".py", ".sh", ".exe", ".dll"}
REGISTRY = "https://registry.ollama.ai/v2"
HF = "https://huggingface.co"

RUNTIME_KEYS = {
    "OLLAMA_CONTEXT_LENGTH": ("Context length (tokens)", ["", "2048", "4096", "8192", "16384", "32768"]),
    "OLLAMA_KV_CACHE_TYPE": ("Key/value cache precision", ["", "f16", "q8_0", "q4_0"]),
    "OLLAMA_FLASH_ATTENTION": ("Flash attention", ["", "1", "0"]),
    "OLLAMA_KEEP_ALIVE": ("Keep a model loaded for", ["", "5m", "30m", "2h", "-1"]),
    "OLLAMA_MAX_LOADED_MODELS": ("Models loaded at once", ["", "1", "2", "3"]),
    "OLLAMA_NUM_PARALLEL": ("Parallel requests per model", ["", "1", "2", "4"]),
}


class QuantError(ValueError):
    pass


# ---------- sizes and fit ----------
def parse_params_b(name: str) -> float | None:
    """Billions of parameters from a model name or tag: 'qwen2.5:7b-instruct' -> 7.0, 'phi:3.8b' -> 3.8, '0.5b' -> 0.5."""
    m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", name.replace("_", "-"))
    return float(m.group(1)) if m else None


def estimate(params_b: float, quant: str, ctx: int = 4096, kv: str = "f16") -> dict:
    """Memory a model needs: weights plus the context (key/value cache). Both are estimates."""
    q = BY_ID.get(quant)
    if q is None:
        raise QuantError(f"Unknown quantization {quant}")
    weights = params_b * q["bpw"] / 8
    per_token_mb = 0.06 * (params_b / 7) ** 0.8 * {"f16": 1.0, "q8_0": 0.5, "q4_0": 0.25}.get(kv, 1.0)
    cache = per_token_mb * ctx / 1024
    return {"weights_gb": round(weights, 1), "cache_gb": round(cache, 1), "total_gb": round(weights + cache + 0.5, 1)}


def fit(total_gb: float, hw: dict) -> dict:
    """Where it will run on this computer: fully on the GPU, split with RAM, RAM only, or not at all."""
    vram, ram = hw.get("vram_gb", 0), hw.get("ram_gb", 0)
    if vram and total_gb <= vram * 0.92:
        return {"where": "gpu", "label": f"Fits in GPU memory ({vram} GB)"}
    if total_gb <= (vram + ram * 0.6):
        return {"where": "split" if vram else "ram", "label": "Runs split between GPU and RAM, slower" if vram else f"Runs on the CPU from RAM ({ram} GB), slower"}
    return {"where": "no", "label": f"Too big for this computer ({total_gb} GB needed)"}


# ---------- sources ----------
def ollama_model_name(ref: str, quant: str) -> str:
    """'qwen2.5:7b-instruct' + Q4_K_M -> 'qwen2.5:7b-instruct-q4_K_M' (Ollama's tag convention)."""
    ref = ref.strip()
    if ":" not in ref:
        raise QuantError("Name the size too, like qwen2.5:7b-instruct")
    if re.search(r"-(q\d_[A-Za-z0-9_]+|fp16|q8_0)$", ref):
        return ref
    return f"{ref}-{BY_ID[quant]['tag']}"


def hf_model_name(repo: str, quant: str) -> str:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo.strip()):
        raise QuantError("Use a Hugging Face repository like bartowski/Qwen2.5-7B-Instruct-GGUF")
    return f"hf.co/{repo.strip()}:{quant}"


def model_name(kind: str, ref: str, quant: str) -> str:
    if quant not in BY_ID:
        raise QuantError(f"Unknown quantization {quant}")
    if kind == "ollama":
        return ollama_model_name(ref, quant)
    if kind == "hf":
        return hf_model_name(ref, quant)
    raise QuantError("Source must be ollama or hf")


async def _registry_exists(name: str) -> tuple[bool, int]:
    repo, _, tag = name.partition(":")
    repo = repo if "/" in repo else f"library/{repo}"
    try:
        resp = await http.client("registry").get(f"{REGISTRY}/{repo}/manifests/{tag}", timeout=15,
                                                 headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    except httpx.HTTPError as exc:
        raise QuantError(f"Couldn't reach the Ollama library: {exc}") from exc
    if resp.status_code != 200:
        return False, 0
    size = sum(layer.get("size", 0) for layer in resp.json().get("layers", []))
    return True, size


def quant_of_filename(name: str) -> str | None:
    m = re.search(r"((?:IQ|Q)\d(?:_[A-Z0-9]+)+|F16|BF16|F32)(?=[.\-_]|$)", name.upper().replace("-", "_"))
    return m.group(1) if m else None


async def hf_files(repo: str) -> dict:
    """The GGUF files of a Hugging Face repository with their quantization and size. Anything else is listed as skipped."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo.strip()):
        raise QuantError("Use a Hugging Face repository like bartowski/Qwen2.5-7B-Instruct-GGUF")
    try:
        resp = await http.client("hf").get(f"{HF}/api/models/{repo.strip()}", params={"blobs": "true"}, timeout=20)
    except httpx.HTTPError as exc:
        raise QuantError(f"Couldn't reach Hugging Face: {exc}") from exc
    if resp.status_code == 404:
        raise QuantError("That repository doesn't exist (or is private)")
    if resp.status_code != 200:
        raise QuantError(f"Hugging Face answered {resp.status_code}")
    gguf, skipped = [], []
    for f in resp.json().get("siblings", []):
        name = f.get("rfilename", "")
        ext = Path(name).suffix.lower()
        if ext == ".gguf":
            gguf.append({"file": name, "quant": quant_of_filename(name), "size_gb": round((f.get("size") or 0) / 1e9, 2)})
        elif ext in UNSAFE_EXTENSIONS:
            skipped.append(name)
    return {"repo": repo.strip(), "gguf": gguf, "skipped": skipped,
            "note": "Only GGUF files are downloaded. Python files and pickled weights in the repository are never used." if skipped else ""}


async def check(kind: str, ref: str, quant: str, hw: dict | None = None, ctx: int = 4096, kv: str = "f16") -> dict:
    """Is this exact model and quantization downloadable, how big is it, and will it fit here?"""
    name = model_name(kind, ref, quant)
    out: dict = {"name": name, "kind": kind, "quant": quant}
    if kind == "ollama":
        ok, size = await _registry_exists(name)
        out.update(available=ok, size_gb=round(size / 1e9, 2) if ok else None)
    else:
        listing = await hf_files(ref)
        match = [f for f in listing["gguf"] if f["quant"] == quant.upper()]
        out.update(available=bool(match), size_gb=match[0]["size_gb"] if match else None, files=listing["gguf"], skipped=listing["skipped"])
    params = parse_params_b(ref)
    if params and hw is not None:
        est = estimate(params, quant, ctx, kv)
        out.update(estimate=est, fit=fit(est["total_gb"], hw))
    return out


# ---------- what is installed ----------
async def installed() -> list[dict]:
    """Local models with their real quantization, parameter count and format, from Ollama itself."""
    status = await ollama.status()
    out = []
    for m in status["models"]:
        if not m["local"]:
            continue
        details: dict = {}
        try:
            resp = await http.client("ollama").post(ollama.base_url() + "/api/show", json={"model": m["name"]}, timeout=10)
            if resp.status_code == 200:
                details = resp.json().get("details", {})
        except httpx.HTTPError:
            pass
        out.append({"name": m["name"], "size_gb": m["size_gb"], "quant": details.get("quantization_level"), "params": details.get("parameter_size"),
                    "format": details.get("format"), "family": details.get("family"), "loaded": m["name"] in status["loaded"]})
    return out


# ---------- jobs ----------
def pull_job(kind: str, ref: str, quant: str):
    """Download a quantized model. Ollama pulls GGUF layers only."""
    name = model_name(kind, ref, quant)
    base = ollama.pull_job(name)

    async def job(ctx) -> dict:
        result = await base(ctx)
        _remember(name, {"source": kind, "ref": ref, "quant": quant})
        return {**result, "quant": quant}

    return job


def quantize_job(source: str, quant: str, new_name: str | None = None):
    """Make a smaller copy of a downloaded model (works from F16/F32 models; Ollama refuses already-quantized sources)."""
    if quant not in BY_ID or quant == "F16":
        raise QuantError("Pick a quantization smaller than F16")
    target = (new_name or f"{source.split(':')[0]}:{source.split(':')[-1]}-{BY_ID[quant]['tag']}").strip()
    if not re.fullmatch(r"[\w./:-]+", target):
        raise QuantError("Use letters, numbers and - . / : in the new model name")

    async def job(ctx) -> dict:
        await ollama.start()
        await ctx.progress(0.02, f"Quantizing {source} to {quant}")
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", ollama.base_url() + "/api/create",
                                     json={"model": target, "from": source, "quantize": BY_ID[quant]["tag"], "stream": True}) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"Ollama couldn't quantize: HTTP {resp.status_code} {(await resp.aread())[:200]!r}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if "error" in event:
                        raise RuntimeError(f"Ollama: {event['error']}")
                    total, done = event.get("total"), event.get("completed")
                    await ctx.progress(done / total if total and done is not None else -1, event.get("status", "working"))
        _remember(target, {"source": "quantized", "ref": source, "quant": quant})
        return {"model": target, "quant": quant}

    return job


def _meta_path() -> Path:
    return get_settings().data_dir / "models" / "quantized.json"


def _remember(name: str, info: dict) -> None:
    path = _meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = info
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def remembered() -> dict:
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------- runtime ----------
def runtime_values() -> dict:
    return {k: os.getenv(k, "") for k in RUNTIME_KEYS}


def validate_runtime(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key not in RUNTIME_KEYS:
            raise QuantError(f"Unknown setting {key}")
        value = str(value).strip()
        if value not in RUNTIME_KEYS[key][1]:
            raise QuantError(f"{RUNTIME_KEYS[key][0]}: choose one of {', '.join(v or 'default' for v in RUNTIME_KEYS[key][1])}")
        out[key] = value
    return out
