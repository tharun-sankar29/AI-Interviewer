#!/usr/bin/env python3
"""Verify the real Gemini fallback without requiring or exposing a Groq key."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from backend.llm_service import LLMService, LLMServiceUnavailable


class APITimeoutError(Exception):
    """Simulates the transient error emitted by the Groq SDK."""


class _TransientGroqCompletions:
    def create(self, **_kwargs):
        raise APITimeoutError("simulated timeout")


class _TransientGroqClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _TransientGroqCompletions()})()


SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "provider": {"type": "string"},
    },
    "required": ["status", "provider"],
    "additionalProperties": False,
}


def main():
    load_dotenv()
    if not os.getenv("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY is not configured.")

    if not os.getenv("GROQ_API_KEY"):
        try:
            LLMService().complete_text("Configuration check")
        except LLMServiceUnavailable as exc:
            if exc.category != "configuration":
                raise
            print("Missing Groq key correctly classified as configuration (no fallback).")
        else:
            raise SystemExit("Expected the missing Groq key to be rejected.")

    service = LLMService(groq_client=_TransientGroqClient())
    result = service.complete_json(
        "Return status 'fallback_ok' and provider 'gemini'.",
        SCHEMA,
        {"temperature": 0, "max_tokens": 80, "schema_name": "fallback_health"},
    )
    if result.get("status") != "fallback_ok" or result.get("provider") != "gemini":
        raise SystemExit("Gemini returned an unexpected structured response.")
    print("Transient Groq failure successfully fell back to Gemini with valid structured JSON.")


if __name__ == "__main__":
    main()
