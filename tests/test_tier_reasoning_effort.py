"""Per-tier ``reasoning_effort`` for the OpenAI-compatible provider family.

The deep and quick tiers are configured independently so a local run can keep
reasoning where it pays (debate, judging) and skip it where it does not (the
high-volume analyst turns). Three things have to hold for that to work:

1. the tier keys resolve with the documented precedence,
2. providers hosting arbitrary models stop filtering the parameter out by
   model name, and
3. the tier-independent call keeps behaving exactly as it did before.
"""

import importlib

import pytest

from tradingagents.graph.trading_graph import (
    PROVIDER_TIER_EFFORT_DEFAULTS,
    TradingAgentsGraph,
)
from tradingagents.llm_clients.openai_client import OpenAIClient


def _bare_graph(config):
    g = object.__new__(TradingAgentsGraph)
    g.config = config
    return g


def _effort(config, tier):
    return _bare_graph(config)._get_provider_kwargs(tier).get("reasoning_effort")


# --- precedence ------------------------------------------------------------

@pytest.mark.unit
class TestTierPrecedence:
    def test_tier_key_wins_over_shared_key(self):
        config = {
            "llm_provider": "openai",
            "openai_reasoning_effort": "high",
            "quick_reasoning_effort": "low",
        }
        assert _effort(config, "quick") == "low"
        # The deep tier has no key of its own, so it keeps the shared value.
        assert _effort(config, "deep") == "high"

    def test_shared_key_applies_when_tier_unset(self):
        config = {"llm_provider": "openai", "openai_reasoning_effort": "medium"}
        assert _effort(config, "quick") == "medium"
        assert _effort(config, "deep") == "medium"

    def test_provider_default_is_last_resort(self):
        # Nothing configured: Ollama's quick tier falls back to the table.
        config = {"llm_provider": "ollama"}
        assert _effort(config, "quick") == "none"
        assert _effort(config, "deep") is None

    def test_explicit_config_overrides_provider_default(self):
        config = {"llm_provider": "ollama", "quick_reasoning_effort": "low"}
        assert _effort(config, "quick") == "low"

    def test_ollama_quick_default_is_registered(self):
        assert PROVIDER_TIER_EFFORT_DEFAULTS["ollama"]["quick"] == "none"


# --- scoping ---------------------------------------------------------------

@pytest.mark.unit
class TestProviderScoping:
    @pytest.mark.parametrize("provider", ["google", "anthropic"])
    def test_tier_keys_ignored_off_the_openai_family(self, provider):
        # These providers have their own vocabulary; "none" is not in it.
        config = {"llm_provider": provider, "quick_reasoning_effort": "none"}
        assert _effort(config, "quick") is None

    def test_shared_key_stays_scoped_to_native_openai(self):
        # Widening it would start forwarding the parameter to third-party
        # endpoints that previously ignored it.
        config = {"llm_provider": "groq", "openai_reasoning_effort": "high"}
        assert _effort(config, "deep") is None

    def test_google_thinking_level_still_forwarded(self):
        config = {"llm_provider": "google", "google_thinking_level": "high"}
        kwargs = _bare_graph(config)._get_provider_kwargs("deep")
        assert kwargs["thinking_level"] == "high"

    def test_anthropic_effort_still_forwarded(self):
        config = {"llm_provider": "anthropic", "anthropic_effort": "high"}
        kwargs = _bare_graph(config)._get_provider_kwargs("deep")
        assert kwargs["effort"] == "high"


# --- backward compatibility ------------------------------------------------

@pytest.mark.unit
class TestTierIndependentCall:
    """Calling without a tier must behave as it did before the split."""

    def test_shared_openai_key_still_applies(self):
        config = {"llm_provider": "openai", "openai_reasoning_effort": "high"}
        assert _bare_graph(config)._get_provider_kwargs()["reasoning_effort"] == "high"

    def test_no_provider_default_without_a_tier(self):
        # The Ollama default is a per-tier decision; a tier-less call is not
        # asking about a tier and must not silently acquire one's setting.
        kwargs = _bare_graph({"llm_provider": "ollama"})._get_provider_kwargs()
        assert "reasoning_effort" not in kwargs


# --- the client-side gate --------------------------------------------------

@pytest.mark.unit
class TestClientGate:
    def _forwarded(self, provider, model, **extra):
        llm = OpenAIClient(
            model, provider=provider, reasoning_effort="none", **extra
        ).get_llm()
        return getattr(llm, "reasoning_effort", None)

    def test_ollama_forwards_despite_non_openai_model_name(self):
        # Regression: the gpt-5/o-series regex used to drop this silently, so
        # the parameter never reached the endpoint that honors it.
        assert self._forwarded("ollama", "Qwen3:latest") == "none"

    def test_openai_compatible_forwards(self):
        assert self._forwarded(
            "openai_compatible", "local-model", base_url="http://localhost:8000/v1"
        ) == "none"

    def test_native_openai_still_gated_by_model(self):
        # gpt-4.1 is not a reasoning model and would 400 on the parameter.
        assert self._forwarded("openai", "gpt-4.1", api_key="placeholder") is None
        assert self._forwarded("openai", "gpt-5.5", api_key="placeholder") == "none"


# --- env overlay -----------------------------------------------------------

@pytest.mark.unit
class TestEnvOverlay:
    @pytest.mark.parametrize(
        "env_var,key",
        [
            ("TRADINGAGENTS_DEEP_REASONING_EFFORT", "deep_reasoning_effort"),
            ("TRADINGAGENTS_QUICK_REASONING_EFFORT", "quick_reasoning_effort"),
        ],
    )
    def test_env_sets_tier_effort(self, monkeypatch, env_var, key):
        import tradingagents.default_config as dc

        monkeypatch.setenv(env_var, "low")
        importlib.reload(dc)
        assert dc.DEFAULT_CONFIG[key] == "low"
        monkeypatch.delenv(env_var, raising=False)
        importlib.reload(dc)
        assert dc.DEFAULT_CONFIG[key] is None
