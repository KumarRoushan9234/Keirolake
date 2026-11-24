"""Run Heretic (decensoring by directional ablation) on a model, from the app, as a separate program.

Heretic is AGPL-3.0 software, so KeiroLake does not bundle or import it: it is used from the folder the user points
to (HERETIC_DIR), installed into its own virtual environment, and started as a child process. It reads the same
settings the command line does, so every prompt it would ask is answered up front with flags and the run needs no
keyboard. Results go to a folder of safetensors, which can then be imported into Ollama and quantized.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.local import ollama, quant

log = logging.getLogger(__name__)
CONFIG_RE = re.compile(r"^config\.([\w-]+)\.toml$")
MODEL_RE = re.compile(r"^[\w.-]+/[\w.-]+$|^[A-Za-z]:[\\/][^\"<>|?*]+$|^/[^\0]+$")
TRIAL_RE = re.compile(r"[Tt]rial\s+(\d+)\s*(?:/|of)\s*(\d+)|^Progress (\d+) of (\d+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# Heretic builds every prompt object before checking whether its answer was already given on the command line, and
# building one needs a real console. Run from the app (output piped, no console) those prompts are replaced by stand-ins
# that only fail if Heretic actually needs an answer the app didn't pass.
LAUNCHER = """
import questionary
class _Unanswered:
    def __init__(self, *args, **kwargs):
        self.message = str(args[0]) if args else ""
    def ask(self, *args, **kwargs):
        raise SystemExit("Heretic asked something the app did not answer: " + self.message)
    unsafe_ask = ask
for _name in ("select", "text", "path", "password", "checkbox", "confirm"):
    setattr(questionary, _name, _Unanswered)
