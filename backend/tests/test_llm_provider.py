"""
Tests for the LLM provider layer (Groq primary, Gemini fallback).

The LLM makes no financial decisions, so the contract these tests defend is
narrow but important: **both entry points are total**.  Whatever the provider
does — missing key, 429, garbage output, outage — a negotiation must still
complete deterministically rather than raise, and it must not stall waiting
on a rate limit it cannot outlast.
"""

from __future__ import annotations

import time

import groq
import httpx
import pytest

from src import llm_parser
from src.llm_parser import (
    _call_with_retry,
    _retry_after_seconds,
    explain_decision,
    is_rate_limit_error,
    parse_procurement_request,
)
from src.schemas import DecisionType, Offer, PolicyDecision


def _rate_limit_error(retry_after: str | None = None) -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        429, headers=headers, request=httpx.Request("POST", "https://api.groq.com/")
    )
    return groq.RateLimitError("rate limited", response=response, body=None)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """
    Every test starts with no cached client and a known provider chain.

    Crucially this also blanks *both* provider keys by default: with a
    fallback chain in place, a test that only clears Groq would silently fall
    through and hit the live Gemini API.
    """
    llm_parser.reset_client()
    monkeypatch.setenv("LLM_PROVIDERS", "groq,gemini")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    yield
    llm_parser.reset_client()


@pytest.fixture
def no_api_key(monkeypatch):
    """No provider is configured at all."""
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    llm_parser.reset_client()


# ===================================================================
# Rate-limit detection
# ===================================================================

class TestRateLimitDetection:
    def test_groq_rate_limit_error_detected(self):
        assert is_rate_limit_error(_rate_limit_error()) is True

    def test_duck_typed_429_detected(self):
        class Boom(Exception):
            status_code = 429

        assert is_rate_limit_error(Boom()) is True

    def test_ordinary_error_not_treated_as_rate_limit(self):
        """
        The old Gemini check matched the substring "rate", which is present in
        "generate_content" — so every ordinary failure burned 28 s of backoff.
        """
        assert is_rate_limit_error(RuntimeError("generate_content failed")) is False
        assert is_rate_limit_error(ValueError("bad request")) is False

    def test_non_429_status_not_treated_as_rate_limit(self):
        response = httpx.Response(
            500, request=httpx.Request("POST", "https://api.groq.com/")
        )
        exc = groq.InternalServerError("boom", response=response, body=None)
        assert is_rate_limit_error(exc) is False


# ===================================================================
# Retry-After handling
# ===================================================================

class TestRetryAfter:
    def test_server_retry_after_is_preferred_over_backoff(self):
        assert _retry_after_seconds(_rate_limit_error("3"), fallback=99.0) == 3.0

    def test_seconds_suffix_is_tolerated(self):
        assert _retry_after_seconds(_rate_limit_error("2.5s"), fallback=99.0) == 2.5

    def test_falls_back_when_header_absent(self):
        assert _retry_after_seconds(_rate_limit_error(), fallback=4.0) == 4.0

    def test_falls_back_when_header_unparseable(self):
        assert _retry_after_seconds(_rate_limit_error("soon"), fallback=4.0) == 4.0


# ===================================================================
# Retry loop
# ===================================================================

class TestCallWithRetry:
    async def test_returns_first_success(self):
        calls = []

        async def call():
            calls.append(1)
            return "ok"

        assert await _call_with_retry(call) == "ok"
        assert len(calls) == 1

    async def test_retries_a_short_rate_limit_then_succeeds(self):
        attempts = []

        async def call():
            attempts.append(1)
            if len(attempts) == 1:
                raise _rate_limit_error("0")
            return "ok"

        assert await _call_with_retry(call) == "ok"
        assert len(attempts) == 2

    async def test_gives_up_immediately_when_retry_window_exceeds_budget(self):
        """
        A free-tier *daily* quota returns a Retry-After measured in hours.
        Waiting on that would stall the negotiation for no benefit when a
        deterministic fallback is available right now.
        """
        attempts = []

        async def call():
            attempts.append(1)
            raise _rate_limit_error("7200")  # 2 hours

        started = time.monotonic()
        with pytest.raises(groq.RateLimitError):
            await _call_with_retry(call)
        elapsed = time.monotonic() - started

        assert len(attempts) == 1        # no pointless re-attempts
        assert elapsed < 1.0             # and no long sleep

    async def test_non_rate_limit_error_is_not_retried(self):
        attempts = []

        async def call():
            attempts.append(1)
            raise RuntimeError("bad request")

        with pytest.raises(RuntimeError):
            await _call_with_retry(call)
        assert len(attempts) == 1


# ===================================================================
# parse_procurement_request — never raises
# ===================================================================

