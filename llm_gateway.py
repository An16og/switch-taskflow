"""Multi-provider, resilient LLM Gateway integrated with Switch Trust guardrails.

Features:
1. Exponential backoff and jitter retries on transient errors (429 rate limits, 5xx server errors, timeouts).
2. Guaranteed Claude structured JSON output via Anthropic tool use with schema prefill fallback.
3. Startup model-ID probe preventing silent classification as 'not a task'.
4. Multi-provider cascade: Gemini (Primary) -> OpenAI (Fallback) -> Claude (Fallback).
"""

import json
import logging
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    LLM_MAX_RETRIES,
    LLM_INITIAL_BACKOFF,
    SWITCH_TRUST_API_KEY,
    SWITCH_TRUST_GATEWAY_URL,
    SWITCH_TRUST_POLICY_ID,
    SWITCH_TRUST_REQUIRE_GUARDRAILS,
    SWITCH_TRUST_ALLOW_INSECURE_GATEWAY,
)

logger = logging.getLogger("LLMGateway")
T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")


def is_transient_error(e: Exception) -> bool:
    """Determines whether an exception is transient (rate limits, 5xx server errors, network drops)."""
    # Check status code if present on exception or response
    status_code = getattr(e, "status_code", None) or getattr(e, "code", None)
    if status_code is None and hasattr(e, "response") and hasattr(e.response, "status_code"):
        status_code = e.response.status_code

    if status_code in (429, 500, 502, 503, 504, 529):
        return True

    err_str = str(e).lower()
    transient_indicators = (
        "429", "rate limit", "quota", "resource exhausted", "too many requests",
        "500", "502", "503", "504", "529", "overloaded",
        "timeout", "timed out", "connection reset", "connection refused",
        "connection closed", "service unavailable", "internal server error",
        "bad gateway", "temporarily unavailable", "try again"
    )
    return any(ind in err_str for ind in transient_indicators)


def execute_with_retry(
    func: Callable[[], R],
    provider_name: str,
    max_retries: int = LLM_MAX_RETRIES,
    initial_backoff: float = LLM_INITIAL_BACKOFF,
    backoff_factor: float = 2.0,
    max_backoff: float = 8.0,
) -> R:
    """Executes a provider call with exponential backoff and jitter on transient errors."""
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_err = e
            # If not transient or retries exhausted, fail fast
            if attempt == max_retries or not is_transient_error(e):
                logger.debug(
                    "[%s] Exception not retried (attempt=%d/%d, is_transient=%s): %s",
                    provider_name, attempt + 1, max_retries, is_transient_error(e), e
                )
                raise e

            # Parse Retry-After header if present
            retry_after: Optional[float] = None
            response = getattr(e, "response", None)
            if response and hasattr(response, "headers"):
                ra = response.headers.get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass

            if retry_after is not None:
                delay = min(retry_after, max_backoff)
            else:
                delay = min(initial_backoff * (backoff_factor ** attempt), max_backoff)
                # Apply random jitter to avoid thundering herds
                delay += random.uniform(0.1, 0.4)

            logger.warning(
                "⏳ [LLMGateway] Transient %s error (attempt %d/%d): %s. Retrying in %.2fs...",
                provider_name, attempt + 1, max_retries, e, delay
            )
            time.sleep(delay)

    assert last_err is not None
    raise last_err


def extract_json_dict(text: str) -> dict:
    """Robustly extracts a JSON dictionary from an LLM response, stripping markdown fences or preambles."""
    cleaned = text.strip()
    # Strip markdown code blocks ```json ... ``` or ``` ... ```
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        cleaned = cleaned[start_idx : end_idx + 1]
    elif end_idx != -1 and start_idx == -1:
        # Opening brace was in assistant prefill
        cleaned = "{" + cleaned[: end_idx + 1]

    return json.loads(cleaned)


