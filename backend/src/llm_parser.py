"""
LLM Parser — parses natural-language procurement requests into structured
Offer objects and generates human-readable explanations of policy decisions.

The LLM is NEVER consulted for financial decisions.  It only does:
  1. Text → Structured data (parsing)
  2. Structured data → Text (explanation)

Both entry points are total: any provider failure (missing key, rate limit,
malformed output, outage) degrades to a deterministic fallback rather than
killing the negotiation.

Provider chain
--------------
``LLM_PROVIDERS`` (default ``gemini,groq``) is an ordered fallback chain.
Each call tries providers in order and returns the first success; a provider
with no API key configured is skipped silently.  If every provider fails the
caller falls back to deterministic text — the negotiation never blocks on the
LLM, because the LLM never decides anything financial.

  • **gemini** — ``gemini-3.5-flash-lite``.  Primary: ~0.9–1.5 s, clean JSON.
    Deliberately *not* ``gemini-3.5-flash``, which spends its output budget on
    thinking tokens and returns truncated explanations.
  • **groq** — ``openai/gpt-oss-120b``.  Fallback: ~0.6 s, strict JSON, and it
    respects the "don't leak concession percentages" instruction that the 20b
    variant breaks.  Free tier: 1 000 requests/day, 8 000 tokens/minute.
    ``reasoning_effort="low"`` keeps each call inside the per-minute budget.

Both model choices were verified against the live APIs for these two specific
tasks, not assumed from the model names.
"""

from __future__ import annotations

import asyncio
import json
import os
import logging
from typing import Any

import groq
import httpx

from src.schemas import DecisionType, Offer, PolicyDecision

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Rate-limit plumbing (shared by all providers)
# ---------------------------------------------------------------------------

# Never spend more than this waiting on rate limits before falling back.  A
# free-tier *daily* quota returns a retry-after measured in hours; blocking on
# that would stall the negotiation for no benefit, when the next provider — or
# a deterministic fallback — is available immediately.
_MAX_TOTAL_BACKOFF_S = 20.0


class GeminiAPIError(Exception):
    """A non-2xx response from the Gemini REST API."""

    def __init__(self, message: str, response: httpx.Response):
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code


def is_rate_limit_error(exc: BaseException) -> bool:
    """True only for an explicit 429 rejection."""
    if isinstance(exc, groq.RateLimitError):
        return True
    return getattr(exc, "status_code", None) == 429


def _retry_after_seconds(exc: BaseException, fallback: float) -> float:
    """Prefer the server's own Retry-After over blind exponential backoff."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}) or {}
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = header.get(name)
        if not raw:
            continue
        try:
            return max(0.0, float(str(raw).rstrip("s")))
        except ValueError:
            continue
    return fallback


async def _call_with_retry(
    make_call,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> Any:
    """
    Await ``make_call()`` with backoff on rate-limit (429) errors.

    ``make_call`` is a zero-argument coroutine factory (a fresh awaitable per
    attempt).  Every provider is natively async, so nothing here blocks the
    event loop.

    Backoff honours the server's ``Retry-After`` and is abandoned once the
    cumulative wait would exceed ``_MAX_TOTAL_BACKOFF_S`` — the chain then
    moves to the next provider instead of hanging the negotiation.
    """
    spent = 0.0
    for attempt in range(max_retries + 1):
        try:
            return await make_call()
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt >= max_retries:
                raise
            delay = _retry_after_seconds(exc, base_delay * (2 ** attempt))
            if spent + delay > _MAX_TOTAL_BACKOFF_S:
                logger.warning(
                    "Rate limited and the retry window (%.1f s) exceeds the "
                    "%.0f s budget — moving on instead of waiting.",
                    delay, _MAX_TOTAL_BACKOFF_S,
                )
                raise
            spent += delay
            logger.warning(
                "Rate limited (attempt %d/%d). Retrying in %.1f s …",
                attempt + 1, max_retries, delay,
            )
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

_groq_client: groq.AsyncGroq | None = None


def reset_client() -> None:
    """Drop cached provider clients (used by tests that swap API keys)."""
    global _groq_client
    _groq_client = None


def _get_client() -> groq.AsyncGroq:
    """Lazy-initialise the Groq client."""
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY not set. Get a free key at https://console.groq.com/keys"
            )
        _groq_client = groq.AsyncGroq(api_key=api_key)
    return _groq_client


def _get_model_name() -> str:
    """Return the Groq model name to use (configurable via env var)."""
    return os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def _get_gemini_model_name() -> str:
    """Return the Gemini model name to use (configurable via env var)."""
    return os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


class GroqProvider:
    """Fallback provider — Groq's OpenAI-compatible chat completions."""

    name = "groq"

    # Groq's gpt-oss models are reasoning models.  "low" keeps latency ~0.6 s
    # and the token spend inside the free tier; these tasks need no deliberation.
    reasoning_effort = "low"

    @staticmethod
    def is_configured() -> bool:
        return bool(os.environ.get("GROQ_API_KEY", ""))

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        client = _get_client()
        kwargs: dict[str, Any] = {
            "model": _get_model_name(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = await client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()


class GeminiProvider:
    """
    Primary provider — Gemini's REST API over httpx.

    Called over plain HTTP rather than the ``google-genai`` SDK: that SDK's
    client is synchronous, which would put a blocking call back on the event
    loop, and httpx is already a dependency.
    """

    name = "gemini"

    @staticmethod
    def is_configured() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY", ""))

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Get a free key at https://aistudio.google.com/"
            )

        model = _get_gemini_model_name()
        config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            config["responseMimeType"] = "application/json"

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": config,
        }

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json=payload,
            )

        if response.status_code != 200:
            raise GeminiAPIError(
                f"Gemini returned {response.status_code}: {response.text[:200]}",
                response,
            )

        # A safety block or a truncated candidate yields no text; treat it as
        # an empty answer so the chain moves on rather than raising a KeyError.
        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            return ""
        return "".join(p.get("text", "") for p in parts).strip()


