import json

import httpx

from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.state import Goal


async def test_anthropic_requests_use_anthropic_host_and_key(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1/")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "claude",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"intent": "test", "constraints": []}),
                        },
                    }
                ],
            },
        )

    provider = create_llm_provider(provider="anthropic")
    assert provider._client.api_key == "test-anthropic-key"
    await provider._client.close()
    from openai import AsyncOpenAI

    provider._client = AsyncOpenAI(
        api_key="test-anthropic-key",
        base_url=provider._client.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        await provider.understand_instruction(Goal(description="test"))
    finally:
        await provider.aclose()
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.anthropic.com/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-anthropic-key"
