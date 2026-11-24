# KeiroLake overview

KeiroLake is a local data lake and RAG platform. Documents are stored untouched in the raw zone,
normalized into the clean zone, split into chunks, embedded, and indexed in Qdrant.

## Hybrid search

Search combines dense vectors from Qdrant with keyword matching from SQLite FTS5. The two ranked
lists are merged with Reciprocal Rank Fusion, which rewards results that both methods agree on.

## Embedding fallback

If the primary embedding model is unavailable, ingestion falls back to the next model in EMBED_MODELS.
Each model has its own named vector, so incompatible vector spaces are never mixed.
