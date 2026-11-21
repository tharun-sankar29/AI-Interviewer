#!/usr/bin/env python3
"""Verify Groq primary access and strict structured output without fallback."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from backend.interview_session import TURN_SCHEMA
from backend.llm_service import LLMService


class _DisabledGeminiModels:
    def generate_content(self, **_kwargs):
        raise RuntimeError("Fallback was disabled for the Groq primary verification.")


class _DisabledGeminiClient:
    def __init__(self):
        self.models = _DisabledGeminiModels()


def main():
    load_dotenv()
    if not os.getenv("GROQ_API_KEY"):
        raise SystemExit("GROQ_API_KEY is not configured.")

    service = LLMService(gemini_client=_DisabledGeminiClient())
    started = time.perf_counter()
    result = service.complete_json(
        """Evaluate this sample interview response using the required JSON schema.
Question: What is a database index?
Answer: A database index is a data structure that speeds up reads at the cost of storage and additional write work.
Return a concise reaction, integer score, evaluation, and useful next question.""",
        TURN_SCHEMA,
        {"temperature": 0, "max_tokens": 300, "schema_name": "interview_turn"},
    )
    elapsed = time.perf_counter() - started
    if set(result) != {"reaction", "score", "evaluation", "next_question"}:
        raise SystemExit("Groq returned an unexpected interview-turn response.")
    print(f"Groq primary verified with the production interview schema using {service.groq_model} (score={result['score']}/10, latency={elapsed:.2f}s).")


if __name__ == "__main__":
    main()
