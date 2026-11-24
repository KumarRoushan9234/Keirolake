"""Test environment: isolated data dir, fake keys, tiny embedding dims.

Must set env vars before any `app` import because settings are cached.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="keirolake-test-")
os.environ.update(
    DATA_DIR=_tmp,
    SQLITE_PATH=os.path.join(_tmp, "test.db"),
    LLM_FALLBACK_ORDER="openrouter,groqcloud",
    OPENROUTER_API_KEY="test-or",
    OPENROUTER_MODELS="model-a,model-b",
    GROQCLOUD_API_KEY="test-groq",
    GROQCLOUD_MODELS="groq-a",
    DEEPSEEK_API_KEY="",
    KEIROLABS_SEARCH_LITE_API_KEY="keiro_test",
    KEIROLABS_API_KEY="",
    EMBED_MODELS="baai/bge-m3:8,qwen/qwen3-embedding-8b:8",
    EMBED_GEMINI_ENABLED="false",
    QDRANT_URL=os.environ.get("TEST_QDRANT_URL", "http://localhost:6333"),
    QDRANT_API_KEY="",
    KEIROLAKE_KEYRING="0",
    KEIROLAKE_NO_SAMPLES="1",
    KEIROLAKE_NO_LOCAL_FALLBACK="1",
    LLM_MODE="auto",
    LOCAL_LLM_MODEL="",
    OLLAMA_BASE_URL="http://127.0.0.1:9",
    KEIROLAKE_TOKEN="",
)

import pytest  # noqa: E402

from app.db import init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()