from heretic.main import run
run()
"""


class HereticError(RuntimeError):
    pass


def root() -> Path:
    return get_settings().data_dir / "heretic"


def source_dir() -> Path | None:
    raw = os.getenv("HERETIC_DIR", "").strip()
    p = Path(raw) if raw else None
    return p if p and (p / "pyproject.toml").exists() and (p / "src" / "heretic").exists() else None


def venv_python() -> Path:
    custom = os.getenv("HERETIC_PYTHON", "").strip()
    if custom:
        return Path(custom)
    return root() / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def configs(folder: Path | None = None) -> list[dict]:
    """Presets that ship with Heretic (config.<name>.toml), plus the default."""
    folder = folder or source_dir()
    if not folder:
        return []
    out = [{"id": "default", "file": "config.default.toml"}]
    for f in sorted(folder.glob("config.*.toml")):
        m = CONFIG_RE.match(f.name)
        if m and m.group(1) not in ("default", "classifier", "piqa"):
            out.append({"id": m.group(1), "file": f.name})
    return [c for c in out if (folder / c["file"]).exists()]


def _probe_code() -> str:
    return ("import json,importlib.metadata as m,torch;"
            "print(json.dumps({'heretic':m.version('heretic-llm'),'torch':torch.__version__,'cuda':torch.cuda.is_available(),"
            "'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}))")


def status() -> dict:
    src = source_dir()
    py = venv_python()
    out: dict = {"found": src is not None, "dir": str(src) if src else os.getenv("HERETIC_DIR", ""), "installed": False,
                 "configs": configs(src), "license": "AGPL-3.0-or-later"}
    if src and py.exists():
        try:
            res = subprocess.run([str(py), "-I", "-c", _probe_code()], capture_output=True, text=True, timeout=60, creationflags=NO_CONSOLE)
            if res.returncode == 0:
                out.update(json.loads(res.stdout.strip().splitlines()[-1]), installed=True)
            else:
                out["problem"] = (res.stderr.strip().splitlines() or ["Heretic is not installed in its environment yet"])[-1][:300]
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            out["problem"] = str(exc)[:300]
    return out


def find_python() -> list[str]:
    """A Python 3.10-3.13 to build the environment with (PyTorch wheels lag behind the newest Python)."""
    if sys.platform == "win32" and shutil.which("py"):
        for v in ("3.12", "3.11", "3.13", "3.10"):
            if subprocess.run(["py", f"-{v}", "-c", "pass"], capture_output=True, creationflags=NO_CONSOLE).returncode == 0:
                return ["py", f"-{v}"]
    for name in ("python3.12", "python3.11", "python3.13", "python3.10"):
        if shutil.which(name):
            return [name]
    raise HereticError("Heretic needs Python 3.10 to 3.13 and none was found. Install Python 3.12 and try again.")


def _torch_index() -> str:
    """CUDA wheels when an NVIDIA GPU is present (cu128 supports current RTX 50-series), otherwise the CPU build."""
    return "https://download.pytorch.org/whl/cu128" if shutil.which("nvidia-smi") else "https://download.pytorch.org/whl/cpu"


async def _run(ctx, args: list[str], label: str, log_file: Path, cwd: Path | None = None, env: dict | None = None, frac: tuple[float, float] = (0, 1)) -> None:
    proc = await asyncio.create_subprocess_exec(*args, cwd=str(cwd) if cwd else None, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, creationflags=NO_CONSOLE, limit=1 << 24)
    lo, hi = frac
    shown = -1.0
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {label}\n")
        assert proc.stdout
        async for raw in proc.stdout:
            line = ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
            if not line:
                continue
            m = TRIAL_RE.search(line)
            if m and m.group(3):  # pip's raw download progress in bytes: shown every half percent, not logged
                done, total = int(m.group(3)), max(1, int(m.group(4)))
                if done / total - shown >= 0.005 or done == total:
                    shown = done / total
                    await ctx.progress(lo + (hi - lo) * shown, f"Downloading {done / 1e9:.2f} of {total / 1e9:.2f} GB")
                continue
            fh.write(line + "\n")
            fh.flush()
            await ctx.progress(lo + (hi - lo) * int(m.group(1)) / max(1, int(m.group(2))) if m else -1, line[-200:])
    if await proc.wait() != 0:
        raise HereticError(f"{label} failed (exit {proc.returncode}). Last lines:\n" + "\n".join(tail(log_file, 6)))


def tail(path: Path, n: int = 80) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def setup_job():
    """Create Heretic's own environment and install PyTorch plus Heretic into it. Large download (several GB)."""
    src = source_dir()
    if not src:
        raise HereticError("Set the Heretic folder first (the one that contains pyproject.toml)")
    base = find_python()
    log_file = root() / "setup.log"

    async def job(ctx) -> dict:
        root().mkdir(parents=True, exist_ok=True)
        log_file.write_text("", encoding="utf-8")
        py = venv_python()
        if not py.exists():
            await ctx.progress(0.02, "Creating the environment")
            await _run(ctx, [*base, "-m", "venv", str(root() / "venv")], "create venv", log_file)
        await ctx.progress(0.05, "Updating pip")
        await _run(ctx, [str(py), "-m", "pip", "install", "--upgrade", "pip"], "pip upgrade", log_file)
        await ctx.progress(0.1, "Installing PyTorch (large download)")
        await _run(ctx, [str(py), "-m", "pip", "install", "--no-input", "--progress-bar", "raw", "torch", "torchvision", "--index-url", _torch_index()], "install torch", log_file, frac=(0.1, 0.55))
        await ctx.progress(0.55, "Installing Heretic and its libraries")
        await _run(ctx, [str(py), "-m", "pip", "install", "--no-input", "--progress-bar", "off", "-e", str(src)], "install heretic", log_file, frac=(0.55, 0.98))
        await asyncio.to_thread(status)
        return {"installed": True}

    return job


