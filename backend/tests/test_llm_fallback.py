import json

import httpx
import pytest
import respx

from app.config import provider_config
from app.llm.base import ChatMessage
from app.llm.openai_compat import OpenAICompatBackend
from app.llm.registry import AllModelsFailed, LLMRouter

OR = "https://openrouter.ai/api/v1/chat/completions"
GROQ = "https://api.groq.com/openai/v1/chat/completions"
MSG = [ChatMessage("user", "hi")]


def ok(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@respx.mock
async def test_falls_back_across_models_and_providers():
    def handler(request):
        model = json.loads(request.content)["model"]
        if model.startswith("model-"):
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return ok("from groq")

    respx.post(OR).mock(side_effect=handler)
    respx.post(GROQ).mock(side_effect=handler)
    result = await LLMRouter().complete(MSG)
    assert (result.provider, result.model, result.text) == ("groqcloud", "groq-a", "from groq")
    assert len(result.errors) == 2  # model-a and model-b both failed first


@respx.mock
async def test_first_model_wins_when_healthy():
    respx.post(OR).mock(return_value=ok("hello"))
    result = await LLMRouter().complete(MSG)
    assert (result.provider, result.model) == ("openrouter", "model-a")
    assert result.errors == []


@respx.mock
async def test_all_fail_raises_with_every_error():
    respx.post(OR).mock(return_value=httpx.Response(500, text="boom"))
    respx.post(GROQ).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(AllModelsFailed) as exc:
        await LLMRouter().complete(MSG)
    assert len(exc.value.errors) == 3


@respx.mock
async def test_failed_model_goes_on_cooldown():
    route = respx.post(OR).mock(side_effect=[httpx.Response(500, text="x"), ok("b works"), ok("b again")])
    router = LLMRouter()
    first = await router.complete(MSG)
    second = await router.complete(MSG)
    assert first.model == second.model == "model-b"  # model-a skipped on the 2nd call
    assert route.call_count == 3


@respx.mock
async def test_stream_yields_meta_then_tokens():
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(OR).mock(return_value=httpx.Response(200, text=sse))
    items = [i async for i in LLMRouter().stream(MSG)]
    assert items[0] == ("meta", "openrouter:model-a")
    assert "".join(items[1:]) == "Hello"


@respx.mock
async def test_reasoning_models_get_low_effort_so_the_answer_is_not_empty():
    route = respx.post(GROQ).mock(return_value=ok("fine"))
    backend = OpenAICompatBackend(provider_config("groqcloud"))
    await backend.complete("openai/gpt-oss-120b", MSG)
    assert json.loads(route.calls[0].request.content)["reasoning_effort"] == "low"


@respx.mock
async def test_openrouter_requests_disable_thinking():
    route = respx.post(OR).mock(return_value=ok("fine"))
    await OpenAICompatBackend(provider_config("openrouter")).complete("qwen/qwen3.7-flash", MSG)
    assert json.loads(route.calls[0].request.content)["reasoning"] == {"enabled": False}
