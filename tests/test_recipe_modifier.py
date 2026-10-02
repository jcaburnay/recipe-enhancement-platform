"""Deterministic adversarial checks for textual edit correctness."""

import unittest

from src.llm_pipeline.models import ModificationEdit, ModificationObject, Recipe
from src.llm_pipeline.recipe_modifier import RecipeModifier


def edit(find, operation="replace", target="ingredients", replace=None, add=None):
    return ModificationEdit(target=target, operation=operation, find=find, replace=replace, add=add)


def modification(*edits):
    return ModificationObject(modification_type="quantity_adjustment", reasoning="Reviewer adjustment",
                              evidence_quote="halved the sugar", edits=list(edits))


class RecipeModifierTests(unittest.TestCase):
    def setUp(self):
        self.modifier = RecipeModifier()
        self.recipe = Recipe(recipe_id="sample", title="Cookies",
                             ingredients=["1 cup sugar", "2 eggs", "1 cup walnuts"],
                             instructions=["Bake at 350 degrees F.", "Mix walnuts and sugar."])

    def test_unique_substring_replaces_only_requested_temperature(self):
        changed, records = self.modifier.apply_modification(self.recipe, modification(
            edit("350", target="instructions", replace="325")))
        self.assertEqual(changed.instructions[0], "Bake at 325 degrees F.")
        self.assertEqual(records[0].from_text, "Bake at 350 degrees F.")
        self.assertEqual((records[0].before_index, records[0].after_index), (0, 0))
        self.assertEqual(self.recipe.instructions[0], "Bake at 350 degrees F.")

    def test_exact_full_line_does_not_hide_other_substring_occurrences(self):
        with self.assertRaises(ValueError):
            self.modifier.apply_edit(edit("sugar", replace="honey"), ["sugar", "1 cup sugar"])

    def test_rejects_ambiguous_duplicate_lines_and_substrings(self):
        for lines in (["1 cup sugar", "1 cup sugar"], ["sugar and sugar"]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                self.modifier.apply_edit(edit("sugar", replace="honey"), lines)

    def test_rejects_overlapping_substring_matches(self):
        with self.assertRaises(ValueError):
            self.modifier.apply_edit(edit("aa", replace="b"), ["aaa"])

    def test_rejects_case_mismatch_and_fuzzy_only_match(self):
        for find in ("1 Cup sugar", "1 cup sugars", "missing"):
            with self.subTest(find=find), self.assertRaises(ValueError):
                self.modifier.apply_edit(edit(find, replace="half cup sugar"), self.recipe.ingredients)

    def test_rejects_noop_and_missing_payloads(self):
        for invalid in (edit("sugar", replace="sugar"), edit("sugar", replace=""),
                        edit("sugar"), edit("sugar", operation="add_after"),
                        edit("", replace="sugar"), edit("sugar", operation="add_after", add=" ")):
            with self.subTest(edit=invalid), self.assertRaises(ValueError):
                self.modifier.apply_edit(invalid, self.recipe.ingredients)

    def test_remove_requires_entire_line(self):
        with self.assertRaises(ValueError):
            self.modifier.apply_edit(edit("walnuts", operation="remove"), self.recipe.ingredients)
        changed, records = self.modifier.apply_edit(edit("1 cup walnuts", operation="remove"), self.recipe.ingredients)
        self.assertEqual(changed, ["1 cup sugar", "2 eggs"])
        self.assertEqual((records[0].before_index, records[0].after_index), (2, None))

    def test_failed_later_edit_rolls_back_modification(self):
        original = self.recipe.model_dump()
        with self.assertRaises(ValueError):
            self.modifier.apply_modification(self.recipe, modification(
                edit("1 cup sugar", replace="half cup sugar"), edit("absent", replace="honey")))
        self.assertEqual(self.recipe.model_dump(), original)

    def test_multiple_edits_record_intermediate_indexes(self):
        changed, records = self.modifier.apply_modification(self.recipe, modification(
            edit("1 cup sugar", operation="remove"),
            edit("2 eggs", operation="add_after", add="1 egg yolk"),
            edit("1 cup walnuts", replace="half cup walnuts")))
        self.assertEqual(changed.ingredients, ["2 eggs", "1 egg yolk", "half cup walnuts"])
        self.assertEqual([(r.before_index, r.after_index) for r in records],
                         [(0, None), (None, 1), (2, 2)])

    def test_append_supports_empty_sections_and_preserves_input(self):
        content = []
        changed, records = self.modifier.apply_edit(edit("", operation="add_after", add="1 egg"), content)
        self.assertEqual(content, [])
        self.assertEqual(changed, ["1 egg"])
        self.assertEqual(records[0].after_index, 0)

    def test_nonempty_add_anchor_must_exist_uniquely(self):
        for lines in ([], ["sugar", "sugar"]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                self.modifier.apply_edit(edit("sugar", operation="add_after", add="egg"), lines)

    def test_rejects_empty_modification_and_empty_batch(self):
        with self.assertRaises(ValueError):
            self.modifier.apply_modification(self.recipe, modification())
        with self.assertRaises(ValueError):
            self.modifier.apply_modifications_batch(self.recipe, [])

    def test_cannot_delete_last_original_section_line(self):
        recipe = self.recipe.model_copy(deep=True)
        recipe.instructions = ["Bake."]
        with self.assertRaises(ValueError):
            self.modifier.apply_modification(recipe, modification(edit("Bake.", operation="remove", target="instructions")))
        self.assertEqual(recipe.instructions, ["Bake."])

    def test_failed_batch_never_mutates_original_recipe(self):
        original = self.recipe.model_dump()
        with self.assertRaises(ValueError):
            self.modifier.apply_modifications_batch(self.recipe, [
                modification(edit("1 cup sugar", replace="half cup sugar")),
                modification(edit("missing", replace="honey"))])
        self.assertEqual(self.recipe.model_dump(), original)

    def test_batch_returns_real_records_grouped_by_modification(self):
        changed, groups = self.modifier.apply_modifications_batch(self.recipe, [
            modification(edit("1 cup sugar", replace="half cup sugar")),
            modification(edit("2 eggs", replace="3 eggs"))])
        self.assertEqual(changed.ingredients[:2], ["half cup sugar", "3 eggs"])
        self.assertEqual([len(group) for group in groups], [1, 1])
        self.assertEqual(changed.recipe_id, "sample_modified")

    def test_safety_check_uses_transactional_semantics(self):
        good = modification(edit("2 eggs", replace="3 eggs"))
        self.assertEqual(self.modifier.validate_modification_safety(good, self.recipe), (True, []))
        safe, warnings = self.modifier.validate_modification_safety(modification(edit("missing", replace="3 eggs")), self.recipe)
        self.assertFalse(safe)
        self.assertTrue(warnings)

    def test_batch_rejects_reversed_change_even_if_another_change_survives(self):
        original = self.recipe.model_dump()
        with self.assertRaisesRegex(ValueError, "reverses"):
            self.modifier.apply_modifications_batch(self.recipe, [
                modification(edit("1 cup sugar", replace="half cup sugar")),
                modification(edit("2 eggs", replace="3 eggs")),
                modification(edit("half cup sugar", replace="1 cup sugar")),
            ])
        self.assertEqual(self.recipe.model_dump(), original)


if __name__ == "__main__":
    unittest.main()
