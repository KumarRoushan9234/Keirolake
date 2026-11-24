"""Local model catalog and the hardware advisor.

Sizes are the default Ollama downloads (checked on ollama.com, Nov 2026).
`need_gb` is the memory the model needs to run fully on that device (weights +
a modest context). With a GPU the budget is VRAM; without one it is ~60% of RAM.
"""
from __future__ import annotations

LLMS: list[dict] = [
    {"id": "qwen3.5:27b", "label": "Qwen 3.5 27B", "size_gb": 17.0, "need_gb": 20.0, "license": "Apache-2.0",
     "note": "Best quality that runs locally on a 24 GB GPU"},
    {"id": "gpt-oss:20b", "label": "gpt-oss 20B", "size_gb": 14.0, "need_gb": 16.0, "license": "Apache-2.0",
     "note": "ChatGPT-like assistant, 16 GB GPU"},
    {"id": "qwen3.5:9b", "label": "Qwen 3.5 9B", "size_gb": 6.6, "need_gb": 7.5, "license": "Apache-2.0",
     "note": "Best overall for 8 GB GPUs"},
    {"id": "qwen3.5:4b", "label": "Qwen 3.5 4B", "size_gb": 3.3, "need_gb": 4.5, "license": "Apache-2.0",
     "note": "Fast; good on 6 GB GPUs or 8 GB RAM without a GPU"},
    {"id": "qwen3.5:2b", "label": "Qwen 3.5 2B", "size_gb": 2.7, "need_gb": 3.5, "license": "Apache-2.0",
     "note": "Low-memory machines"},
    {"id": "qwen3.5:0.8b", "label": "Qwen 3.5 0.8B", "size_gb": 1.2, "need_gb": 2.0, "license": "Apache-2.0",
     "note": "Runs anywhere, basic answers"},
]

EMBEDDERS: list[dict] = [
    {"id": "qwen3-embedding:0.6b", "label": "Qwen3 Embedding 0.6B", "size_gb": 0.64, "dim": 1024, "mrl": True,
     "note": "Default for offline libraries (truncated to 256 dims)"},
    {"id": "bge-m3", "label": "BGE-M3", "size_gb": 1.2, "dim": 1024, "mrl": False,
     "note": "Same model your OpenRouter knowledge bases use"},
    {"id": "nomic-embed-text", "label": "Nomic Embed Text v1.5", "size_gb": 0.27, "dim": 768, "mrl": True,
     "note": "Lightest, good on CPU"},
]

# Wikipedia flavours on the Kiwix mirror (Nov 2026)
WIKIPEDIA = [
    {"name": "wikipedia_en_all_maxi", "label": "Wikipedia (full, with images)", "size_gb": 119},
    {"name": "wikipedia_en_all_nopic", "label": "Wikipedia (full text, no images)", "size_gb": 49},
    {"name": "wikipedia_en_all_mini", "label": "Wikipedia (intros only)", "size_gb": 13},
]

# Practical and reference libraries recommended alongside Wikipedia.
ESSENTIALS = [
    "zimgit-post-disaster_en", "zimgit-water_en", "zimgit-medicine_en", "zimgit-food-preparation_en", "zimgit-knots_en",
    "wikipedia_en_medicine_maxi", "mdwiki_en_all_maxi", "ifixit_en_all", "appropedia_en_all_maxi",
    "wikibooks_en_all_maxi", "wiktionary_en_all_nopic", "wikivoyage_en_all_maxi", "diy.stackexchange.com_en_all",
]
ESSENTIALS_GB = 25.0  # the list above (24.3 GB on the mirror, Nov 2026)


def recommend(hw: dict) -> dict:
    gpu = hw.get("vram_gb", 0) >= 3.5
    budget = hw["vram_gb"] if gpu else hw["ram_gb"] * 0.6
    llm = next((m for m in LLMS if m["need_gb"] <= budget), LLMS[-1])
    faster = next((m for m in LLMS if m["need_gb"] <= budget * 0.6), None)
    embedder = EMBEDDERS[0] if gpu or hw["ram_gb"] >= 12 else EMBEDDERS[2]

    free = hw.get("disk_free_gb", 0)
    reserve = 15 + llm["size_gb"]  # models, indexes, headroom
    wiki = next((w for w in WIKIPEDIA if w["size_gb"] + ESSENTIALS_GB + reserve <= free), None)
    if wiki is None:
        wiki = next((w for w in reversed(WIKIPEDIA) if w["size_gb"] + reserve <= free), None)

    if gpu:
        tier, tier_note = 1, "Index every article intro (a few hours on this GPU)"
    elif hw["ram_gb"] >= 16 and hw.get("threads", 0) >= 8:
        tier, tier_note = 1, "Index article intros overnight or longer on CPU; library search works meanwhile"
    else:
        tier, tier_note = 0, "Use the search index built into each library; no extra indexing"

    reasons = [f"{'GPU with ' + str(hw['vram_gb']) + ' GB VRAM' if gpu else 'No usable GPU; ' + str(hw['ram_gb']) + ' GB RAM'}"
               f" gives about {budget:.1f} GB for a model"]
    if wiki is None:
        reasons.append(f"Only {free:.0f} GB free: put the library on another drive")
    return {
        "llm": llm, "llm_fast": faster if faster and faster["id"] != llm["id"] else None, "embedder": embedder,
        "wikipedia": wiki, "essentials_gb": ESSENTIALS_GB, "index_tier": tier, "index_note": tier_note,
        "gpu": gpu, "budget_gb": round(budget, 1), "reasons": reasons,
    }
