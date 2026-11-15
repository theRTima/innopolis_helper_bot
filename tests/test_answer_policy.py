from app.llm.policy import thinking_options, completion_options
import pytest


def test_thinking_default_is_provider_and_model_scoped():
    assert thinking_options(
        "https://opencode.ai/zen/go/v1", "qwen3.8-flash"
    ) == {"enable_thinking": True, "thinking_budget": 512}
    assert thinking_options("https://example.com/v1", "qwen3.8-flash") == {}
    assert thinking_options("https://opencode.ai/zen/go/v1", "other") == {}
    assert (
        thinking_options(
            "https://opencode.ai.attacker.test/v1", "qwen3.8-flash"
        )
        == {}
    )


def test_explicit_override():
    assert thinking_options(
        "https://opencode.ai/zen/go/v1", "qwen3.8-flash", True
    ) == {"enable_thinking": True, "thinking_budget": 512}
    assert thinking_options(
        "https://opencode.ai/zen/go/v1", "qwen3.8-flash", False
    ) == {"enable_thinking": False}


def test_combined_limit_and_budget_validation():
    result = completion_options(
        "https://opencode.ai/zen/go/v1", "qwen3.8-flash", 900
    )
    assert result["max_completion_tokens"] == 1400
    assert "max_tokens" not in result
    for budget in (0, -1, True, 4097):
        with pytest.raises(ValueError):
            thinking_options(
                "https://opencode.ai/zen/go/v1", "qwen3.8-flash", True, budget
            )
    with pytest.raises(ValueError):
        completion_options(
            "https://opencode.ai/zen/go/v1",
            "qwen3.8-flash",
            900,
            total_tokens=512,
        )
    assert completion_options("https://example.com/v1", "other", 600) == {
        "extra_body": None,
        "max_tokens": 600,
    }


def test_real_client_payload_and_no_reasoning_leak(monkeypatch):
    import asyncio
    import importlib.util
    import sys
    import types
    from pathlib import Path

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.chat = types.SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            captured["request"] = kwargs
            message = types.SimpleNamespace(
                content=None, reasoning_content="PRIVATE"
            )
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)]
            )

    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    path = Path(__file__).parents[1] / "app/llm/client.py"
    spec = importlib.util.spec_from_file_location("isolated_llm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.LLMClient(
        base_url="https://opencode.ai/zen/go/v1",
        model="qwen3.8-flash",
        api_key="test",
        session_id="test",
        user_agent="test",
        max_tokens=900,
        temperature=0.1,
    )
    answer = asyncio.run(client.answer_question("Вопрос", []))
    assert "PRIVATE" not in answer
    assert captured["init"]["timeout"] == 20
    assert captured["init"]["max_retries"] == 0
    assert captured["request"]["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 512,
    }
    assert captured["request"]["max_completion_tokens"] == 1400
    assert "max_tokens" not in captured["request"]
