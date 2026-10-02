"""
Step 3: Enhanced Recipe Generation with Attribution

This module generates enhanced recipes with full citation tracking.
It combines modified recipes with attribution information to create
comprehensive enhanced recipe objects.
"""

from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from loguru import logger

from .models import (
    ChangeRecord,
    EnhancedRecipe,
    EnhancementSummary,
    ModificationApplied,
    ModificationObject,
    Recipe,
    Review,
    ReviewStatement,
    SourceReview,
)


class EnhancedRecipeGenerator:
    """Generates enhanced recipes with full citation tracking and attribution."""

    def __init__(self, pipeline_version: str = "1.0.0"):
        """
        Initialize the Enhanced Recipe Generator.

        Args:
            pipeline_version: Version identifier for the pipeline
        """
        self.pipeline_version = pipeline_version
        logger.info(f"Initialized EnhancedRecipeGenerator v{pipeline_version}")

    def create_source_review(self, review: Review) -> SourceReview:
        """
        Convert a Review object to a SourceReview for attribution.

        Args:
            review: Original review object

        Returns:
            SourceReview with attribution information
        """
        return SourceReview(
            text=review.text, reviewer=review.username, rating=review.rating,
            is_featured=review.is_featured, helpful_votes=review.helpful_votes,
        )

    def create_modification_applied(
        self,
        modification: ModificationObject,
        source_review: Review,
        change_records: List[ChangeRecord],
    ) -> ModificationApplied:
        """
        Create a ModificationApplied record for attribution.

        Args:
            modification: Original modification object
            source_review: Review that suggested this modification
            change_records: List of changes that were actually made

        Returns:
            ModificationApplied with full attribution
        """
        return ModificationApplied(
            source_review=self.create_source_review(source_review),
            modification_type=modification.modification_type,
            reasoning=modification.reasoning,
            evidence_quote=modification.evidence_quote,
            intent=modification.intent,
            changes_made=change_records,
        )

    def calculate_enhancement_summary(
        self, modifications_applied: List[ModificationApplied]
    ) -> EnhancementSummary:
        """
        Calculate summary statistics for all applied modifications.

        Args:
            modifications_applied: List of all modifications applied

        Returns:
            EnhancementSummary with aggregate statistics
        """
        total_changes = sum(len(mod.changes_made) for mod in modifications_applied)
        change_types = sorted(set(mod.modification_type for mod in modifications_applied))

        # Generate expected impact summary
        impact_descriptions = []
        for mod in modifications_applied:
            if mod.reasoning:
                impact_descriptions.append(mod.reasoning)

        expected_impact = "; ".join(impact_descriptions[:3])  # Limit to top 3
        if len(impact_descriptions) > 3:
            expected_impact += (
                f" (and {len(impact_descriptions) - 3} more improvements)"
            )

        return EnhancementSummary(
            total_changes=total_changes,
            change_types=change_types,
            expected_impact=expected_impact
            or "Community-validated recipe improvements",
        )

    def generate_enhanced_recipe(
        self,
        original_recipe: Recipe,
        modified_recipe: Recipe,
        modifications: List[ModificationObject],
        source_review: Review,
        change_records: List[List[ChangeRecord]],
        excluded_changes: Optional[List[str]] = None,
        statements: Optional[List[ReviewStatement]] = None,
    ) -> EnhancedRecipe:
        """
        Generate a complete enhanced recipe with attribution.

        Args:
            original_recipe: Original unmodified recipe
            modified_recipe: Recipe with modifications applied
            modifications: Discrete modifications that were applied
            source_review: Review that suggested the modification
            change_records: Changes made for the modification

        Returns:
            Complete EnhancedRecipe with attribution
        """
        logger.info(f"Generating enhanced recipe for: {original_recipe.title}")

        # Create modification applied record
        if len(modifications) != len(change_records) or not modifications:
            raise ValueError("Each modification must have its own change records")
        if any(not records for records in change_records):
            raise ValueError("Cannot publish a modification with no applied edits")
        modifications_applied = [
            self.create_modification_applied(modification, source_review, records)
            for modification, records in zip(modifications, change_records)
        ]

        # Calculate enhancement summary
        enhancement_summary = self.calculate_enhancement_summary(modifications_applied)

        # Generate enhanced recipe ID and title
        enhanced_recipe_id = f"{original_recipe.recipe_id}_enhanced"
        enhanced_title = f"{original_recipe.title} (Community Enhanced)"

        # Create the enhanced recipe
        enhanced_recipe = EnhancedRecipe(
            recipe_id=enhanced_recipe_id,
            original_recipe_id=original_recipe.recipe_id,
            title=enhanced_title,
            ingredients=modified_recipe.ingredients,
            instructions=modified_recipe.instructions,
            modifications_applied=modifications_applied,
            excluded_changes=excluded_changes or [],
            statements=statements or [],
            enhancement_summary=enhancement_summary,
            line_diffs=self.generate_line_diffs(original_recipe, modified_recipe),
            description=original_recipe.description,
            servings=original_recipe.servings,
            prep_time=getattr(original_recipe, "prep_time", None),
            cook_time=getattr(original_recipe, "cook_time", None),
            total_time=getattr(original_recipe, "total_time", None),
            created_at=datetime.now().isoformat(),
            pipeline_version=self.pipeline_version,
        )

        logger.info(
            f"Generated enhanced recipe with {enhancement_summary.total_changes} changes "
            f"from {len(modifications_applied)} modifications"
        )

        return enhanced_recipe

    def generate_line_diffs(self, original_recipe: Recipe, modified_recipe: Recipe) -> List[ChangeRecord]:
        """Produce line differences with indexes in original and final content."""
        records = []
        for target, kind in (("ingredients", "ingredient"), ("instructions", "instruction")):
            before = getattr(original_recipe, target)
            after = getattr(modified_recipe, target)
            for tag, i1, i2, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                for offset in range(max(i2 - i1, j2 - j1)):
                    before_index = i1 + offset if i1 + offset < i2 else None
                    after_index = j1 + offset if j1 + offset < j2 else None
                    records.append(ChangeRecord(
                        type=kind,
                        from_text=before[before_index] if before_index is not None else "",
                        to_text=after[after_index] if after_index is not None else "",
                        operation="replace" if before_index is not None and after_index is not None else "remove" if before_index is not None else "add",
                        before_index=before_index, after_index=after_index,
                    ))
        return records

    def generate_comparison_data(
        self, original_recipe: Recipe, enhanced_recipe: EnhancedRecipe
    ) -> Dict[str, Any]:
        """
        Generate side-by-side comparison data for UI display.

        Args:
            original_recipe: Original recipe
            enhanced_recipe: Enhanced recipe

        Returns:
            Dictionary with comparison data
        """
        comparison = {
            "original": {
                "title": original_recipe.title,
                "ingredients": original_recipe.ingredients,
                "instructions": original_recipe.instructions,
                "servings": original_recipe.servings,
            },
            "enhanced": {
                "title": enhanced_recipe.title,
                "ingredients": enhanced_recipe.ingredients,
                "instructions": enhanced_recipe.instructions,
                "servings": enhanced_recipe.servings,
            },
            "changes": {
                "total_modifications": len(enhanced_recipe.modifications_applied),
                "total_changes": enhanced_recipe.enhancement_summary.total_changes,
                "change_types": enhanced_recipe.enhancement_summary.change_types,
                "expected_impact": enhanced_recipe.enhancement_summary.expected_impact,
            },
            "citations": [
                {
                    "reviewer": mod.source_review.reviewer,
                    "rating": mod.source_review.rating,
                    "modification_type": mod.modification_type,
                    "reasoning": mod.reasoning,
                    "evidence_quote": mod.evidence_quote,
                    "changes": [
                        {
                            "type": change.type,
                            "from": change.from_text,
                            "to": change.to_text,
                            "operation": change.operation,
                        }
                        for change in mod.changes_made
                    ],
                }
                for mod in enhanced_recipe.modifications_applied
            ],
            "excluded_changes": enhanced_recipe.excluded_changes,
            "line_diffs": [record.model_dump() for record in enhanced_recipe.line_diffs],
        }

        return comparison

    def save_enhanced_recipe(
        self, enhanced_recipe: EnhancedRecipe, output_path: str
    ) -> str:
        """
        Save enhanced recipe to JSON file.

        Args:
            enhanced_recipe: Enhanced recipe to save
            output_path: Path to save the file

        Returns:
            Path to the saved file
        """
        import json
        import os

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Convert to dict and save
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(enhanced_recipe.model_dump(), f, indent=2, ensure_ascii=False)

        logger.info(f"Saved enhanced recipe to: {output_path}")
        return output_path
