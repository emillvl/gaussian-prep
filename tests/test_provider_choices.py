import json

import httpx
import pytest

from gaussian_prep import cli
from gaussian_prep.models import Plan
from gaussian_prep.provider import PROVIDERS, ModelProvider, ProviderError, concurrency_limit, provider_base_url, provider_configuration
from gaussian_prep.pipeline import Pipeline, PipelineError
from gaussian_prep.verification import compute
from conftest import ScriptedProvider, slot


def test_opencode_go_is_default_and_has_its_own_endpoint(tmp_path):
    provider = ModelProvider("glm-5.2", "opencode-test-key", tmp_path)
    url, headers, body, protocol = provider.request_parts("planner", {}, Plan, [])
    assert url == "https://opencode.ai/zen/go/v1/chat/completions"
    assert protocol == "chat/completions"
    assert headers["Authorization"] == "Bearer opencode-test-key"
    assert headers["User-Agent"] == "gaussian-prep/0.1.0"
    assert headers["x-opencode-session"]
    assert body["model"] == "glm-5.2"
    repeated = ModelProvider("glm-5.2", "opencode-test-key", tmp_path)
    assert repeated.session_id == provider.session_id


def test_provider_keys_cannot_cross_between_services(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "go-only-key")
    monkeypatch.setenv("OPENCODE_MODEL", "glm-5.2")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-only-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    assert provider_configuration("opencode") == ("glm-5.2", "go-only-key")
    assert provider_configuration("deepseek") == ("deepseek-flash", "direct-only-key")
    provider = ModelProvider("deepseek-flash", "direct-only-key", tmp_path, provider="deepseek")
    url, headers, _, _ = provider.request_parts("planner", {}, Plan, [])
    assert url == "https://api.deepseek.com/chat/completions"
    assert "x-opencode-session" not in headers


@pytest.mark.parametrize("model,protocol", [("qwen3.7-plus", "messages"), ("gpt-5.6-luna", "responses"), ("deepseek-v4-pro", "chat/completions")])
def test_opencode_routes_documented_model_protocols(tmp_path, model, protocol):
    provider = ModelProvider(model, "go-key", tmp_path)
    url, _, body, actual = provider.request_parts("planner", {}, Plan, [])
    assert url == f"https://opencode.ai/zen/go/v1/{protocol}"
    assert actual == protocol and body["model"] == model


@pytest.mark.parametrize("model,protocol", [("qwen3.7-plus", "messages"), ("gpt-5.6-luna", "responses"), ("glm-5.2", "chat/completions")])
def test_opencode_outputs_are_validated_for_every_protocol(tmp_path, monkeypatch, model, protocol):
    plan = Plan(title="Test", interpretation="Test", assumptions=[], requirements=[], total_questions=1, module_count=1, minutes_per_module=[2], slots=[slot()])
    text = plan.model_dump_json()
    responses = {
        "messages": {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]},
        "responses": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]},
        "chat/completions": {"choices": [{"finish_reason": "stop", "message": {"content": text}}]},
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json=responses[protocol]))
    provider = ModelProvider(model, "go-key", tmp_path)
    assert provider.ask("planner", {}, Plan).total_questions == 1


def test_opencode_namespace_is_removed_from_model_id(tmp_path):
    assert ModelProvider("opencode-go/glm-5.2", "key", tmp_path).model == "glm-5.2"


def test_resuming_does_not_silently_switch_provider(corpus, tmp_path):
    first = ScriptedProvider()
    first.name = "opencode"
    run_dir = tmp_path / "run"
    Pipeline(corpus, first, run_dir, progress=lambda _: None, compute=compute).run("Generate 1 question")
    second = ScriptedProvider()
    second.name = "deepseek"
    with pytest.raises(PipelineError, match="original provider"):
        Pipeline(corpus, second, run_dir, compute=compute).run("Generate 1 question", resume=True)


def test_missing_go_key_is_actionable(tmp_path):
    with pytest.raises(ProviderError, match="OPENCODE_API_KEY"):
        ModelProvider("glm-5.2", "", tmp_path)


@pytest.mark.parametrize("provider,model,expected", [
    ("deepseek", "deepseek-v4-flash", 2500),
    ("deepseek", "deepseek-v4.1-flash", 2500),
    ("deepseek", "deepseek-v4-pro", 500),
    ("deepseek", "deepseek-chat", 2500),
    ("deepseek", "some-unknown-model", 500),
    ("opencode", "glm-5.2", 4),
    ("opencode", "gpt-5.6-luna", 8),
    ("opencode", "claude-sonnet-5", 16),
    ("opencode", "gemini-3.5-flash", 8),
    ("opencode", "grok-4.6", 30),
    ("opencode", "qwen3.7-plus", 16),
    ("opencode", "mistral-large", 8),
    ("opencode", "totally-unknown", 4),
])
def test_concurrency_limit_follows_provider(provider, model, expected):
    assert concurrency_limit(provider, model) == expected


def test_menu_keeps_opencode_then_deepseek_first():
    slugs = list(PROVIDERS)
    assert slugs[:2] == ["opencode", "deepseek"]
    assert {"opencode", "deepseek", "openai", "anthropic", "google", "ollama", "custom"} <= set(slugs)


@pytest.mark.parametrize("provider,suffix", [
    ("openai", "https://api.openai.com/v1/chat/completions"),
    ("groq", "https://api.groq.com/openai/v1/chat/completions"),
    ("google", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("mistral", "https://api.mistral.ai/v1/chat/completions"),
])
def test_openai_compatible_providers_use_their_endpoint(tmp_path, provider, suffix):
    instance = ModelProvider("some-model", "key", tmp_path, provider=provider)
    url, headers, body, protocol = instance.request_parts("planner", {}, Plan, [])
    assert url == suffix and protocol == "chat/completions"
    assert headers["Authorization"] == "Bearer key"
    assert body["response_format"] == {"type": "json_object"}


def test_anthropic_direct_uses_messages_protocol(tmp_path):
    instance = ModelProvider("claude-sonnet-5", "key", tmp_path, provider="anthropic")
    url, headers, body, protocol = instance.request_parts("planner", {}, Plan, [])
    assert url == "https://api.anthropic.com/v1/messages" and protocol == "messages"
    assert headers["x-api-key"] == "key" and headers["anthropic-version"] == "2023-06-01"
    assert body["system"] and body["messages"][0]["role"] == "user"


def test_keyless_local_provider_needs_no_key(tmp_path):
    instance = ModelProvider("llama3.1", "", tmp_path, provider="ollama")
    assert instance.request_parts("planner", {}, Plan, [])[0] == "http://localhost:11434/v1/chat/completions"


def test_hosted_provider_still_requires_a_key(tmp_path):
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        ModelProvider("gpt-5", "", tmp_path, provider="openai")


def test_custom_provider_requires_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("CUSTOM_BASE_URL", raising=False)
    with pytest.raises(ProviderError, match="CUSTOM_BASE_URL"):
        provider_base_url("custom")
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://example.test/v1/")
    assert provider_base_url("custom") == "https://example.test/v1"


def test_choose_provider_accepts_number_or_name(monkeypatch):
    slugs = list(PROVIDERS)
    monkeypatch.setattr("builtins.input", lambda _: str(slugs.index("openai") + 1))
    assert cli.choose_provider() == "openai"
    monkeypatch.setattr("builtins.input", lambda _: "anthropic")
    assert cli.choose_provider() == "anthropic"
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert cli.choose_provider() == "opencode"
    monkeypatch.setattr("builtins.input", lambda _: "999")
    with pytest.raises(ProviderError):
        cli.choose_provider()
