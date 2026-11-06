import json
import logging
from typing import Type, TypeVar, Optional
from pydantic import BaseModel

from config import GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY

logger = logging.getLogger("LLMGateway")
T = TypeVar("T", bound=BaseModel)

class LLMGateway:
    """
    Multi-provider, resilient LLM Gateway.
    Automatically cascades: Gemini (Primary) -> OpenAI (Fallback) -> Claude (Fallback).
    Guarantees that rate limits or API key quotas never crash the system.
    """
    def __init__(self):
        self._gemini_client = None
        self._openai_client = None
        self._anthropic_client = None

    def _get_gemini(self):
        if self._gemini_client is None and GEMINI_API_KEY:
            from google import genai
            self._gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return self._gemini_client

    def _get_openai(self):
        if self._openai_client is None and OPENAI_API_KEY:
            from openai import OpenAI
            self._openai_client = OpenAI(api_key=OPENAI_API_KEY)
        return self._openai_client

    def _get_anthropic(self):
        if self._anthropic_client is None and ANTHROPIC_API_KEY:
            from anthropic import Anthropic
            self._anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
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
