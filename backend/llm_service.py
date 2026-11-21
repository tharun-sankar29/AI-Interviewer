"""Provider-neutral LLM access with narrowly scoped Groq -> Gemini failover."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class LLMServiceUnavailable(RuntimeError):
    """A required model request could not be served safely."""

    category: str
    public_message: str = "The AI service is temporarily unavailable. Please retry this request."

    def __str__(self) -> str:
        return self.public_message


class InvalidModelResponse(LLMServiceUnavailable):
    def __init__(self, message: str = "The AI service returned an invalid response. Please retry."):
        super().__init__("invalid_response", message)


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None and getattr(exc, "response", None) is not None:
        value = getattr(exc.response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _groq_failure_category(exc: Exception) -> tuple[str, bool]:
    """Return (safe category, may_fallback). Avoid depending on SDK internals."""
    status = _status_code(exc)
    name = type(exc).__name__.lower()
    if status == 429:
        return "rate_limit", True
    if status is not None and status >= 500:
        return "server_error", True
    if "timeout" in name:
        return "timeout", True
    if "connection" in name:
        return "connection", True
    if status in (401, 403):
        return "configuration", False
    if status == 404:
        return "model_not_found", False
    if status is not None and 400 <= status < 500:
        return "invalid_request", False
    return "provider_error", False


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "response") -> None:
    """Small JSON-Schema subset sufficient for the service's strict contracts."""
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if expected in types and (not isinstance(value, types[expected]) or expected == "integer" and isinstance(value, bool)):
        raise InvalidModelResponse(f"The AI service returned an invalid {path}.")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidModelResponse(f"The AI service returned an unsupported value in {path}.")
    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise InvalidModelResponse(f"The AI service response omitted {path}.{key}.")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(schema.get("properties", {}))
            if extras:
                raise InvalidModelResponse(f"The AI service returned unexpected fields in {path}.")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                _validate_schema(value[key], child, f"{path}.{key}")
    elif expected == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise InvalidModelResponse(f"The AI service returned too few items in {path}.")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise InvalidModelResponse(f"The AI service returned too many items in {path}.")
        for index, item in enumerate(value):
            _validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
    elif expected in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            raise InvalidModelResponse(f"The AI service returned an out-of-range value in {path}.")
    elif expected == "string" and "minLength" in schema and len(value.strip()) < schema["minLength"]:
        raise InvalidModelResponse(f"The AI service returned an empty value in {path}.")


def _gemini_response_schema(schema: Any) -> Any:
    """Omit unsupported array upper bounds on the wire, not in local validation."""
    if isinstance(schema, dict):
        return {key: _gemini_response_schema(value) for key, value in schema.items() if key != "maxItems"}
    if isinstance(schema, list):
        return [_gemini_response_schema(value) for value in schema]
    return schema


