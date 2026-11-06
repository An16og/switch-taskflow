import json
import logging
from typing import Type, TypeVar, Optional
from pydantic import BaseModel

from config import (
    GEMINI_API_KEY,
    OPENAI_API_KEY,
    ANTHROPIC_API_KEY,
    SWITCH_TRUST_API_KEY,
    SWITCH_TRUST_GATEWAY_URL,
    SWITCH_TRUST_POLICY_ID,
    SWITCH_TRUST_REQUIRE_GUARDRAILS,
    SWITCH_TRUST_ALLOW_INSECURE_GATEWAY,
)

logger = logging.getLogger("LLMGateway")
T = TypeVar("T", bound=BaseModel)

class LLMGateway:
    """
    Multi-provider, resilient LLM Gateway integrated with Switch Trust guardrails.
    Automatically wraps every client with switch_trust.wrap(...) to intercept prompt injections,
    data leaks, and toxic content before requests reach upstream model providers.
    Cascades: Gemini (Primary) -> OpenAI (Fallback) -> Claude (Fallback).
    """
    def __init__(self):
        self._gemini_client = None
        self._openai_client = None
        self._anthropic_client = None

    def _wrap_trust(self, client):
        """Wraps an LLM client with Switch Trust guardrails routing."""
        try:
            import os
            import switch_trust

            # If user configured SWITCH_TRUST_API_KEY, activate full guardrails routing
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
                # If no API key is provided yet, wrap with require_guardrails=False
                # and remove partial gateway_url from os.environ to avoid mismatched parameter error
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

    def generate_structured(self, prompt: str, system_prompt: str, response_schema: Type[T]) -> T:
        """
        Generates structured JSON mapped to a Pydantic model with automatic provider fallback.
        """
        errors = []

        # 1. Try Primary: Gemini Models (cascade across active versions)
        gemini = self._get_gemini()
        if gemini:
            models_to_try = [
                "gemini-3.5-flash-lite",
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite"
            ]
            for model in models_to_try:
                try:
                    from google.genai import types
                    cfg = types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=response_schema,
                        temperature=0.1
                    )
                    res = gemini.models.generate_content(model=model, contents=prompt, config=cfg)
                    if res.parsed:
                        return res.parsed
                    data = json.loads(res.text)
                    return response_schema.model_validate(data)
                except Exception as e:
                    logger.warning(f"Gemini {model} failed: {e}")
                    errors.append(f"Gemini {model}: {e}")

        # 2. Try Fallback: OpenAI (gpt-4o-mini)
        openai_client = self._get_openai()
        if openai_client:
            try:
                logger.info("Cascading to OpenAI fallback...")
                completion = openai_client.beta.chat.completions.parse(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    response_format=response_schema,
                    temperature=0.1
                )
                parsed = completion.choices[0].message.parsed
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning(f"OpenAI fallback failed: {e}")
                errors.append(f"OpenAI: {e}")

        # 3. Try Fallback: Anthropic Claude
        anthropic_client = self._get_anthropic()
        if anthropic_client:
            try:
                logger.info("Cascading to Anthropic Claude fallback...")
                json_prompt = f"{prompt}\n\nRespond ONLY with a valid JSON object matching the requested schema."
                msg = anthropic_client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": json_prompt}],
                    temperature=0.1
                )
                text_content = msg.content[0].text
                data = json.loads(text_content)
                return response_schema.model_validate(data)
            except Exception as e:
                logger.warning(f"Claude fallback failed: {e}")
                errors.append(f"Claude: {e}")

        raise RuntimeError(f"All LLM providers failed: {'; '.join(errors)}")

    def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        """
        Generates freeform text/markdown with automatic provider fallback.
        """
        # 1. Try Gemini
        gemini = self._get_gemini()
        if gemini:
            for model in ["gemini-3.5-flash", "gemini-3.1-flash-lite"]:
                try:
                    from google.genai import types
                    cfg = types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3
                    )
                    res = gemini.models.generate_content(model=model, contents=prompt, config=cfg)
                    return res.text.strip()
                except Exception as e:
                    logger.warning(f"Gemini {model} text gen failed: {e}")

        # 2. Try OpenAI
        openai_client = self._get_openai()
        if openai_client:
            try:
                res = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3
                )
                return res.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"OpenAI text gen failed: {e}")

        # 3. Try Anthropic
        anthropic_client = self._get_anthropic()
        if anthropic_client:
            try:
                msg = anthropic_client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}]
                )
                return msg.content[0].text.strip()
            except Exception as e:
                logger.warning(f"Claude text gen failed: {e}")

        raise RuntimeError("All LLM providers failed to generate text.")