_PROVIDERS: dict[str, Any] = {
    GroqProvider.name: GroqProvider(),
    GeminiProvider.name: GeminiProvider(),
}

_DEFAULT_PROVIDER_ORDER = "gemini,groq"


def _provider_chain() -> list[Any]:
    """Ordered provider chain from ``LLM_PROVIDERS`` (unknown names ignored)."""
    raw = os.environ.get("LLM_PROVIDERS", _DEFAULT_PROVIDER_ORDER)
    chain = []
    for name in (n.strip().lower() for n in raw.split(",")):
        if not name:
            continue
        provider = _PROVIDERS.get(name)
        if provider is None:
            logger.warning("Unknown LLM provider %r in LLM_PROVIDERS — ignoring.", name)
            continue
        chain.append(provider)
    return chain


async def _chat(
    system_prompt: str,
    user_content: str,
    *,
    temperature: float,
    max_completion_tokens: int,
    json_mode: bool = False,
) -> str:
    """
    One chat completion, trying each configured provider in order.

    Raises only when every configured provider failed; the callers turn that
    into a deterministic fallback.
    """
    failures: list[str] = []

    for provider in _provider_chain():
        if not provider.is_configured():
            continue

        async def _call(p=provider):
            return await p.chat(
                system_prompt,
                user_content,
                temperature=temperature,
                max_tokens=max_completion_tokens,
                json_mode=json_mode,
            )

        try:
            text = await _call_with_retry(_call)
        except Exception as exc:
            logger.warning("LLM provider %s failed: %s", provider.name, exc)
            failures.append(f"{provider.name}: {exc}")
            continue

        if text:
            return text
        logger.warning("LLM provider %s returned an empty response.", provider.name)
        failures.append(f"{provider.name}: empty response")

    raise RuntimeError(
        "no LLM provider available: " + ("; ".join(failures) or "none configured")
    )


# ---------------------------------------------------------------------------
# Parse procurement request → Offer
# ---------------------------------------------------------------------------

_PARSE_SYSTEM_PROMPT = """\
You are a procurement data extractor.  Given a natural-language purchase
request, extract the following fields and return ONLY valid JSON (no
markdown fences, no extra text):

{
  "price": <float, the unit price the buyer is offering or requesting>,
  "quantity": <int, how many units>,
  "terms": "<string, delivery / payment terms if mentioned, else empty>"
}

Rules:
- If the buyer doesn't mention a specific price, set "price" to 0.
- If quantity is not mentioned, default to 1.
- Return raw JSON only.
"""