class TestParseProcurementRequest:
    async def test_missing_api_key_falls_back(self, no_api_key):
        offer = await parse_procurement_request("100 units of bolts", round_number=2)
        assert isinstance(offer, Offer)
        assert offer.price == 0.0
        assert offer.terms == "parse_unavailable"
        assert offer.round_number == 2

    async def test_provider_error_falls_back(self, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(llm_parser, "_chat", boom)
        offer = await parse_procurement_request("100 units of bolts")
        assert offer.terms == "parse_unavailable"

    async def test_malformed_json_falls_back(self, monkeypatch):
        async def garbage(*a, **kw):
            return "not json at all"

        monkeypatch.setattr(llm_parser, "_chat", garbage)
        offer = await parse_procurement_request("100 units of bolts")
        assert offer.terms == "parse_error"
        assert offer.price == 0.0

    async def test_empty_response_falls_back(self, monkeypatch):
        async def empty(*a, **kw):
            return ""

        monkeypatch.setattr(llm_parser, "_chat", empty)
        offer = await parse_procurement_request("100 units of bolts")
        assert offer.terms == "parse_unavailable"

    async def test_well_formed_json_is_parsed(self, monkeypatch):
        async def good(*a, **kw):
            return '{"price": 42.5, "quantity": 100, "terms": "net 30"}'

        monkeypatch.setattr(llm_parser, "_chat", good)
        offer = await parse_procurement_request("...", round_number=1)
        assert offer.price == 42.5
        assert offer.quantity == 100
        assert offer.terms == "net 30"

    async def test_markdown_fenced_json_is_unwrapped(self, monkeypatch):
        async def fenced(*a, **kw):
            return '```json\n{"price": 10, "quantity": 5, "terms": ""}\n```'

        monkeypatch.setattr(llm_parser, "_chat", fenced)
        offer = await parse_procurement_request("...")
        assert offer.price == 10.0
        assert offer.quantity == 5

    async def test_negative_price_is_floored_at_zero(self, monkeypatch):
        async def negative(*a, **kw):
            return '{"price": -5, "quantity": 0, "terms": ""}'

        monkeypatch.setattr(llm_parser, "_chat", negative)
        offer = await parse_procurement_request("...")
        assert offer.price == 0.0
        assert offer.quantity == 1  # quantity must stay positive


# ===================================================================
# explain_decision — never raises
# ===================================================================

class TestExplainDecision:
    @pytest.fixture
    def counter(self) -> PolicyDecision:
        return PolicyDecision(
            decision=DecisionType.COUNTER,
            offer=Offer(price=93.33, quantity=100, round_number=1),
            reason="conceding_33pct_of_gap",
        )

    async def test_missing_api_key_falls_back_to_rupees(self, no_api_key, counter):
        text = await explain_decision(counter, role="buyer", context={"round": 1})
        assert "₹93.33" in text
        assert "$" not in text

    async def test_accept_fallback(self, no_api_key):
        decision = PolicyDecision(decision=DecisionType.ACCEPT, reason="whatever")
        text = await explain_decision(decision, role="seller")
        assert text == "The seller has accepted the offer."

    async def test_reject_fallback_names_the_reason(self, no_api_key):
        decision = PolicyDecision(
            decision=DecisionType.REJECT, reason="max_rounds_exceeded"
        )
        text = await explain_decision(decision, role="buyer")
        assert "max_rounds_exceeded" in text

    async def test_empty_provider_response_falls_back(self, monkeypatch, counter):
        async def empty(*a, **kw):
            return ""

        monkeypatch.setattr(llm_parser, "_chat", empty)
        text = await explain_decision(counter, role="buyer")
        assert "₹93.33" in text

    async def test_provider_text_is_returned_when_available(self, monkeypatch, counter):
        async def good(*a, **kw):
            return "The buyer countered at ₹93.33 per unit."

        monkeypatch.setattr(llm_parser, "_chat", good)
        assert await explain_decision(counter, role="buyer") == (
            "The buyer countered at ₹93.33 per unit."
        )


# ===================================================================
# Model / configuration
# ===================================================================

class TestModelConfig:
    def test_default_model_is_the_verified_free_tier_choice(self, monkeypatch):
        monkeypatch.delenv("GROQ_MODEL", raising=False)
        assert llm_parser._get_model_name() == "openai/gpt-oss-120b"

    def test_model_is_overridable(self, monkeypatch):
        monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
        assert llm_parser._get_model_name() == "openai/gpt-oss-20b"

    def test_missing_key_raises_a_actionable_error(self, no_api_key):
        with pytest.raises(RuntimeError, match="console.groq.com"):
            llm_parser._get_client()


# ===================================================================
# Provider fallback chain
# ===================================================================

def _gemini_error(status: int, retry_after: str | None = None) -> llm_parser.GeminiAPIError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        status, headers=headers,
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/"),
    )
    return llm_parser.GeminiAPIError(f"Gemini returned {status}", response)


