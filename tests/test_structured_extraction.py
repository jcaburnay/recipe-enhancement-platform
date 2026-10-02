"""Offline regression checks for structured recipe extraction."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import BadRequestError, NotFoundError, RateLimitError

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
        self.modification = {
            "modification_type": "quantity_adjustment",
            "reasoning": "Less sugar",
            "evidence_quote": self.review.text,
            "edits": [{"target": "ingredients", "operation": "replace", "find": "1 cup sugar", "replace": "0.5 cup sugar", "add": None}],
        }

        self.payload = {"modifications": [self.modification], "unresolved": []}

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
        for obj in (schema, schema["$defs"]["ModificationObject"], schema["$defs"]["ModificationEdit"]):
            self.assertFalse(obj["additionalProperties"])
            self.assertEqual(set(obj["required"]), set(obj["properties"]))
        edit = schema["$defs"]["ModificationEdit"]
        self.assertIn({"type": "null"}, edit["properties"]["add"]["anyOf"])
        self.assertNotIn("default", edit["properties"]["add"])

    def test_valid_response_and_compatible_routing(self):
        self.create.return_value = self.response(self.payload)
        result = self.extractor.extract_modification(self.review, self.recipe)
        self.assertEqual(result.modifications[0].edits[0].replace, "0.5 cup sugar")
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(kwargs["extra_body"]["provider"], {"require_parameters": True})
        self.assertEqual(kwargs["max_tokens"], 4096)

    def test_wrong_wrapper_retries_then_accepts_valid_response(self):
        self.create.side_effect = [self.response(self.modification), self.response(self.payload)]
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

    def test_gemini_uses_schema_without_openrouter_routing(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "OPENROUTER_API_KEY": "other-key",
            "LLM_PROVIDER": "gemini", "LLM_REASONING_ENABLED": "false",
        }, clear=True):
            extractor = TweakExtractor()
        self.assertEqual(extractor.model, "gemini-3.8-flash")
        self.create.return_value = self.response(self.payload)
        self.assertIsNotNone(extractor.extract_modification(self.review, self.recipe))
        request = self.create.call_args.kwargs
        self.assertNotIn("extra_body", request)
        self.assertNotIn("reasoning_effort", request)
        self.assertEqual(request["response_format"]["type"], "json_schema")

    def test_invalid_provider_rejected(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "unknown"}, clear=True):
            with self.assertRaisesRegex(ValueError, "LLM_PROVIDER"):
                TweakExtractor()

    def test_instruction_only_change_is_rejected(self):
        self.modification["edits"] = [{"target": "instructions", "operation": "replace",
            "find": "Drop dough.", "replace": "Scoop dough.", "add": None}]
        self.create.return_value = self.response(self.payload)
        self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))

    def test_excluded_technique_is_retained_with_ingredient_change(self):
        self.payload["excluded_changes"] = ["Used a scoop: outside ingredient scope"]
        self.create.return_value = self.response(self.payload)
        result = self.extractor.extract_modifications(self.review, self.recipe)
        self.assertEqual(len(result.modifications), 1)
        self.assertEqual(result.unresolved, [])
        self.assertEqual(result.excluded_changes, self.payload["excluded_changes"])

    def test_retry_receives_wrong_section_anchor_error(self):
        invalid = dict(self.modification, edits=self.modification["edits"] + [{
            "target": "instructions", "operation": "add_after", "find": "1 cup sugar",
            "replace": None, "add": "Stir sugar."}])
        self.create.side_effect = [self.response({"modifications": [invalid], "unresolved": []}),
                                   self.response(self.payload)]
        result = self.extractor.extract_modifications(self.review, self.recipe)
        self.assertIsNotNone(result)
        messages = self.create.call_args.kwargs["messages"]
        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertIn("found 0", messages[-1]["content"])
        self.assertEqual(self.recipe.ingredients, ["1 cup sugar"])

    def test_retry_receives_operation_payload_error(self):
        invalid = dict(self.modification, edits=[dict(self.modification["edits"][0], add="wrong field")])
        self.create.side_effect = [self.response({"modifications": [invalid], "unresolved": []}),
                                   self.response(self.payload)]
        self.assertIsNotNone(self.extractor.extract_modifications(self.review, self.recipe))
        self.assertIn("add=null", self.create.call_args.kwargs["messages"][-1]["content"])

    def test_unquantified_addition_with_instruction_update_is_actionable(self):
        self.recipe.instructions = ["Mix sugar. Serve."]
        self.review.text = "I drizzled cream at serving."
        modification = dict(self.modification, modification_type="addition",
            evidence_quote=self.review.text, edits=[
                {"target": "ingredients", "operation": "add_after", "find": "",
                 "replace": None, "add": "Cream (amount unspecified), for drizzling"},
                {"target": "instructions", "operation": "replace", "find": "Serve.",
                 "replace": "Drizzle cream at serving.", "add": None}])
        self.create.return_value = self.response({"modifications": [modification], "unresolved": []})
        result = self.extractor.extract_modifications(self.review, self.recipe)
        self.assertEqual(result.unresolved, [])
        self.assertIn("amount unspecified", result.modifications[0].edits[0].add)
        self.assertEqual(self.recipe.ingredients, ["1 cup sugar"])
        self.assertEqual(self.recipe.instructions, ["Mix sugar. Serve."])

    def test_rate_limit_does_not_retry_immediately(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://example.test"))
        self.create.side_effect = RateLimitError("Quota exhausted", response=response, body=None)
        self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe))
        self.assertEqual(self.create.call_count, 1)

    def test_labeled_ingredient_intents(self):
        # Independently labeled cases: count, amount, application and statement type.
        cases = [
            ("I added an egg yolk to the batter.", "egg yolk", "1 egg yolk", "add to batter", []),
            ("I drizzled coconut cream at serving.", "coconut cream", None, "drizzle at serving", ["amount unspecified"]),
            ("I added toasted sesame seeds.", "toasted sesame seeds", None, None,
             ["amount unspecified", "application unspecified"]),
        ]
        for text, ingredient, amount, application, uncertainty in cases:
            with self.subTest(text=text):
                self.review.text = text
                intent = {"ingredient": ingredient, "amount": amount, "application": application,
                          "application_evidence": text if application else None, "uncertainties": uncertainty}
                modification = dict(self.modification, modification_type="addition", evidence_quote=text,
                    intent=intent, edits=[{"target": "ingredients", "operation": "add_after", "find": "",
                    "replace": None, "add": amount or ingredient + " (amount unspecified)"}])
                payload = {"statements": [{"evidence_quote": text, "classification": "performed_change"}],
                           "modifications": [modification], "unresolved": [], "excluded_changes": []}
                self.create.return_value = self.response(payload)
                result = self.extractor.extract_modifications(self.review, self.recipe, max_retries=0)
                self.assertEqual(result.modifications[0].intent.model_dump(), intent)
                if application is None:
                    payload["modifications"][0]["edits"].append({"target": "instructions",
                        "operation": "add_after", "find": "", "replace": None, "add": "Garnish and serve."})
                    self.create.return_value = self.response(payload)
                    self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))

    def test_future_and_hypothetical_statements_cannot_drive_changes(self):
        for text, classification in [("Next time I will halve sugar", "future_plan"),
                                      ("If unavailable, use honey", "hypothetical_suggestion")]:
            with self.subTest(classification=classification):
                self.review.text = text
                payload = {"statements": [{"evidence_quote": text, "classification": classification}],
                    "modifications": [dict(self.modification, evidence_quote=text)], "unresolved": []}
                self.create.return_value = self.response(payload)
                self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))
                payload["modifications"] = []
                payload["unresolved"] = [text + ": missing detail"]
                self.create.return_value = self.response(payload)
                self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))
                payload["unresolved"] = []
                self.create.return_value = self.response(payload)
                self.assertEqual(self.extractor.extract_modifications(self.review, self.recipe).modifications, [])

    def test_unspecified_amount_cannot_be_presented_as_known(self):
        self.modification["intent"] = {"ingredient": "sugar", "amount": "extra sugar (amount unspecified)",
            "application": None, "application_evidence": None, "uncertainties": []}
        self.create.return_value = self.response(self.payload)
        self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))

    def test_known_addition_application_requires_method_edit(self):
        self.recipe.instructions = ["Mix sugar. Serve."]
        self.modification.update(modification_type="addition", intent={"ingredient": "sugar",
            "amount": "0.5 cup sugar", "application": "mix in", "application_evidence": self.review.text,
            "uncertainties": []})
        self.create.return_value = self.response(self.payload)
        self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))

    def test_all_discrete_modifications_preserved(self):
        self.review.text += " and added one egg"
        second = dict(self.modification, modification_type="addition", reasoning="No reason stated",
                      evidence_quote="added one egg", edits=[{"target": "ingredients", "operation": "add_after", "find": "0.5 cup sugar", "replace": None, "add": "1 egg"}])
        self.payload["modifications"].append(second)
        self.create.return_value = self.response(self.payload)
        result = self.extractor.extract_modifications(self.review, self.recipe)
        self.assertEqual(len(result.modifications), 2)
        self.assertEqual(result.modifications[1].modification_type, "addition")

    def test_invalid_evidence_or_empty_edits_is_rejected(self):
        for update in ({"evidence_quote": "not in source"}, {"evidence_quote": ""}, {"edits": []}):
            with self.subTest(update=update):
                self.create.return_value = self.response({"modifications": [dict(self.modification, **update)], "unresolved": []})
                self.assertIsNone(self.extractor.extract_modifications(self.review, self.recipe, max_retries=0))

    def test_empty_and_unresolved_results_preserved(self):
        for unresolved in ([], ["Added some milk: amount unspecified"]):
            self.create.return_value = self.response({"modifications": [], "unresolved": unresolved})
            result = self.extractor.extract_modifications(self.review, self.recipe)
            self.assertEqual(result.modifications, [])
            self.assertEqual(result.unresolved, unresolved)

    def test_featured_selection_votes_then_stable_order(self):
        first = Review(text="first", is_featured=True, rating=1)
        second = Review(text="second", is_featured=True, rating=5)
        unfeatured = Review(text="unfeatured", helpful_votes=100, has_modification=True)
        self.assertIs(self.extractor.select_review([first, second, unfeatured]), first)
        second.helpful_votes = 3
        self.assertIs(self.extractor.select_review([first, second, unfeatured]), second)
        first.helpful_votes = 3
        self.assertIs(self.extractor.select_review([first, second]), first)
        self.assertIsNone(self.extractor.select_review([unfeatured]))

    def test_selected_review_can_lack_regex_flag(self):
        self.review.is_featured = True
        self.review.has_modification = False
        self.create.return_value = self.response(self.payload)
        result, source = self.extractor.extract_review_modifications([self.review], self.recipe)
        self.assertEqual(len(result.modifications), 1)
        self.assertIs(source, self.review)

    def test_synthetic_prompt_example_is_grounded_discrete_and_applicable(self):
        from llm_pipeline.prompts import EXAMPLE_SOURCE, EXAMPLE_RESULT, build_simple_prompt
        from llm_pipeline.models import ExtractionResult
        from llm_pipeline.recipe_modifier import RecipeModifier
        result = ExtractionResult.model_validate(EXAMPLE_RESULT)
        self.assertEqual(len(result.modifications), 4)
        self.assertEqual([m.modification_type for m in result.modifications[:2]],
                         ["quantity_adjustment", "quantity_adjustment"])
        edits = []
        for modification in result.modifications:
            self.assertIn(modification.evidence_quote, EXAMPLE_SOURCE["review"])
            edits.extend(edit.model_dump_json() for edit in modification.edits)
        self.assertEqual(len(edits), len(set(edits)))
        recipe = Recipe(recipe_id="synthetic", title="Synthetic", ingredients=EXAMPLE_SOURCE["ingredients"],
                        instructions=EXAMPLE_SOURCE["instructions"])
        enhanced, _ = RecipeModifier().apply_modifications_batch(recipe, result.modifications)
        self.assertNotIn("1 cup water", enhanced.ingredients)
        self.assertIn("1 teaspoon lemon juice", enhanced.ingredients)
        self.assertEqual(enhanced.instructions, ["Mix honey, syrup, and lemon juice. Pour into molds."])
        prompt = build_simple_prompt(self.review.text, self.recipe.title, self.recipe.ingredients, self.recipe.instructions)
        self.assertIn("CONTIGUOUS", prompt)
        self.assertIn("never duplicate", prompt)


if __name__ == "__main__":
    unittest.main()