def _fallback_offer(round_number: int, terms: str) -> Offer:
    """Deterministic stand-in when no provider is available or output is unusable."""
    return Offer(price=0.0, quantity=1, terms=terms, round_number=round_number)


async def parse_procurement_request(text: str, round_number: int = 1) -> Offer:
    """
    Extract a structured Offer from free-text.

    Parameters
    ----------
    text : str
        The natural-language procurement request.
    round_number : int
        Current negotiation round (attached to the resulting Offer).

    Returns
    -------
    Offer
        Never raises — a missing API key or a total provider outage degrades to
        a zero-price ``parse_unavailable`` offer.  The parsed price is advisory
        only (the opening bid always comes from the mandate), so losing it
        costs flavour text, not the negotiation.
    """
    try:
        raw = await _chat(
            _PARSE_SYSTEM_PROMPT, text,
            temperature=0.0, max_completion_tokens=512, json_mode=True,
        )
    except Exception as exc:
        logger.warning("LLM parse failed: %s. Using fallback.", exc)
        return _fallback_offer(round_number, "parse_unavailable")

    if not raw:
        logger.warning("LLM returned an empty parse response. Using fallback.")
        return _fallback_offer(round_number, "parse_unavailable")

    # Strip markdown fences if the model wraps them anyway
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        data = json.loads(raw)
        return Offer(
            price=max(float(data.get("price") or 0.0), 0.0),
            quantity=max(int(data.get("quantity") or 1), 1),
            terms=str(data.get("terms") or ""),
            round_number=round_number,
        )
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        logger.error("Failed to parse LLM output as JSON: %s", raw)
        # Fallback: zero-price offer — the buyer agent uses the target price
        return _fallback_offer(round_number, "parse_error")


# ---------------------------------------------------------------------------
# Explain a policy decision in natural language
# ---------------------------------------------------------------------------

_EXPLAIN_SYSTEM_PROMPT = """\
You are a professional procurement negotiation assistant.  Given a JSON
object describing a negotiation decision, write a clear 1-2 sentence
explanation in plain English that could be shown to a business user.

Be concise and professional. Do NOT use markdown. Do NOT reveal
internal system details like concession percentages.
ALWAYS format money in Indian Rupees using the '₹' symbol (e.g. ₹5,000).
NEVER use dollars ('$').

Report every price EXACTLY as given, to the paisa (₹97.78 stays "₹97.78",
never "₹98"). All prices are PER UNIT. Never multiply a price by the
quantity, and never present a total as if it were the unit price.
"""


async def explain_decision(
    decision: PolicyDecision,
    role: str,  # "buyer" or "seller"
    context: dict | None = None,
) -> str:
    """
    Generate a human-readable explanation of a PolicyDecision.

    Parameters
    ----------
    decision : PolicyDecision
        The decision output from the policy engine.
    role : str
        "buyer" or "seller".
    context : dict | None
        Optional extra context (e.g. round number, other party's last offer).

    Returns
    -------
    str
        A 1-2 sentence explanation.  Never raises — falls back to a
        deterministic sentence if no provider is available.
    """
    payload: dict[str, Any] = {
        "role": role,
        "decision": decision.decision.value,
        "reason": decision.reason,
    }
    if decision.offer:
        payload["counter_price"] = decision.offer.price
        payload["counter_quantity"] = decision.offer.quantity
    if context:
        payload["context"] = context

    try:
        text = await _chat(
            _EXPLAIN_SYSTEM_PROMPT, json.dumps(payload, indent=2),
            temperature=0.3, max_completion_tokens=400,
        )
        if text:
            return text
        raise RuntimeError("empty explanation")
    except Exception as exc:
        logger.warning("LLM explanation failed: %s. Using fallback.", exc)
        # Deterministic fallback — never block the negotiation
        if decision.decision == DecisionType.ACCEPT:
            return f"The {role} has accepted the offer."
        elif decision.decision == DecisionType.REJECT:
            return f"The {role} has walked away from the negotiation. Reason: {decision.reason}."
        else:
            price = decision.offer.price if decision.offer else "N/A"
            return f"The {role} has countered with a price of ₹{price}."
