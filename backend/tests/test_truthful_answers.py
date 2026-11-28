"""Answers that don't make things up: short standalone queries, relevance floors and the fabrication guard.

These replay a real failure: "pull the full report fo these top performing indian stocks" got a report about
"XYZ Limited" and sources about the History of India, Qdrant, Rust and survival shelters."""
import asyncio

from app.engine import evidence
from app.engine.evidence import Passage
from app.llm.base import ChatMessage
from app.rag import guard, rewrite

FAKE_REPORT = """Certainly! Since I don't have direct access to real-time databases, I'll simulate a report.
1. Company Name: XYZ Limited
* Ticker Symbol: XYZ
* 30-Day Performance: Price: Rs. 1,200 Change: +Rs. 200 (18.18%) Volume: 500,000 shares
* Market Cap: Rs. 20,000 crore, P/E Ratio: 20, 52-Week Range: Rs. 800 - Rs. 1,500
2. Company Name: ABC Corporation
* Ticker Symbol: ABC  Price: Rs. 750 Change: +Rs. 150 (25%) Volume: 300,000 shares
"""

HONEST = ("The sources don't include 30-day returns for individual Indian stocks [W1]. The NSE publishes daily "
          "price files (bhavcopy) that do, and Tickertape lists the highest-return stocks.")


def test_the_pasted_fake_report_is_caught():
    v = guard.check(FAKE_REPORT, "Tickertape lists stocks with the highest return in the last month.")
    assert v.tripped
    assert any("placeholder" in r for r in v.reasons) and any("simulated" in r for r in v.reasons)


def test_an_honest_answer_passes():
    assert not guard.check(HONEST, "The NSE publishes daily price files. Tickertape lists the highest-return stocks.").tripped


def test_numbers_in_the_sources_are_fine():
    answer = "Revenue was 94.7 billion and EPS 2.95 in the quarter, a rise of 11%."
    assert not guard.check(answer, "Apple reported revenue of 94.7 billion and EPS of 2.95, up 11%.").tripped


def test_many_unsourced_figures_trip_the_guard():
    answer = "Price 1250. Volume 480000. Market cap 90500. Revenue 3200. Profit 410. Debt 770. Assets 5600."
    v = guard.check(answer, "Some unrelated text without numbers.")
    assert v.tripped and len(v.unsourced_numbers) >= 6


def test_cited_numbers_are_left_to_the_source_check():
    answer = "Price 1250 [W1]. Volume 480000 [W1]. Market cap 90500 [W1]. Revenue 3200 [W1]. Profit 410 [W1]. Debt 770 [W1]."
    assert not guard.check(answer, "x").tripped


def test_follow_up_without_a_model_borrows_the_topic():
    history = [ChatMessage("user", "What is the news about the Indian market and the most profitable companies for the last 30 days?")]
    q = rewrite.rule_queries("no pull the full report fo these top performing indian stocks for the last 30 days ?", history)
    assert len(q) == 1 and len(q[0].split()) <= rewrite.MAX_WORDS
    assert "indian" in q[0].lower() and "30" in q[0] and "report" not in q[0].lower() and "these" not in q[0].lower()


def test_short_questions_are_used_as_they_are():
    assert asyncio.run(rewrite.search_queries("NSE bhavcopy download")) == ["NSE bhavcopy download"]


def test_a_failing_model_falls_back_to_rules(monkeypatch):
    class Broken:
        async def complete(self, *a, **k):
            raise RuntimeError("no model")

    import app.llm.registry as registry

    monkeypatch.setattr(registry, "get_router", lambda: Broken())
    history = [ChatMessage("user", "top gaining Indian stocks this month")]
    out = asyncio.run(rewrite.search_queries("give me a full detailed report on these please", history))
    assert out and "stocks" in out[0].lower()


def test_query_cleaning_limits_length_and_count():
    assert rewrite._clean(["a b c d e f g h i j k l m n o", "second", "third"]) == ["a b c d e f g h i j k l", "second"]
    assert rewrite._clean("not a list") == []


def test_stored_evidence_must_be_about_the_question(tmp_path):
    from app.db import init_db

    init_db()
    store = [
        Passage("History of India after 1947: partition, the constitution and the first five-year plans.", "https://x.test/india", "History of India", "web"),
        Passage("Top gaining NSE stocks over the last 30 days with returns and market cap.", "https://x.test/gainers", "Top gaining NSE stocks", "web"),
        Passage("Qdrant is a vector database written in Rust.", "https://x.test/qdrant", "Qdrant", "web"),
    ]
    evidence.store("test", "q", store)
    found = evidence.recall("top gaining NSE stocks last 30 days")
    titles = {p.title for p in found}
    assert "Top gaining NSE stocks" in titles
    assert "History of India" not in titles and "Qdrant" not in titles


def test_relevance_score():
    assert guard.relevance("top gaining nse stocks", {"nse", "stocks", "gaining"}) == 1.0
    assert guard.relevance("survival shelter basics", {"nse", "stocks"}) == 0.0