def run_job(model: str, preset: str = "default", trials: int = 50, bnb_4bit: bool = False, strategy: str = "merge", name: str | None = None):
    """Run Heretic on `model` (a Hugging Face id or a local folder) with every prompt answered by flags."""
    src = source_dir()
    if not src:
        raise HereticError("Set the Heretic folder first")
    if not MODEL_RE.match(model.strip()):
        raise HereticError("Use a Hugging Face model id like owner/name, or a full folder path")
    cfg = next((c for c in configs(src) if c["id"] == preset), None)
    if not cfg:
        raise HereticError(f"Unknown preset '{preset}'")
    if not 1 <= trials <= 500:
        raise HereticError("Trials must be between 1 and 500")
    if strategy not in ("merge", "adapter"):
        raise HereticError("Strategy must be merge or adapter")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or model.split("/")[-1].split("\\")[-1]).strip("-") or "model"
    run_dir = root() / "runs" / f"{stamp}-{slug}"
    out_dir = run_dir / "model"
    log_file = run_dir / "run.log"

    async def job(ctx) -> dict:
        py = venv_python()
        if not py.exists():
            raise HereticError("Heretic isn't installed yet. Use Set up first.")
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        args = [str(py), "-X", "utf8", "-u", "-c", LAUNCHER, "--model", model.strip(), "--n-trials", str(trials),
                "--checkpoint-action", "restart", "--trial-index", "0", "--model-action", "save", "--save-directory", str(out_dir),
                "--export-strategy", strategy, "--max-batch-size", "32", "--quantization", "bnb_4bit" if bnb_4bit else "none"]
        env = {**os.environ, "HERETIC_CONFIG_FILE": str(src / cfg["file"]), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200", "HF_HUB_DISABLE_TELEMETRY": "1"}
        (run_dir / "meta.json").write_text(json.dumps({"model": model, "preset": preset, "trials": trials, "bnb_4bit": bnb_4bit,
                                                        "strategy": strategy, "started": stamp}), encoding="utf-8")
        await ctx.progress(0.01, "Freeing graphics memory")
        try:  # a loaded Ollama model can hold most of the GPU; Heretic needs it
            await ollama.unload_all()
        except Exception:  # noqa: BLE001 - Ollama may not be running
            pass
        await ctx.progress(0.02, "Loading the model")
        await _run(ctx, args, f"heretic {model}", log_file, cwd=run_dir, env=env, frac=(0.05, 0.95))
        if not any(out_dir.glob("*.safetensors")) and not any(out_dir.glob("adapter_model*")):
            raise HereticError("Heretic finished but saved no model files. Last lines:\n" + "\n".join(tail(log_file, 6)))
        return {"folder": str(out_dir), "run": run_dir.name, "adapter": strategy == "adapter"}

    return job, run_dir.name


def runs() -> list[dict]:
    out = []
    for d in sorted((root() / "runs").glob("*"), reverse=True) if (root() / "runs").exists() else []:
        meta: dict = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        folder = d / "model"
        out.append({"run": d.name, **meta, "saved": any(folder.glob("*.safetensors")), "adapter": any(folder.glob("adapter_model*")),
                    "size_gb": round(sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1e9, 2) if folder.exists() else 0})
    return out


def log_tail(run: str, n: int = 120) -> list[str]:
    if not re.fullmatch(r"[\w.-]+", run):
        raise HereticError("Unknown run")
    target = root() / ("setup.log" if run == "setup" else f"runs/{run}/run.log")
    return tail(target, n)


def import_job(run: str, name: str, quantize: str | None = "Q4_K_M"):
    """Bring a saved Heretic model into Ollama (and quantize it on the way in)."""
    if not re.fullmatch(r"[\w.-]+", run):
        raise HereticError("Unknown run")
    if not re.fullmatch(r"[\w./:-]+", name):
        raise HereticError("Use letters, numbers and - . / : in the model name")
    q = quant.BY_ID.get(quantize or "")
    if quantize and not q:
        raise HereticError("Unknown quantization")
    folder = root() / "runs" / run / "model"
    if not any(folder.glob("*.safetensors")):
        raise HereticError("That run has no merged model to import (adapters can't be imported directly)")

    async def job(ctx) -> dict:
        exe = ollama.find_exe()
        if not exe:
            raise HereticError("Ollama is not installed")
        await ollama.start()
        log_file = folder.parent / "import.log"
        log_file.write_text("", encoding="utf-8")
        qargs = ["--quantize", q["tag"]] if q and q["id"] != "F16" else []
        modelfile = folder.parent / "Modelfile"
        modelfile.write_text(f'FROM "{folder.as_posix()}"\n', encoding="utf-8")
        await ctx.progress(0.05, f"Importing into Ollama as {name}")
        try:
            await _run(ctx, [exe, "create", name, "-f", str(modelfile), *qargs], "ollama create", log_file, cwd=folder.parent)
        except HereticError as exc:
            if "unsupported architecture" not in str(exc):
                raise
            # Ollama can't read this architecture's safetensors: convert to GGUF with llama.cpp's converter, then import that.
            gguf = await _to_gguf(ctx, folder, log_file)
            modelfile.write_text(f'FROM "{gguf.as_posix()}"\n', encoding="utf-8")
            await ctx.progress(0.85, f"Importing the GGUF into Ollama as {name}")
            await _run(ctx, [exe, "create", name, "-f", str(modelfile), *qargs], "ollama create (gguf)", log_file, cwd=folder.parent, frac=(0.85, 0.99))
        quant._remember(name, {"source": "heretic", "ref": run, "quant": q["id"] if q else "F16"})
        return {"model": name, "quant": q["id"] if q else "F16"}

    return job


# ---------- GGUF conversion (for architectures Ollama can't import from safetensors) ----------
LLAMACPP_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
CONVERTER_FILES = ("convert_hf_to_gguf.py", "conversion/", "gguf-py/")


def converter_dir() -> Path:
    return root() / "llamacpp"


def _safe_members(tar, prefix: str):
    """Only the converter's own files, and never a path that escapes the target folder."""
    for m in tar.getmembers():
        if not m.name.startswith(prefix):
            continue
        rel = m.name[len(prefix):]
        if not rel or not rel.startswith(CONVERTER_FILES) or ".." in Path(rel).parts or m.issym() or m.islnk():
            continue
        m.name = rel
        yield m


async def ensure_converter(ctx, log_file: Path) -> Path:
    """Download llama.cpp's converter (the latest release, from GitHub) once, and install its gguf package into Heretic's environment."""
    import io
    import tarfile

    import httpx

    folder = converter_dir()
    script = folder / "convert_hf_to_gguf.py"
    py = venv_python()
    if script.exists():
        return script
    await ctx.progress(0.1, "Downloading the GGUF converter (llama.cpp)")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers={"User-Agent": "KeiroLake"}) as client:
        tag = (await client.get(LLAMACPP_RELEASES)).json()["tag_name"]
        if not re.fullmatch(r"[\w.-]{1,40}", tag):
            raise HereticError("Unexpected llama.cpp release name")
        data = (await client.get(f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/tags/{tag}")).content
    folder.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        top = tar.getmembers()[0].name.split("/")[0] + "/"
        tar.extractall(folder, members=list(_safe_members(tar, top)))
    if not script.exists():
        raise HereticError("The llama.cpp download didn't contain the converter")
    await ctx.progress(0.2, "Installing the converter's package")
    await _run(ctx, [str(py), "-m", "pip", "install", "--no-input", "--progress-bar", "off", str(folder / "gguf-py")], "install gguf", log_file, frac=(0.2, 0.3))
    (folder / "VERSION").write_text(tag, encoding="utf-8")
    return script


async def _to_gguf(ctx, folder: Path, log_file: Path) -> Path:
    script = await ensure_converter(ctx, log_file)
    out = folder.parent / "model-f16.gguf"
    if not out.exists():
        await ctx.progress(0.35, "Converting the model to GGUF")
        await _run(ctx, [str(venv_python()), "-X", "utf8", str(script), str(folder), "--outtype", "f16", "--outfile", str(out)], "convert to gguf",
                   log_file, cwd=converter_dir(), frac=(0.35, 0.8))
    if not out.exists():
        raise HereticError("The conversion finished without writing a GGUF file")
    return out
