# Providers

In the desktop app you set all of this on the **Models** page and keys are kept in your system keychain. The variables
below are for the server version (`.env`) and for anyone who prefers files.

Every provider is configured by three environment variables: `<NAME>_API_KEY`, `<NAME>_MODELS` (comma separated, tried in order) and optionally `<NAME>_BASE_URL`. A provider takes part only if it appears in `LLM_FALLBACK_ORDER` and has a key and at least one model.

| Name | Notes |
|---|---|
| `openrouter` | One key, hundreds of models. Also serves embeddings. |
| `groqcloud` | Fast open models. Check <https://console.groq.com/docs/models>: model ids change and retire. |
| `deepseek` | Use DeepSeek's own ids (e.g. `deepseek-chat`), not OpenRouter slugs. |
| `gemini` | Google's OpenAI-compatible endpoint. |
| `anthropic` | Claude, via the official SDK. |
| `openai`, `mistral`, `together` | OpenAI-compatible. |
| `ollama` | Local. Set `OLLAMA_MODELS`; no key needed. |

Use **Models → Test connections** to see which models actually answer. Models that error are skipped at runtime, so a stale id degrades gracefully instead of breaking chat.

## Embedding models

`EMBED_MODELS` is `[provider:]model:dimension`, comma separated. The first working model is used for ingestion; all models that hold data are used for search.

| Model | Dim | Context | Notes |
|---|---|---|---|
| `baai/bge-m3` | 1024 | 8k | Multilingual, best default |
| `qwen/qwen3-embedding-8b` | up to 4096 (set 1024) | 32k+ | Very strong, Apache-2.0, supports truncation |
| `qwen/qwen3-embedding-4b` | up to 2560 (set 1024) | 32k | Cheaper sibling |
| `intfloat/multilingual-e5-large` | 1024 | 512 | 90+ languages |
| `baai/bge-large-en-v1.5` | 1024 | 512 | English only |
| `thenlper/gte-large` | 1024 | 512 | English |

Dimensions are verified on every call: a mismatch with `EMBED_MODELS` is an error rather than silent corruption.

### Gemini embeddings

```bash
EMBED_GEMINI_ENABLED=true
EMBED_GEMINI_MODEL=gemini-embedding-001
EMBED_GEMINI_DIM=768
EMBED_GEMINI_VIA=openrouter     # or "native" with GEMINI_API_KEY
EMBED_GEMINI_POSITION=first     # or "last" (default: used only as a fallback)
```

Gemini is not open source; it is opt-in. After changing the list for an existing knowledge base, press **Re-embed** on that collection (Data → Collections).

### Fully local embeddings

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull nomic-embed-text
EMBED_MODELS="ollama:nomic-embed-text:768"
```
