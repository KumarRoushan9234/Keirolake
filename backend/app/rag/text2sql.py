"""Natural language -> SQLite SELECT, validated and executed read-only."""
from __future__ import annotations

import json
import logging
import re

from app.lake.warehouse import SQLValidationError, run_query, schema_prompt, validate_select
from app.llm.base import ChatMessage
from app.llm.registry import get_router

log = logging.getLogger(__name__)
ANSWER_PROMPT = """You answer the user's question from a query result. Use 1-3 short sentences, give the key numbers,
and say plainly if the result is empty or doesn't answer the question. Don't mention SQL."""

PROMPT = """You translate questions into a single SQLite SELECT query.
Rules:
- Use only the tables and columns listed below. Quote identifiers with double quotes when needed.
- Output exactly one query in a ```sql fenced block, then one short sentence explaining it.
- Never write INSERT/UPDATE/DELETE/DDL. Add LIMIT 200 unless the question asks for an aggregate.

Schema:
{schema}"""


def extract_sql(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    return (match.group(1) if match else text).strip()


async def text_to_sql(question: str, execute: bool = True, tables: list[str] | None = None) -> dict:
    """`tables` limits the question to these tables (asking one sheet)."""
    router = get_router()
    messages = [ChatMessage("system", PROMPT.format(schema=schema_prompt(only=tables))), ChatMessage("user", question)]
    result = await router.complete(messages, temperature=0, max_tokens=800)
    sql = extract_sql(result.text)
    explanation = re.sub(r"```.*?```", "", result.text, flags=re.S).strip()
    out = {"sql": sql, "explanation": explanation, "model": f"{result.provider}:{result.model}"}
    if not execute:
        return out
    try:
        validate_select(sql)
        out["result"] = run_query(sql, limit=500)
    except SQLValidationError as first_error:
        # One self-repair round: show the model the error.
        messages += [ChatMessage("assistant", result.text),
                     ChatMessage("user", f"That query failed with: {first_error}. Return a corrected query.")]
        retry = await router.complete(messages, temperature=0, max_tokens=800)
        out["sql"] = extract_sql(retry.text)
        try:
            out["result"] = run_query(out["sql"], limit=500)
        except SQLValidationError as exc:
            out["error"] = str(exc)
    if out.get("result"):
        out["answer"] = await explain(question, out["result"])
    return out


async def explain(question: str, result: dict) -> str:
    """A plain-language answer drawn from the first rows of the result."""
    preview = {"columns": result["columns"], "rows": result["rows"][:25], "total_rows": result["row_count"]}
    try:
        reply = await get_router().complete([ChatMessage("system", ANSWER_PROMPT),
                                             ChatMessage("user", f"Question: {question}\nResult: {json.dumps(preview, default=str)[:4000]}")],
                                            temperature=0.1, max_tokens=250)
        return reply.text.strip()
    except Exception as exc:  # noqa: BLE001 - the table is still shown
        log.info("Result explanation skipped: %s", exc)
        return ""
