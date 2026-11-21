import json
import unittest

from backend.llm_service import InvalidModelResponse, LLMService, LLMServiceUnavailable


SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 10}},
    "required": ["score"],
    "additionalProperties": False,
}


class _Message:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Message(content)


class _Response:
    def __init__(self, content): self.choices = [_Choice(content)]


class _GroqCompletions:
    def __init__(self, result=None, error=None): self.result, self.error, self.calls = result, error, []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return _Response(self.result)


class _Groq:
    def __init__(self, completions):
        self.chat = type("Chat", (), {"completions": completions})()


class _GeminiModels:
    def __init__(self, text=None, error=None): self.text, self.error, self.calls = text, error, []
    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return type("GeminiResponse", (), {"text": self.text})()


class _Gemini:
    def __init__(self, models): self.models = models


def _error(name, status=None):
    return type(name, (Exception,), {"status_code": status})("provider failed")


class LLMServiceTests(unittest.TestCase):
    def test_groq_strict_json_success(self):
        completions = _GroqCompletions('{"score": 8}')
        service = LLMService(_Groq(completions), _Gemini(_GeminiModels()))
        self.assertEqual(service.complete_json("prompt", SCHEMA), {"score": 8})
        call = completions.calls[0]
        self.assertEqual(call["reasoning_effort"], "low")
        self.assertTrue(call["response_format"]["json_schema"]["strict"])

    def test_all_transient_conditions_fall_back(self):
        for exc in (_error("APITimeoutError"), _error("APIConnectionError"), _error("RateLimitError", 429), _error("InternalServerError", 500), _error("BadGateway", 503)):
            with self.subTest(error=type(exc).__name__):
                models = _GeminiModels('{"score": 7}')
                service = LLMService(_Groq(_GroqCompletions(error=exc)), _Gemini(models))
                self.assertEqual(service.complete_json("prompt", SCHEMA), {"score": 7})
                self.assertEqual(len(models.calls), 1)

    def test_configuration_errors_do_not_fall_back(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                models = _GeminiModels('{"score": 7}')
                service = LLMService(_Groq(_GroqCompletions(error=_error("APIStatusError", status))), _Gemini(models))
                with self.assertRaises(LLMServiceUnavailable): service.complete_json("prompt", SCHEMA)
                self.assertEqual(models.calls, [])

    def test_dual_provider_failure_is_503_error(self):
        service = LLMService(_Groq(_GroqCompletions(error=_error("RateLimitError", 429))), _Gemini(_GeminiModels(error=RuntimeError("down"))))
        with self.assertRaises(LLMServiceUnavailable) as caught:
            service.complete_json("prompt", SCHEMA)
        self.assertEqual(caught.exception.category, "dual_provider_failure")

    def test_schema_violation_is_not_fabricated(self):
        service = LLMService(_Groq(_GroqCompletions(json.dumps({"score": 11}))), _Gemini(_GeminiModels()))
        with self.assertRaises(InvalidModelResponse): service.complete_json("prompt", SCHEMA)

    def test_enum_violation_is_rejected(self):
        schema = {
            "type": "object",
            "properties": {"basis": {"type": "string", "enum": ["jd_requirement"]}},
            "required": ["basis"],
            "additionalProperties": False,
        }
        service = LLMService(_Groq(_GroqCompletions('{"basis":"unrelated"}')), _Gemini(_GeminiModels()))
        with self.assertRaises(InvalidModelResponse):
            service.complete_json("prompt", schema)


if __name__ == "__main__":
    unittest.main()
