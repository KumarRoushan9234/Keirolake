from app.config import parse_embed_models
from app.ingest.chunking import chunk_text


def test_chunks_respect_size_and_cover_text():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(20))
    chunks = chunk_text(text, size=500, overlap=80)
    assert len(chunks) > 5
    assert all(len(c) <= 500 + 1 for c in chunks)
    assert "Paragraph 0" in chunks[0] and "Paragraph 19" in chunks[-1]


def test_empty_and_tiny_text():
    assert chunk_text("   \n\n ") == []
    assert chunk_text("hello", 100, 10) == ["hello"]


def test_overlap_repeats_context():
    text = " ".join(f"w{i}" for i in range(400))
    chunks = chunk_text(text, size=300, overlap=60)
    assert chunks[1].split()[0] in chunks[0]


def test_parse_embed_models_handles_colons_and_providers():
    specs = parse_embed_models("baai/bge-m3:1024,nvidia/nemotron-3.5-embed-1b:nitro:1024,gemini:gemini-embedding-001:768")
    assert [(s.provider, s.model, s.dim) for s in specs] == [
        ("openrouter", "baai/bge-m3", 1024),
        ("openrouter", "nvidia/nemotron-3.5-embed-1b:nitro", 1024),
        ("gemini", "gemini-embedding-001", 768),
    ]
    assert specs[0].vector_name == "openrouter_baai_bge_m3"


def test_free_tier_models_are_ignored(monkeypatch):
    from app.config import parse_embed_models, provider_config
    monkeypatch.setenv("OPENROUTER_MODELS", "a/cheap,b/model:free,c/other")
    assert provider_config("openrouter").models == ["a/cheap", "c/other"]
    assert [s.model for s in parse_embed_models("x/emb:1024,y/emb:free:1024")] == ["x/emb"]