class LLMService:
    def __init__(self, groq_client=None, gemini_client=None):
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        self.gemini_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
        # The former gemini-2.5-flash ID is no longer available to some
        # accounts. This rolling alias is supported by the configured API.
        self.gemini_init_model = os.getenv("GEMINI_INIT_MODEL", "gemini-3.5-flash-lite")
        self._groq_client = groq_client
        self._gemini_client = gemini_client

    def _groq(self):
        if self._groq_client is None:
            key = os.getenv("GROQ_API_KEY")
            if not key:
                raise LLMServiceUnavailable("configuration", "The primary AI service is not configured.")
            try:
                from groq import Groq
                self._groq_client = Groq(api_key=key, timeout=8.0, max_retries=0)
            except LLMServiceUnavailable:
                raise
            except Exception as exc:
                raise LLMServiceUnavailable("configuration", "The primary AI service could not be configured.") from exc
        return self._groq_client

    def _gemini(self):
        if self._gemini_client is None:
            key = os.getenv("GEMINI_API_KEY")
            if not key:
                raise LLMServiceUnavailable("fallback_configuration")
            try:
                from google import genai
                self._gemini_client = genai.Client(api_key=key)
            except Exception as exc:
                raise LLMServiceUnavailable("fallback_configuration") from exc
        return self._gemini_client

    @staticmethod
    def _content(response) -> str:
        try:
            return response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise InvalidModelResponse() from exc

    def _run(self, prompt: str, *, schema: dict[str, Any] | None, options: dict[str, Any]) -> str:
        started = time.perf_counter()
        try:
            kwargs = {
                "model": self.groq_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": options.get("temperature", 0.3),
                "max_completion_tokens": options.get("max_tokens", 500),
                "reasoning_effort": "low",
            }
            if schema is not None:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": options.get("schema_name", "response"), "strict": True, "schema": schema},
                }
            result = self._groq().chat.completions.create(**kwargs)
            logger.info("llm_request provider=groq latency_ms=%d category=success fallback=false", (time.perf_counter() - started) * 1000)
            return self._content(result)
        except InvalidModelResponse:
            raise
        except LLMServiceUnavailable:
            raise
        except Exception as exc:
            category, may_fallback = _groq_failure_category(exc)
            logger.warning("llm_request provider=groq latency_ms=%d category=%s fallback=%s", (time.perf_counter() - started) * 1000, category, str(may_fallback).lower())
            if not may_fallback or options.get("allow_gemini_fallback") is False:
                raise LLMServiceUnavailable(category, "The primary AI service is unavailable due to its configuration.") from exc

        fallback_started = time.perf_counter()
        try:
            from google.genai import types
            config = types.GenerateContentConfig(
                http_options=types.HttpOptions(
                    timeout=20000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
                temperature=options.get("temperature", 0.3),
                max_output_tokens=options.get("max_tokens", 500),
                response_mime_type="application/json" if schema is not None else "text/plain",
                response_json_schema=_gemini_response_schema(schema) if schema is not None else None,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
            result = self._gemini().models.generate_content(model=self.gemini_model, contents=prompt, config=config)
            text = getattr(result, "text", None)
            if not text:
                raise InvalidModelResponse()
            logger.info("llm_request provider=gemini latency_ms=%d category=success fallback=true", (time.perf_counter() - fallback_started) * 1000)
            return text
        except InvalidModelResponse:
            raise
        except Exception as exc:
            logger.error("llm_request provider=gemini latency_ms=%d category=provider_error fallback=true", (time.perf_counter() - fallback_started) * 1000)
            raise LLMServiceUnavailable("dual_provider_failure") from exc

    def complete_text(self, prompt: str, options: dict[str, Any] | None = None) -> str:
        text = self._run(prompt, schema=None, options=options or {}).strip()
        if not text:
            raise InvalidModelResponse()
        return text

    def complete_json(self, prompt: str, schema: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = self._run(prompt, schema=schema, options=options or {})
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidModelResponse() from exc
        _validate_schema(value, schema)
        return value

    def complete_groq_json(self, prompt: str, schema: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Use Groq only; graph recovery must not retry a failing Gemini request."""
        return self.complete_json(prompt, schema, {**(options or {}), "allow_gemini_fallback": False})

    def complete_gemini_json(self, prompt: str, schema: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a strict JSON request directly against the configured Gemini init model."""
        options = options or {}
        started = time.perf_counter()
        try:
            from google.genai import types
            config = types.GenerateContentConfig(
                http_options=types.HttpOptions(
                    timeout=options.get("timeout_ms", 30000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
                temperature=options.get("temperature", 0.2),
                max_output_tokens=options.get("max_tokens", 1200),
                response_mime_type="application/json",
                response_json_schema=_gemini_response_schema(schema),
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
            response = self._gemini().models.generate_content(
                model=self.gemini_init_model,
                contents=prompt,
                config=config,
            )
            raw = getattr(response, "text", None)
            if not raw:
                raise InvalidModelResponse()
            value = json.loads(raw)
            _validate_schema(value, schema)
            logger.info("llm_request provider=gemini model=%s latency_ms=%d category=success direct=true", self.gemini_init_model, (time.perf_counter() - started) * 1000)
            return value
        except InvalidModelResponse:
            raise
        except json.JSONDecodeError as exc:
            raise InvalidModelResponse() from exc
        except LLMServiceUnavailable:
            raise
        except Exception as exc:
            logger.error("llm_request provider=gemini model=%s latency_ms=%d category=provider_error direct=true", self.gemini_init_model, (time.perf_counter() - started) * 1000)
            raise LLMServiceUnavailable("gemini_initialization_failure") from exc


_service: LLMService | None = None


def get_llm_service() -> LLMService:
    global _service
    if _service is None:
        _service = LLMService()
    return _service


def complete_text(prompt: str, options: dict[str, Any] | None = None) -> str:
    return get_llm_service().complete_text(prompt, options)


def complete_json(prompt: str, schema: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_llm_service().complete_json(prompt, schema, options)


def complete_groq_json(prompt: str, schema: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_llm_service().complete_groq_json(prompt, schema, options)


def complete_gemini_json(prompt: str, schema: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_llm_service().complete_gemini_json(prompt, schema, options)
