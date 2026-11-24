"""Quantized models: names, estimates, availability checks, safety filter, and the API."""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.local import quant


def test_model_names_follow_each_sources_convention():
    assert quant.model_name("ollama", "qwen2.5:7b-instruct", "Q4_K_M") == "qwen2.5:7b-instruct-q4_K_M"
    assert quant.model_name("ollama", "llama3.1:8b", "Q8_0") == "llama3.1:8b-q8_0"
    assert quant.model_name("ollama", "llama3.1:8b-instruct-q5_K_M", "Q4_K_M") == "llama3.1:8b-instruct-q5_K_M"  # already pinned
    assert quant.model_name("hf", "bartowski/Qwen2.5-7B-Instruct-GGUF", "Q4_K_M") == "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
    for bad in (("ollama", "qwen2.5", "Q4_K_M"), ("hf", "not a repo", "Q4_K_M"), ("ollama", "a:b", "Q9_Z"), ("url", "x:y", "Q4_K_M")):
        with pytest.raises(quant.QuantError):
            quant.model_name(*bad)


@pytest.mark.parametrize("name,params", [("qwen2.5:7b-instruct", 7.0), ("phi3:3.8b", 3.8), ("qwen3:0.6b-q8_0", 0.6), ("llama3.3:70b", 70.0),
                                         ("bartowski/Qwen2.5-14B-Instruct-GGUF", 14.0), ("nomic-embed-text", None)])
def test_parameter_counts_are_read_from_names(name, params):
    assert quant.parse_params_b(name) == params


def test_estimates_scale_with_bits_and_context_and_fit_the_hardware():
    q4, q8, f16 = (quant.estimate(7, q)["weights_gb"] for q in ("Q4_K_M", "Q8_0", "F16"))
    assert q4 < q8 < f16 and 4.0 < q4 < 4.6 and f16 == 14.0
    assert quant.estimate(7, "Q4_K_M", 32768)["cache_gb"] > quant.estimate(7, "Q4_K_M", 4096)["cache_gb"]
    assert quant.estimate(7, "Q4_K_M", 32768, "q4_0")["cache_gb"] < quant.estimate(7, "Q4_K_M", 32768, "f16")["cache_gb"]
    hw = {"vram_gb": 8, "ram_gb": 24}
    assert quant.fit(5.5, hw)["where"] == "gpu" and quant.fit(14, hw)["where"] == "split" and quant.fit(40, hw)["where"] == "no"
    assert quant.fit(5, {"vram_gb": 0, "ram_gb": 16})["where"] == "ram"


@pytest.mark.parametrize("file,expected", [
    ("Qwen2.5-7B-Instruct-Q4_K_M.gguf", "Q4_K_M"), ("model-q8_0.gguf", "Q8_0"), ("llama-3.2-3b-instruct-IQ3_XS.gguf", "IQ3_XS"),
    ("Model-f16.gguf", "F16"), ("model.gguf", None)])
def test_quantization_is_read_from_gguf_file_names(file, expected):
    assert quant.quant_of_filename(file) == expected


@respx.mock
@pytest.mark.asyncio
async def test_hf_listing_keeps_gguf_and_reports_unsafe_files_as_skipped():
    respx.get("https://huggingface.co/api/models/someone/Tiny-GGUF").mock(return_value=httpx.Response(200, json={"siblings": [
        {"rfilename": "Tiny-Q4_K_M.gguf", "size": 2_000_000_000}, {"rfilename": "Tiny-Q8_0.gguf", "size": 3_900_000_000},
        {"rfilename": "pytorch_model.bin", "size": 5}, {"rfilename": "modeling_custom.py", "size": 5}, {"rfilename": "README.md"}]}))
    out = await quant.hf_files("someone/Tiny-GGUF")
    assert [f["quant"] for f in out["gguf"]] == ["Q4_K_M", "Q8_0"] and out["gguf"][0]["size_gb"] == 2.0
    assert set(out["skipped"]) == {"pytorch_model.bin", "modeling_custom.py"} and "never used" in out["note"]
    respx.get("https://huggingface.co/api/models/someone/missing").mock(return_value=httpx.Response(404))
    with pytest.raises(quant.QuantError, match="doesn't exist"):
        await quant.hf_files("someone/missing")


@respx.mock
@pytest.mark.asyncio
async def test_check_reports_availability_size_and_fit():
    hit = respx.get("https://registry.ollama.ai/v2/library/qwen2.5/manifests/7b-instruct-q4_K_M").mock(
        return_value=httpx.Response(200, json={"layers": [{"size": 4_000_000_000}, {"size": 600_000_000}]}))
    respx.get("https://registry.ollama.ai/v2/library/qwen2.5/manifests/7b-instruct-q2_K").mock(return_value=httpx.Response(404))
    hw = {"vram_gb": 8, "ram_gb": 24}
    ok = await quant.check("ollama", "qwen2.5:7b-instruct", "Q4_K_M", hw)
    assert hit.called and ok["available"] and ok["size_gb"] == 4.6 and ok["fit"]["where"] == "gpu"
    missing = await quant.check("ollama", "qwen2.5:7b-instruct", "Q2_K", hw)
    assert missing["available"] is False and missing["size_gb"] is None


def test_runtime_settings_are_validated():
    assert quant.validate_runtime({"OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_CONTEXT_LENGTH": "8192"}) == {"OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_CONTEXT_LENGTH": "8192"}
    for bad in ({"OLLAMA_KV_CACHE_TYPE": "q1"}, {"PATH": "/evil"}, {"OLLAMA_CONTEXT_LENGTH": "99999999"}):
        with pytest.raises(quant.QuantError):
            quant.validate_runtime(bad)


def test_quantize_refuses_f16_target_and_bad_names():
    with pytest.raises(quant.QuantError):
        quant.quantize_job("llama3.2:3b-instruct-fp16", "F16")
    with pytest.raises(quant.QuantError):
        quant.quantize_job("llama3.2:3b-instruct-fp16", "Q4_K_M", "bad name; rm -rf")


def test_models_api(monkeypatch):
    from app.main import app

    async def fake_installed():
        return [{"name": "qwen2.5:7b", "size_gb": 4.7, "quant": "Q4_K_M", "params": "7.6B", "format": "gguf", "family": "qwen2", "loaded": False}]

    monkeypatch.setattr(quant, "installed", fake_installed)
    with TestClient(app) as c:
        o = c.get("/api/models/quant").json()
        assert [q["id"] for q in o["quants"]][:2] == ["Q2_K", "Q3_K_M"] and o["installed"][0]["quant"] == "Q4_K_M"
        assert "OLLAMA_KV_CACHE_TYPE" in o["runtime"]["options"]
        assert c.post("/api/models/quant/check", json={"kind": "ollama", "ref": "nocolon", "quant": "Q4_K_M"}).status_code == 400
        assert c.put("/api/models/runtime", json={"values": {"OLLAMA_KV_CACHE_TYPE": "bogus"}}).status_code == 400
        saved = c.put("/api/models/runtime", json={"values": {"OLLAMA_KV_CACHE_TYPE": "q8_0"}}).json()
        assert saved["values"]["OLLAMA_KV_CACHE_TYPE"] == "q8_0" and saved["restarted"] is False
        c.put("/api/models/runtime", json={"values": {"OLLAMA_KV_CACHE_TYPE": ""}})
        assert c.post("/api/models/quant/pull", json={"kind": "hf", "ref": "bad repo", "quant": "Q4_K_M"}).status_code == 400