class TestProviderChain:
    """`_chat` tries providers in order and returns the first success."""

    @pytest.fixture
    def both_keys(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
        monkeypatch.setenv("GEMINI_API_KEY", "AQ.fake")
        llm_parser.reset_client()

    async def _chat_once(self):
        return await llm_parser._chat(
            "sys", "user", temperature=0.0, max_completion_tokens=64,
        )

    async def test_primary_success_never_touches_the_fallback(self, both_keys, monkeypatch):
        called = []

        async def groq_ok(self, *a, **kw):
            called.append("groq"); return "from groq"

        async def gemini_ok(self, *a, **kw):
            called.append("gemini"); return "from gemini"

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", groq_ok)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", gemini_ok)

        assert await self._chat_once() == "from groq"
        assert called == ["groq"]

    async def test_falls_through_to_gemini_when_groq_fails(self, both_keys, monkeypatch):
        called = []

        async def groq_down(self, *a, **kw):
            called.append("groq"); raise RuntimeError("groq is down")

        async def gemini_ok(self, *a, **kw):
            called.append("gemini"); return "from gemini"

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", groq_down)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", gemini_ok)

        assert await self._chat_once() == "from gemini"
        assert called == ["groq", "gemini"]

    async def test_daily_quota_on_groq_falls_through_without_waiting(self, both_keys, monkeypatch):
        """A multi-hour Retry-After must move to the next provider, not sleep."""
        async def groq_quota(self, *a, **kw):
            raise _rate_limit_error("7200")

        async def gemini_ok(self, *a, **kw):
            return "from gemini"

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", groq_quota)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", gemini_ok)

        started = time.monotonic()
        assert await self._chat_once() == "from gemini"
        assert time.monotonic() - started < 1.0

    async def test_empty_primary_response_falls_through(self, both_keys, monkeypatch):
        async def groq_empty(self, *a, **kw):
            return ""

        async def gemini_ok(self, *a, **kw):
            return "from gemini"

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", groq_empty)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", gemini_ok)

        assert await self._chat_once() == "from gemini"

    async def test_unconfigured_provider_is_skipped_silently(self, monkeypatch):
        """No Groq key → straight to Gemini, with no error surfaced."""
        monkeypatch.setenv("GROQ_API_KEY", "")
        monkeypatch.setenv("GEMINI_API_KEY", "AQ.fake")
        called = []

        async def groq_chat(self, *a, **kw):
            called.append("groq"); return "from groq"

        async def gemini_ok(self, *a, **kw):
            called.append("gemini"); return "from gemini"

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", groq_chat)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", gemini_ok)

        assert await self._chat_once() == "from gemini"
        assert called == ["gemini"]

    async def test_all_providers_failing_raises(self, both_keys, monkeypatch):
        async def down(self, *a, **kw):
            raise RuntimeError("down")

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", down)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", down)

        with pytest.raises(RuntimeError, match="no LLM provider available"):
            await self._chat_once()

    async def test_no_provider_configured_raises(self, no_api_key):
        with pytest.raises(RuntimeError, match="none configured"):
            await self._chat_once()

    async def test_a_total_outage_still_yields_a_deterministic_explanation(
        self, both_keys, monkeypatch
    ):
        """The whole point of the chain: the negotiation never blocks on it."""
        async def down(self, *a, **kw):
            raise RuntimeError("down")

        monkeypatch.setattr(llm_parser.GroqProvider, "chat", down)
        monkeypatch.setattr(llm_parser.GeminiProvider, "chat", down)

        decision = PolicyDecision(
            decision=DecisionType.COUNTER,
            offer=Offer(price=93.33, quantity=100, round_number=1),
            reason="conceding_33pct_of_gap",
        )
        assert "₹93.33" in await explain_decision(decision, role="buyer")


class TestProviderChainConfig:
    def test_default_order_is_gemini_then_groq(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDERS", raising=False)
        assert [p.name for p in llm_parser._provider_chain()] == ["gemini", "groq"]

    def test_order_is_reversible(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDERS", "groq,gemini")
        assert [p.name for p in llm_parser._provider_chain()] == ["groq", "gemini"]

    def test_single_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDERS", "groq")
        assert [p.name for p in llm_parser._provider_chain()] == ["groq"]

    def test_unknown_names_are_ignored(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDERS", "groq, nope , gemini")
        assert [p.name for p in llm_parser._provider_chain()] == ["groq", "gemini"]

    def test_default_gemini_model_is_the_verified_choice(self, monkeypatch):
        """Not gemini-3.5-flash: it spends its budget on thinking and truncates."""
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        assert llm_parser._get_gemini_model_name() == "gemini-3.5-flash-lite"

    def test_gemini_model_is_overridable(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")
        assert llm_parser._get_gemini_model_name() == "gemini-3.6-flash"


class TestGeminiProviderErrors:
    def test_429_is_classified_as_a_rate_limit(self):
        assert is_rate_limit_error(_gemini_error(429)) is True

    def test_403_is_not_a_rate_limit(self):
        """A blocked project must fail over immediately, not retry."""
        assert is_rate_limit_error(_gemini_error(403)) is False

    def test_retry_after_is_read_from_the_gemini_response(self):
        assert _retry_after_seconds(_gemini_error(429, "5"), fallback=99.0) == 5.0
