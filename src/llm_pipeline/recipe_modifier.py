"""Apply exact recipe edits transactionally; never guess an intended match."""

from typing import List, Optional, Tuple

from .models import ChangeRecord, ModificationEdit, ModificationObject, Recipe


class RecipeModifier:
    """Apply textual edits, without claiming to validate cooking outcomes."""

    def __init__(self, similarity_threshold: float = 0.6):
        # Retained for callers using the old constructor; matching is always exact.
        self.similarity_threshold = similarity_threshold

    def _match_index(self, target: str, candidates: List[str], *, full_line: bool = False) -> int:
        if not target:
            raise ValueError("An edit requires nonempty find text")
        occurrences = [
            index
            for index, candidate in enumerate(candidates)
            for _ in range(
                int(candidate == target) if full_line else
                sum(candidate.startswith(target, offset) for offset in range(len(candidate)))
            )
        ]
        if len(occurrences) != 1:
            raise ValueError(f"Expected one exact match for {target!r}; found {len(occurrences)}")
        return occurrences[0]

    def find_best_match(self, target: str, candidates: List[str]) -> Tuple[Optional[str], Optional[int], float]:
        """Compatibility helper: return only a unique exact substring match."""
        try:
            index = self._match_index(target, candidates)
        except ValueError:
            return None, None, 0.0
        return candidates[index], index, 1.0

    def apply_edit(self, edit: ModificationEdit, recipe_content: List[str]) -> Tuple[List[str], List[ChangeRecord]]:
        if edit.operation == "replace" and edit.add is not None:
            raise ValueError("Replace requires add=null; use replace for replacement text")
        if edit.operation == "add_after" and edit.replace is not None:
            raise ValueError("Addition requires replace=null; use add for inserted text")
        if edit.operation == "remove" and (edit.add is not None or edit.replace is not None):
            raise ValueError("Remove requires add=null and replace=null")
        content = list(recipe_content)
        kind = "ingredient" if edit.target == "ingredients" else "instruction"
        if edit.operation == "replace":
            if edit.replace is None or not edit.replace.strip():
                raise ValueError("Replace requires nonempty replacement text")
            index = self._match_index(edit.find, content)
            original = content[index]
            replacement = original.replace(edit.find, edit.replace, 1)
            if replacement == original:
                raise ValueError("Replacement makes no change")
            content[index] = replacement
            record = ChangeRecord(type=kind, operation="replace", from_text=original,
                                  to_text=replacement, before_index=index, after_index=index)
        elif edit.operation == "add_after":
            if edit.add is None or not edit.add.strip():
                raise ValueError("Addition requires nonempty add text")
            index = self._match_index(edit.find, content) + 1 if edit.find else len(content)
            content.insert(index, edit.add)
            record = ChangeRecord(type=kind, operation="add", from_text="", to_text=edit.add,
                                  before_index=None, after_index=index)
        elif edit.operation == "remove":
            index = self._match_index(edit.find, content, full_line=True)
            original = content.pop(index)
            record = ChangeRecord(type=kind, operation="remove", from_text=original,
                                  to_text="", before_index=index, after_index=None)
        else:
            raise ValueError(f"Unknown edit operation: {edit.operation}")
        return content, [record]

    def apply_modification(self, recipe: Recipe, modification: ModificationObject) -> Tuple[Recipe, List[ChangeRecord]]:
        """Return a complete changed copy, or raise without modifying the input."""
        if not modification.edits:
            raise ValueError("A modification requires at least one edit")
        changed = recipe.model_copy(deep=True)
        records = []
        for edit in modification.edits:
            if edit.target not in ("ingredients", "instructions"):
                raise ValueError(f"Unknown edit target: {edit.target}")
            content, changes = self.apply_edit(edit, getattr(changed, edit.target))
            setattr(changed, edit.target, content)
            records.extend(changes)
        for section in ("ingredients", "instructions"):
            if getattr(recipe, section) and not getattr(changed, section):
                raise ValueError(f"Modification would empty the {section} section")
        changed.recipe_id = f"{recipe.recipe_id}_modified"
        return changed, records

    def apply_modifications_batch(self, recipe: Recipe, modifications: List[ModificationObject]) -> Tuple[Recipe, List[List[ChangeRecord]]]:
        if not modifications:
            raise ValueError("At least one modification is required")
        current = recipe.model_copy(deep=True)
        grouped = []
        previous = []
        for modification in modifications:
            current, records = self.apply_modification(current, modification)
            for record in records:
                if any(record.type == earlier.type and
                       record.from_text == earlier.to_text and
                       record.to_text == earlier.from_text for earlier in previous):
                    raise ValueError("An edit reverses an earlier change; conflicting modifications")
                previous.append(record)
            grouped.append(records)
        current.recipe_id = f"{recipe.recipe_id}_modified"
        return current, grouped

    def validate_modification_safety(self, modification: ModificationObject, recipe: Recipe) -> Tuple[bool, List[str]]:
        """Check textual applicability, not culinary or food safety."""
        try:
            self.apply_modification(recipe, modification)
        except ValueError as error:
            return False, [str(error)]
        return True, []
