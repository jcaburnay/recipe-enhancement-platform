"""Offline regression checks for structured recipe extraction."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import BadRequestError, NotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_pipeline.models import Recipe, Review
from llm_pipeline.tweak_extractor import TweakExtractor, build_modification_response_format


class StructuredExtractionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        client = patch("llm_pipeline.tweak_extractor.OpenAI")
        self.client = client.start().return_value
        self.addCleanup(client.stop)
        self.extractor = TweakExtractor()
        self.create = self.client.chat.completions.create
        self.recipe = Recipe(recipe_id="test", title="Cookies", ingredients=["1 cup sugar"], instructions=[])
        self.review = Review(text="I used half the sugar", has_modification=True)
        self.payload = {
            "modification_type": "quantity_adjustment",
            "reasoning": "Less sugar",
            "edits": [{"target": "ingredients", "operation": "replace", "find": "1 cup sugar", "replace": "0.5 cup sugar", "add": None}],
        }

    def response(self, payload, finish_reason="stop", refusal=None):
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=json.dumps(payload), refusal=refusal),
        )])

    def test_strict_schema_requires_nullable_fields_and_forbids_extra_keys(self):
        response_format = build_modification_response_format()
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        for obj in (schema, schema["$defs"]["ModificationEdit"]):
            self.assertFalse(obj["additionalProperties"])
            self.assertEqual(set(obj["required"]), set(obj["properties"]))
        edit = schema["$defs"]["ModificationEdit"]
        self.assertIn({"type": "null"}, edit["properties"]["add"]["anyOf"])
        self.assertNotIn("default", edit["properties"]["add"])

    def test_valid_response_and_compatible_routing(self):
        self.create.return_value = self.response(self.payload)
        result = self.extractor.extract_modification(self.review, self.recipe)
        self.assertEqual(result.edits[0].replace, "0.5 cup sugar")
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(kwargs["extra_body"]["provider"], {"require_parameters": True})
        self.assertEqual(kwargs["max_tokens"], 4096)

    def test_wrong_wrapper_retries_then_accepts_valid_response(self):
        self.create.side_effect = [self.response({"modifications": [self.payload]}), self.response(self.payload)]
        self.assertIsNotNone(self.extractor.extract_modification(self.review, self.recipe))
        self.assertEqual(self.create.call_count, 2)

    def test_length_response_is_rejected_even_when_json_is_parseable(self):
        self.create.return_value = self.response(self.payload, finish_reason="length")
        self.assertIsNone(self.extractor.extract_modification(self.review, self.recipe))
        self.assertEqual(self.create.call_count, 1)

    def test_optional_reasoning_can_be_disabled(self):
        with patch.dict(os.environ, {"LLM_REASONING_ENABLED": "false"}):
            extractor = TweakExtractor()
        self.create.return_value = self.response(self.payload)
        self.assertIsNotNone(extractor.extract_modification(self.review, self.recipe))
        self.assertEqual(self.create.call_args.kwargs["extra_body"]["reasoning"], {"enabled": False})

    def test_refusal_does_not_retry(self):
        self.create.return_value = self.response(None, refusal="Refused")
        self.assertIsNone(self.extractor.extract_modification(self.review, self.recipe))
        self.assertEqual(self.create.call_count, 1)

    def test_unsupported_schema_or_endpoint_does_not_retry(self):
        for error_type, status in ((BadRequestError, 400), (NotFoundError, 404)):
            with self.subTest(status=status):
                self.create.reset_mock()
                response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
                self.create.side_effect = error_type("Unsupported schema", response=response, body=None)
                self.assertIsNone(self.extractor.extract_modification(self.review, self.recipe))
                self.assertEqual(self.create.call_count, 1)

    def test_openai_request_omits_openrouter_routing(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            extractor = TweakExtractor()
        self.create.return_value = self.response(self.payload)
        self.assertIsNotNone(extractor.extract_modification(self.review, self.recipe))
        self.assertNotIn("extra_body", self.create.call_args.kwargs)
        self.assertEqual(extractor.model, "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