class LLMGateway:
    """Multi-provider, resilient LLM Gateway integrated with Switch Trust guardrails.
    
    Supports:
    - Retries with exponential backoff on 429 and transient 5xx errors.
    - Claude structured JSON output via tool use and JSON prefill fallbacks.
    - Startup model probes to prevent silent misclassification of tasks.
    - Cascades: Gemini (Primary) -> OpenAI (Fallback) -> Claude (Fallback).
    """

    def __init__(
        self,
        gemini_model: Optional[str] = None,
        openai_model: Optional[str] = None,
        anthropic_model: Optional[str] = None,
        max_retries: int = LLM_MAX_RETRIES,
        initial_backoff: float = LLM_INITIAL_BACKOFF,
    ):
        self.gemini_model = gemini_model or GEMINI_MODEL
        self.openai_model = openai_model or OPENAI_MODEL
        self.anthropic_model = anthropic_model or ANTHROPIC_MODEL
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff

        self._gemini_client = None
        self._openai_client = None
        self._anthropic_client = None

    def _wrap_trust(self, client):
        """Wraps an LLM client with Switch Trust guardrails routing."""
        try:
            import os
            import switch_trust

            if SWITCH_TRUST_API_KEY:
                kwargs = {
                    "api_key": SWITCH_TRUST_API_KEY,
                    "gateway_url": SWITCH_TRUST_GATEWAY_URL or "https://api.switchagents.ai",
                    "policy_id": SWITCH_TRUST_POLICY_ID or "default",
                    "forward_llm_key": True,
                    "allow_insecure_gateway": SWITCH_TRUST_ALLOW_INSECURE_GATEWAY,
                    "require_guardrails": SWITCH_TRUST_REQUIRE_GUARDRAILS,
                }
            else:
                if "SWITCH_TRUST_GATEWAY_URL" in os.environ and not os.environ.get("SWITCH_TRUST_API_KEY"):
                    del os.environ["SWITCH_TRUST_GATEWAY_URL"]
                kwargs = {
                    "require_guardrails": False,
                }

            wrapped = switch_trust.wrap(client, **kwargs)
            logger.info("🛡️ Client wrapped with Switch Trust guardrails (%s)", type(client).__name__)
            return wrapped
        except Exception as e:
            logger.warning("Could not wrap client with switch_trust: %s", e)
            return client

    def _get_gemini(self):
        if self._gemini_client is None and GEMINI_API_KEY:
            from google import genai
            raw_client = genai.Client(api_key=GEMINI_API_KEY)
            self._gemini_client = self._wrap_trust(raw_client)
        return self._gemini_client

    def _get_openai(self):
        if self._openai_client is None and OPENAI_API_KEY:
            from openai import OpenAI
            raw_client = OpenAI(api_key=OPENAI_API_KEY)
            self._openai_client = self._wrap_trust(raw_client)
        return self._openai_client

    def _get_anthropic(self):
        if self._anthropic_client is None and ANTHROPIC_API_KEY:
            from anthropic import Anthropic
            raw_client = Anthropic(api_key=ANTHROPIC_API_KEY)
            self._anthropic_client = self._wrap_trust(raw_client)
        return self._anthropic_client

    def _generate_claude_structured(
        self,
        anthropic_client: Any,
        prompt: str,
        system_prompt: str,
        response_schema: Type[T]
    ) -> T:
        """Generates guaranteed structured JSON conforming to response_schema using Anthropic Claude.
        
        Uses Anthropic tool calling as the primary mechanism, with schema prefill fallback.
        """
        tool_name = f"return_{response_schema.__name__.lower()}"
        tool_schema = response_schema.model_json_schema()
        tools = [
            {
                "name": tool_name,
                "description": f"Outputs the structured {response_schema.__name__} data.",
                "input_schema": tool_schema,
            }
        ]

        # 1. Primary approach: Tool use with tool_choice forcing structured input
        def _call_claude_tool():
            return anthropic_client.messages.create(
                model=self.anthropic_model,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                tools=tools,
                tool_choice={"type": "tool", "name": tool_name},
                temperature=0.1,
            )

        try:
            msg = execute_with_retry(
                _call_claude_tool,
                provider_name="Anthropic Claude (Tool Calling)",
                max_retries=self.max_retries,
                initial_backoff=self.initial_backoff,
            )
            for block in getattr(msg, "content", []):
                if getattr(block, "type", "") == "tool_use" and block.name == tool_name:
                    return response_schema.model_validate(block.input)
                elif isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == tool_name:
                    return response_schema.model_validate(block.get("input", {}))
        except Exception as tool_err:
            logger.warning("Claude tool use structured output failed: %s. Attempting JSON prefill fallback...", tool_err)

        # 2. Resilient fallback: Raw JSON prefilling forcing Claude to output raw JSON
        json_instruction = (
            f"{prompt}\n\n"
            f"Respond ONLY with a valid JSON object strictly matching this JSON schema:\n"
            f"{json.dumps(tool_schema, indent=2)}\n"
            f"Do not include markdown fences, comments, or introductory text."
        )

        def _call_claude_prefill():
            return anthropic_client.messages.create(
                model=self.anthropic_model,
                max_tokens=2048,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": json_instruction},
                    {"role": "assistant", "content": "{"}  # Forces Claude to directly output JSON
                ],
                temperature=0.1,
            )

        msg = execute_with_retry(
            _call_claude_prefill,
            provider_name="Anthropic Claude (JSON Prefill)",
            max_retries=self.max_retries,
            initial_backoff=self.initial_backoff,
        )
        first_block = msg.content[0]
        text_content = getattr(first_block, "text", str(first_block))
        data = extract_json_dict(text_content)
        return response_schema.model_validate(data)

    def generate_structured(self, prompt: str, system_prompt: str, response_schema: Type[T]) -> T:
        """Generates structured JSON mapped to a Pydantic model with automatic provider fallback and retries."""
        errors: List[str] = []

        # 1. Try Primary: Gemini Models
        gemini = self._get_gemini()
        if gemini:
            # Ordered candidate models: configured model first, followed by known available variants
            candidate_models = [self.gemini_model, "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"]
            # Deduplicate preserving order
            models_to_try = list(dict.fromkeys(candidate_models))

            for model in models_to_try:
                def _call_gemini(m=model):
                    from google.genai import types
                    cfg = types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=response_schema,
                        temperature=0.1,
                    )
                    return gemini.models.generate_content(model=m, contents=prompt, config=cfg)

                try:
                    res = execute_with_retry(
                        _call_gemini,
                        provider_name=f"Gemini ({model})",
                        max_retries=self.max_retries,
                        initial_backoff=self.initial_backoff,
                    )
                    if hasattr(res, "parsed") and res.parsed:
                        return res.parsed
                    data = extract_json_dict(res.text)
                    return response_schema.model_validate(data)
                except Exception as e:
                    logger.warning("Gemini %s structured gen failed: %s", model, e)
                    errors.append(f"Gemini {model}: {e}")

        # 2. Try Fallback: OpenAI
        openai_client = self._get_openai()
        if openai_client:
            def _call_openai():
                return openai_client.beta.chat.completions.parse(
                    model=self.openai_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    response_format=response_schema,
                    temperature=0.1,
                )

            try:
                logger.info("Cascading to OpenAI fallback (%s)...", self.openai_model)
                completion = execute_with_retry(
                    _call_openai,
                    provider_name=f"OpenAI ({self.openai_model})",
                    max_retries=self.max_retries,
                    initial_backoff=self.initial_backoff,
                )
                parsed = completion.choices[0].message.parsed
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning("OpenAI fallback failed: %s", e)
                errors.append(f"OpenAI {self.openai_model}: {e}")

        # 3. Try Fallback: Anthropic Claude
        anthropic_client = self._get_anthropic()
        if anthropic_client:
            try:
                logger.info("Cascading to Anthropic Claude fallback (%s)...", self.anthropic_model)
                return self._generate_claude_structured(
                    anthropic_client=anthropic_client,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    response_schema=response_schema,
                )
            except Exception as e:
                logger.warning("Claude fallback failed: %s", e)
                errors.append(f"Claude {self.anthropic_model}: {e}")

        raise RuntimeError(f"All LLM providers failed: {'; '.join(errors)}")

    def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        """Generates freeform text/markdown with automatic provider fallback and retries."""
        errors: List[str] = []

        # 1. Try Gemini
        gemini = self._get_gemini()
        if gemini:
            candidate_models = list(dict.fromkeys([self.gemini_model, "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"]))
            for model in candidate_models:
                def _call_gemini_text(m=model):
                    from google.genai import types
                    cfg = types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3,
                    )
                    return gemini.models.generate_content(model=m, contents=prompt, config=cfg)

                try:
                    res = execute_with_retry(
                        _call_gemini_text,
                        provider_name=f"Gemini ({model})",
                        max_retries=self.max_retries,
                        initial_backoff=self.initial_backoff,
                    )
                    return res.text.strip()
                except Exception as e:
                    logger.warning("Gemini %s text gen failed: %s", model, e)
                    errors.append(f"Gemini {model}: {e}")

        # 2. Try OpenAI
        openai_client = self._get_openai()
        if openai_client:
            def _call_openai_text():
                return openai_client.chat.completions.create(
                    model=self.openai_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                )

            try:
                res = execute_with_retry(
                    _call_openai_text,
                    provider_name=f"OpenAI ({self.openai_model})",
                    max_retries=self.max_retries,
                    initial_backoff=self.initial_backoff,
                )
                return res.choices[0].message.content.strip()
            except Exception as e:
                logger.warning("OpenAI text gen failed: %s", e)
                errors.append(f"OpenAI {self.openai_model}: {e}")

        # 3. Try Anthropic
        anthropic_client = self._get_anthropic()
        if anthropic_client:
            def _call_anthropic_text():
                return anthropic_client.messages.create(
                    model=self.anthropic_model,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )

            try:
                msg = execute_with_retry(
                    _call_anthropic_text,
                    provider_name=f"Anthropic ({self.anthropic_model})",
                    max_retries=self.max_retries,
                    initial_backoff=self.initial_backoff,
                )
                return msg.content[0].text.strip()
            except Exception as e:
                logger.warning("Claude text gen failed: %s", e)
                errors.append(f"Claude {self.anthropic_model}: {e}")

        raise RuntimeError(f"All LLM providers failed to generate text: {'; '.join(errors)}")

    def probe_models(self) -> Dict[str, Dict[str, Any]]:
        """Probes all configured LLM providers at startup to verify credentials, model IDs, and reachability.
        
        Returns:
            Dictionary mapping provider name to status dictionary:
            {"available": bool, "model": str, "latency_ms": Optional[float], "error": Optional[str]}
        """
        results: Dict[str, Dict[str, Any]] = {}

        # 1. Probe Gemini
        if GEMINI_API_KEY:
            gemini = self._get_gemini()
            start = time.time()
            try:
                from google.genai import types
                cfg = types.GenerateContentConfig(max_output_tokens=5, temperature=0.0)
                gemini.models.generate_content(model=self.gemini_model, contents="ping", config=cfg)
                elapsed = (time.time() - start) * 1000.0
                results["gemini"] = {
                    "available": True,
                    "model": self.gemini_model,
                    "latency_ms": round(elapsed, 1),
                    "error": None,
                }
                logger.info(
                    "✅ [LLMGateway] Probe passed for Gemini model '%s' (%.1fms)",
                    self.gemini_model, elapsed
                )
            except Exception as e:
                results["gemini"] = {
                    "available": False,
                    "model": self.gemini_model,
                    "latency_ms": None,
                    "error": str(e),
                }
                logger.warning(
                    "⚠️ [LLMGateway] Probe failed for Gemini model '%s': %s",
                    self.gemini_model, e
                )

        # 2. Probe OpenAI
        if OPENAI_API_KEY:
            openai_client = self._get_openai()
            start = time.time()
            try:
                openai_client.chat.completions.create(
                    model=self.openai_model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                )
                elapsed = (time.time() - start) * 1000.0
                results["openai"] = {
                    "available": True,
                    "model": self.openai_model,
                    "latency_ms": round(elapsed, 1),
                    "error": None,
                }
                logger.info(
                    "✅ [LLMGateway] Probe passed for OpenAI model '%s' (%.1fms)",
                    self.openai_model, elapsed
                )
            except Exception as e:
                results["openai"] = {
                    "available": False,
                    "model": self.openai_model,
                    "latency_ms": None,
                    "error": str(e),
                }
                logger.warning(
                    "⚠️ [LLMGateway] Probe failed for OpenAI model '%s': %s",
                    self.openai_model, e
                )

        # 3. Probe Anthropic
        if ANTHROPIC_API_KEY:
            anthropic_client = self._get_anthropic()
            start = time.time()
            try:
                anthropic_client.messages.create(
                    model=self.anthropic_model,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "ping"}],
                )
                elapsed = (time.time() - start) * 1000.0
                results["anthropic"] = {
                    "available": True,
                    "model": self.anthropic_model,
                    "latency_ms": round(elapsed, 1),
                    "error": None,
                }
                logger.info(
                    "✅ [LLMGateway] Probe passed for Anthropic model '%s' (%.1fms)",
                    self.anthropic_model, elapsed
                )
            except Exception as e:
                results["anthropic"] = {
                    "available": False,
                    "model": self.anthropic_model,
                    "latency_ms": None,
                    "error": str(e),
                }
                logger.warning(
                    "⚠️ [LLMGateway] Probe failed for Anthropic model '%s': %s",
                    self.anthropic_model, e
                )

        # Summary check
        healthy_count = sum(1 for v in results.values() if v.get("available"))
        if not results:
            logger.warning("⚠️ [LLMGateway] No LLM API keys configured. LLM tasks will require credentials.")
        elif healthy_count == 0:
            logger.critical(
                "🚨 [LLMGateway] CRITICAL: All configured LLM model probes FAILED! "
                "Triage will NOT be able to classify messages until valid model IDs / API keys are provided: %s",
                {k: v["error"] for k, v in results.items()}
            )

        return results

    def probe_active_provider(self) -> Tuple[bool, str]:
        """Convenience method returning (is_healthy, summary_message) for startup banners."""
        probes = self.probe_models()
        if not probes:
            return False, "No LLM providers configured"
        for name, info in probes.items():
            if info["available"]:
                return True, f"{name.capitalize()} ({info['model']} - {info['latency_ms']}ms)"
        first_err = next((v["error"] for v in probes.values() if v.get("error")), "Unknown error")
        return False, f"All configured providers failed probe: {first_err}"
