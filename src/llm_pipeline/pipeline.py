"""
LLM Analysis Pipeline - Main Orchestrator

This module coordinates the complete 3-step pipeline:
1. Extract modifications from reviews
2. Apply modifications to recipes
3. Generate enhanced recipes with attribution

Processes recipe data from scraped JSON files and outputs enhanced recipes.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from loguru import logger

from .enhanced_recipe_generator import EnhancedRecipeGenerator
from .models import EnhancedRecipe, Recipe, Review
from .recipe_modifier import RecipeModifier
from .tweak_extractor import TweakExtractor

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class LLMAnalysisPipeline:
    """Complete pipeline for analyzing recipes and generating enhanced versions."""

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        output_dir: Optional[str] = None,
        pipeline_version: str = "2.0.0",
    ):
        """
        Initialize the complete LLM Analysis Pipeline.

        Args:
            openai_api_key: OpenAI API key (loads from env if not provided)
            output_dir: Directory to save enhanced recipes (defaults to repository data/enhanced)
            pipeline_version: Version identifier for tracking
        """
        # Load environment variables
        load_dotenv()

        self.output_dir = Path(output_dir) if output_dir is not None else DATA_DIR / "enhanced"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.processing_results: List[Dict[str, Any]] = []

        # Initialize pipeline components
        self.tweak_extractor = TweakExtractor(api_key=openai_api_key)
        self.recipe_modifier = RecipeModifier()
        self.enhanced_generator = EnhancedRecipeGenerator(
            pipeline_version=pipeline_version
        )

        logger.info(f"Initialized LLM Analysis Pipeline v{pipeline_version}")
        logger.info(f"Output directory: {self.output_dir}")

    def load_recipe_data(self, file_path: str) -> Dict[str, Any]:
        """
        Load recipe data from JSON file.

        Args:
            file_path: Path to recipe JSON file

        Returns:
            Recipe data dictionary
        """
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def parse_recipe_data(self, recipe_data: Dict[str, Any]) -> Recipe:
        """
        Parse raw recipe data into Recipe object.

        Args:
            recipe_data: Raw recipe data from JSON

        Returns:
            Recipe object
        """
        return Recipe(
            recipe_id=recipe_data.get("recipe_id", "unknown"),
            title=recipe_data.get("title", "Unknown Recipe"),
            ingredients=recipe_data.get("ingredients", []),
            instructions=recipe_data.get("instructions", []),
            description=recipe_data.get("description"),
            servings=recipe_data.get("servings"),
            rating=recipe_data.get("rating"),
        )

    def parse_reviews_data(self, recipe_data: Dict[str, Any]) -> List[Review]:
        """
        Parse raw review data into Review objects.

        Args:
            recipe_data: Raw recipe data containing reviews

        Returns:
            List of Review objects
        """
        reviews = []
        raw_reviews = [
            {**review, "is_featured": True}
            for review in recipe_data.get("featured_tweaks", [])
        ] + recipe_data.get("reviews", [])
        by_text = {}

        for review_data in raw_reviews:
            if review_data.get("text"):
                if review_data["text"] in by_text:
                    existing = by_text[review_data["text"]]
                    existing.is_featured |= review_data.get("is_featured", False)
                    existing.has_modification |= review_data.get("has_modification", False)
                    votes = review_data.get("helpful_votes")
                    if votes is not None:
                        existing.helpful_votes = max(existing.helpful_votes or 0, int(votes))
                    continue
                review = Review(
                    text=review_data["text"],
                    rating=review_data.get("rating"),
                    username=review_data.get("username"),
                    has_modification=review_data.get("has_modification", False),
                    is_featured=review_data.get("is_featured", False),
                    helpful_votes=review_data.get("helpful_votes"),
                )
                reviews.append(review)
                by_text[review.text] = review

        return reviews

    def _record_result(self, recipe_file: str, status: str, reason: str, **details) -> None:
        self.processing_results.append({
            "recipe_file": recipe_file, "status": status, "reason": reason, **details,
        })

    def process_single_recipe(
        self, recipe_file: str, save_output: bool = True
    ) -> Optional[EnhancedRecipe]:
        """
        Process a single recipe through the complete pipeline.

        Args:
            recipe_file: Path to recipe JSON file
            save_output: Whether to save the enhanced recipe

        Returns:
            EnhancedRecipe if successful, None otherwise
        """
        try:
            logger.info(f"Processing recipe file: {recipe_file}")

            # Step 0: Load and parse data
            recipe_data = self.load_recipe_data(recipe_file)
            recipe = self.parse_recipe_data(recipe_data)
            reviews = self.parse_reviews_data(recipe_data)

            logger.info(f"Loaded recipe: {recipe.title}")
            logger.info(
                f"Found {len(reviews)} reviews, {len([r for r in reviews if r.has_modification])} with modifications"
            )

            if not any(r.is_featured for r in reviews):
                logger.warning("No featured tweaks found")
                self._record_result(recipe_file, "skipped", "no_featured_reviews")
                return None

            # Select one featured review without combining conflicting reviews.
            logger.info("Step 1: Extracting all discrete modifications from a featured review...")
            extraction, source_review = (
                self.tweak_extractor.extract_review_modifications(reviews, recipe)
            )

            if extraction is None or source_review is None:
                logger.warning("Extraction failed")
                self._record_result(recipe_file, "failed", "extraction_failed")
                return None
            if extraction.excluded_changes:
                logger.info("Excluded changes outside ingredient scope: {}", extraction.excluded_changes)
            if extraction.unresolved:
                logger.warning(f"Review needs clarification: {extraction.unresolved}")
                self._record_result(recipe_file, "failed", "unresolved_changes", unresolved=extraction.unresolved, excluded_changes=extraction.excluded_changes)
                return None
            if not extraction.modifications:
                self._record_result(recipe_file, "skipped", "no_actionable_changes", excluded_changes=extraction.excluded_changes)
                return None

            logger.info(
                f"Successfully extracted {len(extraction.modifications)} discrete modifications"
            )

            # Step 2: Apply modification to recipe
            logger.info("Step 2: Applying modification to recipe...")
            modified_recipe, change_records = self.recipe_modifier.apply_modifications_batch(
                recipe, extraction.modifications
            )

            logger.info(
                f"Applied modifications: {sum(len(records) for records in change_records)} total edits"
            )

            # Step 3: Generate enhanced recipe with attribution
            logger.info("Step 3: Generating enhanced recipe with attribution...")

            enhanced_recipe = self.enhanced_generator.generate_enhanced_recipe(
                recipe, modified_recipe, extraction.modifications, source_review, change_records, excluded_changes=extraction.excluded_changes, statements=extraction.statements
            )
            if not enhanced_recipe.line_diffs:
                raise ValueError("Modifications produced no net changes")

            logger.info(f"Generated enhanced recipe: {enhanced_recipe.title}")

            # Save output
            if save_output:
                slug = re.sub(r"[^a-z0-9]+", "-", recipe.title.lower()).strip("-")[:30]
                safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", recipe.recipe_id) or "unknown"
                output_filename = f"enhanced_{safe_id}_{slug}.json"
                output_path = self.output_dir / output_filename
                self.enhanced_generator.save_enhanced_recipe(
                    enhanced_recipe, str(output_path)
                )

            self._record_result(recipe_file, "success", "all_changes_applied", modifications=len(extraction.modifications), excluded_changes=extraction.excluded_changes)
            return enhanced_recipe

        except Exception as e:
            logger.error(f"Failed to process recipe {recipe_file}: {e}")
            self._record_result(recipe_file, "failed", str(e))
            import traceback

            traceback.print_exc()
            return None

    def process_recipe_directory(self, data_dir: Optional[str] = None) -> List[EnhancedRecipe]:
        """
        Process all recipe files in a directory.

        Args:
            data_dir: Directory containing recipe JSON files

        Returns:
            List of successfully processed EnhancedRecipe objects
        """
        data_path = Path(data_dir) if data_dir is not None else DATA_DIR
        recipe_files = sorted(data_path.glob("recipe_*.json"))
        self.processing_results = []

        logger.info(f"Found {len(recipe_files)} recipe files to process")

        enhanced_recipes = []
        for recipe_file in recipe_files:
            logger.info(f"\n{'=' * 60}")
            enhanced_recipe = self.process_single_recipe(str(recipe_file))

            if enhanced_recipe:
                enhanced_recipes.append(enhanced_recipe)
                logger.info(f"✓ Successfully processed: {enhanced_recipe.title}")
            else:
                result = self.processing_results[-1] if self.processing_results else {}
                status = result.get("status", "failed")
                reason = result.get("reason", "no_result")
                if status == "skipped":
                    logger.info(f"Skipped {recipe_file.name}: {reason}")
                else:
                    logger.warning(f"Failed {recipe_file.name}: {reason}")

        logger.info(f"\n{'=' * 60}")
        logger.info(
            "Pipeline complete: {} enhanced, {} skipped, {} failed ({} inputs)",
            len(enhanced_recipes),
            sum(result["status"] == "skipped" for result in self.processing_results),
            sum(result["status"] == "failed" for result in self.processing_results),
            len(recipe_files),
        )

        return enhanced_recipes

    def generate_summary_report(
        self, enhanced_recipes: List[EnhancedRecipe]
    ) -> Dict[str, Any]:
        """
        Generate a summary report of pipeline results.

        Args:
            enhanced_recipes: List of enhanced recipes

        Returns:
            Summary report dictionary
        """
        total_modifications = sum(
            len(recipe.modifications_applied) for recipe in enhanced_recipes
        )
        total_changes = sum(
            recipe.enhancement_summary.total_changes for recipe in enhanced_recipes
        )

        change_type_counts = {}
        for recipe in enhanced_recipes:
            for change_type in recipe.enhancement_summary.change_types:
                change_type_counts[change_type] = (
                    change_type_counts.get(change_type, 0) + 1
                )

        report = {
            "pipeline_summary": {
                "inputs_processed": len(self.processing_results),
                "failed": sum(result["status"] == "failed" for result in self.processing_results),
                "skipped": sum(result["status"] == "skipped" for result in self.processing_results),
                "recipes_processed": len(enhanced_recipes),
                "total_modifications_applied": total_modifications,
                "total_changes_made": total_changes,
                "change_type_distribution": change_type_counts,
            },
            "processing_results": self.processing_results,
            "enhanced_recipes": [
                {
                    "recipe_id": recipe.recipe_id,
                    "title": recipe.title,
                    "modifications_count": len(recipe.modifications_applied),
                    "changes_count": recipe.enhancement_summary.total_changes,
                    "change_types": recipe.enhancement_summary.change_types,
                }
                for recipe in enhanced_recipes
            ],
        }

        return report

    def save_summary_report(
        self, enhanced_recipes: List[EnhancedRecipe], output_path: Optional[str] = None
    ) -> str:
        """
        Save pipeline summary report to JSON file.

        Args:
            enhanced_recipes: List of enhanced recipes
            output_path: Path to save report (auto-generated if None)

        Returns:
            Path to saved report
        """
        if output_path is None:
            output_path = str(self.output_dir / "pipeline_summary_report.json")

        report = self.generate_summary_report(enhanced_recipes)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved pipeline summary report to: {output_path}")
        return output_path
