"""Heretic runs as a separate program with every prompt answered by flags; nothing is imported or bundled."""
import pytest

from app.local import heretic


@pytest.fixture
def fake_heretic(tmp_path, monkeypatch):
    src = tmp_path / "heretic"
    (src / "src" / "heretic").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='heretic-llm'\n")
    for n in ("default", "noslop", "terse", "classifier", "piqa"):
        (src / f"config.{n}.toml").write_text("")
    monkeypatch.setenv("HERETIC_DIR", str(src))
    monkeypatch.setattr(heretic, "root", lambda: tmp_path / "data")
    return src


def test_presets_skip_evaluation_only_configs(fake_heretic):
    assert [c["id"] for c in heretic.configs()] == ["default", "noslop", "terse"]


def test_status_without_environment(fake_heretic):
    st = heretic.status()
    assert st["found"] and not st["installed"] and st["license"].startswith("AGPL")


def test_status_when_folder_is_wrong(monkeypatch, tmp_path):
    monkeypatch.setenv("HERETIC_DIR", str(tmp_path))
    assert heretic.status()["found"] is False


@pytest.mark.parametrize("model", ["bad name; rm -rf /", "x", "owner/na me"])
def test_run_rejects_unsafe_models(fake_heretic, model):
    with pytest.raises(heretic.HereticError):
        heretic.run_job(model)


def test_run_validates_options(fake_heretic):
    with pytest.raises(heretic.HereticError, match="preset"):
        heretic.run_job("owner/name", preset="evil")
    with pytest.raises(heretic.HereticError, match="Trials"):
        heretic.run_job("owner/name", trials=0)
    job, run = heretic.run_job("owner/name", "terse", 10)
    assert callable(job) and run.endswith("-name")


@pytest.mark.asyncio
async def test_run_builds_a_fully_non_interactive_command(fake_heretic, tmp_path, monkeypatch):
    seen = {}

    async def fake_run(ctx, args, label, log_file, cwd=None, env=None, frac=(0, 1)):
        seen.update(args=args, env=env, cwd=cwd)
        (cwd / "model").mkdir(parents=True, exist_ok=True)
        (cwd / "model" / "model.safetensors").write_bytes(b"x")

    class Ctx:
        async def progress(self, *a):
            pass

    py = tmp_path / "data" / "venv" / ("Scripts/python.exe" if __import__("sys").platform == "win32" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(heretic, "_run", fake_run)
    job, _ = heretic.run_job("owner/name", "terse", 12, True, "merge")
    result = await job(Ctx())
    a = seen["args"]
    for flag, value in [("--model", "owner/name"), ("--n-trials", "12"), ("--model-action", "save"), ("--trial-index", "0"),
                        ("--checkpoint-action", "restart"), ("--export-strategy", "merge"), ("--quantization", "bnb_4bit")]:
        assert a[a.index(flag) + 1] == value
    assert seen["env"]["HERETIC_CONFIG_FILE"].endswith("config.terse.toml") and result["folder"].endswith("model")
    assert heretic.runs()[0]["saved"] is True


def test_log_tail_refuses_path_tricks(fake_heretic):
    with pytest.raises(heretic.HereticError):
        heretic.log_tail("../../etc")


def test_import_needs_a_merged_model(fake_heretic):
    with pytest.raises(heretic.HereticError, match="merged"):
        heretic.import_job("nope", "my-model", "Q4_K_M")


def test_converter_extraction_keeps_only_its_files(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("llama.cpp-1/convert_hf_to_gguf.py", "llama.cpp-1/conversion/base.py", "llama.cpp-1/gguf-py/pyproject.toml",
                     "llama.cpp-1/ggml/src/big.c", "llama.cpp-1/conversion/../../evil.py"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = [m.name for m in heretic._safe_members(tar, "llama.cpp-1/")]
    assert names == ["convert_hf_to_gguf.py", "conversion/base.py", "gguf-py/pyproject.toml"]


@pytest.mark.asyncio
async def test_import_falls_back_to_gguf(fake_heretic, tmp_path, monkeypatch):
    model = tmp_path / "data" / "runs" / "r1" / "model"
    model.mkdir(parents=True)
    (model / "model.safetensors").write_bytes(b"x")
    calls = []

    async def fake_run(ctx, args, label, log_file, cwd=None, env=None, frac=(0, 1)):
        calls.append(label)
        if label == "ollama create":
            raise heretic.HereticError('ollama create failed (exit 1). Error: unsupported architecture "Qwen3ForCausalLM"')

    async def fake_gguf(ctx, folder, log_file):
        out = folder.parent / "model-f16.gguf"
        out.write_bytes(b"gguf")
        return out

    async def started(*a, **k):
        return {}

    class Ctx:
        async def progress(self, *a):
            pass

    monkeypatch.setattr(heretic, "_run", fake_run)
    monkeypatch.setattr(heretic, "_to_gguf", fake_gguf)
    monkeypatch.setattr(heretic.ollama, "find_exe", lambda: "ollama")
    monkeypatch.setattr(heretic.ollama, "start", started)
    monkeypatch.setattr(heretic.quant, "_remember", lambda *a: None)
    out = await heretic.import_job("r1", "m-heretic:q4", "Q4_K_M")(Ctx())
    assert calls == ["ollama create", "ollama create (gguf)"] and out["quant"] == "Q4_K_M"
    assert "model-f16.gguf" in (model.parent / "Modelfile").read_text()
