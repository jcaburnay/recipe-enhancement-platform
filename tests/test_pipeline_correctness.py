"""Offline integration tests using real orchestration and deterministic extraction."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_pipeline.models import ExtractionResult, ModificationEdit, ModificationObject
from llm_pipeline.pipeline import DATA_DIR, LLMAnalysisPipeline


class PipelineCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.client = patch("llm_pipeline.pipeline.TweakExtractor")
        self.extractor = self.client.start().return_value
        self.addCleanup(self.client.stop)
        self.pipeline = LLMAnalysisPipeline(output_dir=str(self.root / "output"))
        self.data = {
            "recipe_id": "test", "title": "Cookies", "ingredients": ["2 eggs", "1 cup sugar"],
            "instructions": ["Mix eggs and sugar."],
            "featured_tweaks": [{"text": "I added an egg and halved the sugar", "rating": 5}],
            "reviews": [{"text": "I added an egg and halved the sugar", "rating": 5}],
        }
        self.path = self.root / "recipe_test.json"
        self.path.write_text(json.dumps(self.data))
        self.modifications = [
            ModificationObject(modification_type="addition", reasoning="Reviewer preference", evidence_quote="added an egg",
                edits=[ModificationEdit(target="ingredients", operation="replace", find="2 eggs", replace="3 eggs")]),
            ModificationObject(modification_type="quantity_adjustment", reasoning="Reviewer preference", evidence_quote="halved the sugar",
                edits=[ModificationEdit(target="ingredients", operation="replace", find="1 cup sugar", replace="0.5 cup sugar")]),
        ]
        self.review = self.pipeline.parse_reviews_data(self.data)[0]

    def set_extraction(self, modifications=None, unresolved=None):
        result = ExtractionResult(modifications=self.modifications if modifications is None else modifications,
                                  unresolved=unresolved or [])
        self.extractor.extract_review_modifications.return_value = (result, self.review)

    def test_all_discrete_changes_have_attribution_and_final_line_diffs(self):
        self.set_extraction()
        result = self.pipeline.process_single_recipe(str(self.path))
        self.assertEqual(len(result.modifications_applied), 2)
        self.assertEqual(result.ingredients, ["3 eggs", "0.5 cup sugar"])
        self.assertEqual(len(result.line_diffs), 2)
        self.assertEqual(result.line_diffs[1].before_index, 1)
        self.assertTrue(result.modifications_applied[0].source_review.is_featured)
        saved = next((self.root / "output").glob("enhanced_*.json"))
        self.assertEqual(json.loads(saved.read_text())["pipeline_version"], "2.0.0")

    def test_excluded_technique_does_not_block_and_is_saved(self):
        excluded = ["Used a scoop: outside ingredient scope"]
        extraction = ExtractionResult(modifications=self.modifications, unresolved=[], excluded_changes=excluded)
        self.extractor.extract_review_modifications.return_value = (extraction, self.review)
        result = self.pipeline.process_single_recipe(str(self.path))
        self.assertEqual(result.excluded_changes, excluded)
        saved = next((self.root / "output").glob("enhanced_*.json"))
        self.assertEqual(json.loads(saved.read_text())["excluded_changes"], excluded)
        self.assertEqual(result.instructions, self.data["instructions"])

    def test_unresolved_changes_do_not_publish_partial_recipe(self):
        self.set_extraction(unresolved=["extra cinnamon amount unknown"])
        self.assertIsNone(self.pipeline.process_single_recipe(str(self.path)))
        self.assertEqual(self.pipeline.processing_results[-1]["reason"], "unresolved_changes")
        self.assertEqual(list((self.root / "output").glob("*.json")), [])

    def test_failed_second_modification_does_not_save_first(self):
        invalid = self.modifications[1].model_copy(deep=True)
        invalid.edits[0].find = "missing ingredient"
        self.set_extraction(modifications=[self.modifications[0], invalid])
        self.assertIsNone(self.pipeline.process_single_recipe(str(self.path)))
        self.assertEqual(self.pipeline.processing_results[-1]["status"], "failed")
        self.assertEqual(list((self.root / "output").glob("*.json")), [])

    def test_empty_extraction_and_missing_featured_are_skips(self):
        self.set_extraction(modifications=[])
        self.assertIsNone(self.pipeline.process_single_recipe(str(self.path)))
        self.assertEqual(self.pipeline.processing_results[-1]["reason"], "no_actionable_changes")
        self.data["featured_tweaks"] = []
        self.path.write_text(json.dumps(self.data))
        self.assertIsNone(self.pipeline.process_single_recipe(str(self.path)))
        self.assertEqual(self.pipeline.processing_results[-1]["reason"], "no_featured_reviews")

    def test_summary_distinguishes_failures_from_skips(self):
        self.pipeline._record_result("a", "failed", "unresolved_changes")
        self.pipeline._record_result("b", "skipped", "no_featured_reviews")
        report = self.pipeline.generate_summary_report([])
        self.assertEqual(report["pipeline_summary"]["failed"], 1)
        self.assertEqual(report["pipeline_summary"]["skipped"], 1)
        self.assertEqual(report["pipeline_summary"]["recipes_processed"], 0)

    def test_default_paths_do_not_depend_on_working_directory(self):
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            pipeline = LLMAnalysisPipeline()
            self.assertEqual(pipeline.output_dir, DATA_DIR / "enhanced")
            with patch.object(pipeline, "process_single_recipe", return_value=None) as process:
                pipeline.process_recipe_directory()
            self.assertEqual(process.call_count, 6)
        finally:
            os.chdir(old_cwd)

    def test_featured_data_is_deduplicated_and_flags_preserved(self):
        reviews = self.pipeline.parse_reviews_data(self.data)
        self.assertEqual(len(reviews), 1)
        self.assertTrue(reviews[0].is_featured)

    def test_duplicate_review_retains_votes_for_featured_selection(self):
        from llm_pipeline.tweak_extractor import TweakExtractor
        data = {
            "featured_tweaks": [{"text": "A"}, {"text": "B", "helpful_votes": 1}],
            "reviews": [{"text": "A", "helpful_votes": 100}],
        }
        reviews = self.pipeline.parse_reviews_data(data)
        self.assertEqual([r.text for r in reviews], ["A", "B"])
        self.assertEqual(TweakExtractor.select_review(reviews).text, "A")


if __name__ == "__main__":
    unittest.main()
