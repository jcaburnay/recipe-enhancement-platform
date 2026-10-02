"""
Step 1: Tweak Extraction & Parsing

This module extracts structured modifications from review text using LLM processing.
It converts natural language descriptions of recipe changes into structured
ModificationObject instances.
"""

import json
import os
from typing import Optional

from loguru import logger
from openai import BadRequestError, NotFoundError, OpenAI
from pydantic import ValidationError

from .models import ModificationObject, Recipe, Review
from .prompts import build_simple_prompt


def build_modification_response_format() -> dict:
    """Derive a strict provider schema while preserving local model defaults."""
    schema = ModificationObject.model_json_schema()

    def make_strict(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "recipe_modification", "strict": True, "schema": schema},
    }


class TweakExtractor:
    """Extracts structured modifications from review text using LLM processing."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        """
        Initialize the TweakExtractor.

        Args:
            api_key: Provider API key (defaults to environment configuration)
            model: Model to use for extraction (defaults to LLM_MODEL or provider default)
        """
        use_openrouter = bool(os.getenv("OPENROUTER_API_KEY"))
        self.use_openrouter = use_openrouter
        reasoning_enabled = os.getenv("LLM_REASONING_ENABLED")
        self.reasoning_enabled = None
        if reasoning_enabled is not None:
            if reasoning_enabled.lower() not in ("true", "false"):
                raise ValueError("LLM_REASONING_ENABLED must be true or false")
            self.reasoning_enabled = reasoning_enabled.lower() == "true"
        provider_key = os.getenv("OPENROUTER_API_KEY" if use_openrouter else "OPENAI_API_KEY")
        self.client = OpenAI(
            api_key=api_key or provider_key,
            base_url="https://openrouter.ai/api/v1" if use_openrouter else None,
            timeout=60.0,
            max_retries=0,  # Extraction below already handles retries.
        )
        self.model = model or os.getenv("LLM_MODEL") or (
            "openrouter/free" if use_openrouter else "gpt-4o-mini"
        )
        logger.info(f"Initialized TweakExtractor with model: {self.model}")

    def extract_modification(
        self,
        review: Review,
        recipe: Recipe,
        max_retries: int = 2,
    ) -> Optional[ModificationObject]:
        """
        Extract a structured modification from a review.

        Args:
            review: Review object containing modification text
            recipe: Original recipe being modified
            max_retries: Number of retry attempts if parsing fails

        Returns:
            ModificationObject if extraction successful, None otherwise
        """
        if not review.has_modification:
            logger.warning("Review has no modification flag set")
            return None

        # Build the prompt - use simple prompt to avoid format string issues
        prompt = build_simple_prompt(
            review.text, recipe.title, recipe.ingredients, recipe.instructions
        )
        prompt += (
            "\nReturn one modification object, without a modifications wrapper. "
            "Combine this review's edits into its edits list and choose the main "
            "modification_type. Include both replace and add on every edit, using "
            "null for fields that do not apply."
        )

        logger.debug(
            "Extracting modification from review: {}...".format(review.text[:100])
        )

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format=build_modification_response_format(),
                    temperature=0.1,  # Low temperature for consistent extractions
                    max_tokens=4096,
                    **({"extra_body": {
                        "provider": {"require_parameters": True},
                        **({"reasoning": {"enabled": self.reasoning_enabled}}
                           if self.reasoning_enabled is not None else {}),
                    }}
                       if self.use_openrouter else {}),
                )

                choice = response.choices[0]
                usage = getattr(response, "usage", None)
                if usage is not None:
                    details = getattr(usage, "completion_tokens_details", None)
                    logger.info(
                        "Token usage: prompt={}, completion={}, reasoning={}",
                        usage.prompt_tokens, usage.completion_tokens,
                        getattr(details, "reasoning_tokens", None),
                    )
                if choice.finish_reason == "length":
                    logger.warning(
                        "Output token limit reached; discarding truncated response. "
                        "Adjust reasoning or the output budget before retrying."
                    )
                    return None
                if choice.message.refusal:
                    logger.warning("Model refused recipe modification extraction")
                    return None
                raw_output = choice.message.content
                logger.debug(f"LLM raw output: {raw_output}")

                # Check if we got a response
                if not raw_output:
                    logger.warning(
                        f"Attempt {attempt + 1}: Empty response from LLM "
                        f"(finish reason: {response.choices[0].finish_reason})"
                    )
                    continue

                # Parse and validate the JSON response
                modification_data = json.loads(raw_output)
                modification = ModificationObject(**modification_data)

                logger.info(
                    f"Successfully extracted {modification.modification_type} "
                    f"modification with {len(modification.edits)} edits"
                )
                return modification

            except (BadRequestError, NotFoundError) as e:
                logger.error(
                    f"Structured-output request rejected: {e}. "
                    "Check the schema and select a model/provider supporting json_schema."
                )
                return None

            except json.JSONDecodeError as e:
                logger.warning(f"Attempt {attempt + 1}: Failed to parse JSON: {e}")
                if attempt == max_retries:
                    logger.error(f"Max retries reached. Raw output: {raw_output}")

            except ValidationError as e:
                logger.warning(f"Attempt {attempt + 1}: Validation error: {e}")
                if attempt == max_retries:
                    logger.error(
                        f"Max retries reached. Invalid data: {modification_data}"
                    )

            except Exception as e:
                logger.error(f"Attempt {attempt + 1}: Unexpected error: {e}")
                if attempt == max_retries:
                    return None

        return None

    def extract_single_modification(
        self, reviews: list[Review], recipe: Recipe
    ) -> tuple[ModificationObject, Review] | tuple[None, None]:
        """
        Extract modification from a single randomly selected review.

        Args:
            reviews: List of reviews to choose from
            recipe: Original recipe being modified

        Returns:
            Tuple of (ModificationObject, source_Review) if successful, (None, None) otherwise
        """
        import random

        # Filter to reviews with modifications
        modification_reviews = [r for r in reviews if r.has_modification]

        if not modification_reviews:
            logger.warning("No reviews with modifications found")
            return None, None

        # Select one random review
        selected_review = random.choice(modification_reviews)
        logger.info(f"Selected review: {selected_review.text[:100]}...")

        modification = self.extract_modification(selected_review, recipe)
        if modification:
            logger.info("Successfully extracted modification from selected review")
            return modification, selected_review
        else:
            logger.warning("Failed to extract modification from selected review")
            return None, None

    def test_extraction(
        self, review_text: str, recipe_data: dict
    ) -> Optional[ModificationObject]:
        """
        Test extraction with raw text and recipe data.

        Args:
            review_text: Raw review text
            recipe_data: Raw recipe dictionary

        Returns:
            ModificationObject if successful
        """
        review = Review(text=review_text, has_modification=True)
        recipe = Recipe(
            recipe_id=recipe_data.get("recipe_id", "test"),
            title=recipe_data.get("title", "Test Recipe"),
            ingredients=recipe_data.get("ingredients", []),
            instructions=recipe_data.get("instructions", []),
        )

        return self.extract_modification(review, recipe)
