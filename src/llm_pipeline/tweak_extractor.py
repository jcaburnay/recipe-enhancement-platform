"""
Step 1: Tweak Extraction & Parsing

This module extracts structured modifications from review text using LLM processing.
It converts natural language descriptions of recipe changes into structured
ExtractionResult instances preserving every discrete change.
"""

import json
import os
from typing import Optional

from loguru import logger
from openai import BadRequestError, NotFoundError, OpenAI, RateLimitError
from pydantic import ValidationError

from .models import ExtractionResult, Recipe, Review
from .prompts import build_simple_prompt
from .recipe_modifier import RecipeModifier


def build_modification_response_format() -> dict:
    """Derive a strict provider schema while preserving local model defaults."""
    schema = ExtractionResult.model_json_schema()

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
        "json_schema": {"name": "recipe_review_modifications", "strict": True, "schema": schema},
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
        self.provider = os.getenv("LLM_PROVIDER") or (
            "openrouter" if os.getenv("OPENROUTER_API_KEY") else
            "gemini" if os.getenv("GEMINI_API_KEY") else "openai"
        )
        providers = {
            "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openrouter/free"),
            "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.8-flash"),
            "openai": ("OPENAI_API_KEY", None, "gpt-4o-mini"),
        }
        if self.provider not in providers:
            raise ValueError("LLM_PROVIDER must be openrouter, gemini, or openai")
        key_name, base_url, default_model = providers[self.provider]
        self.use_openrouter = self.provider == "openrouter"
        reasoning_enabled = os.getenv("LLM_REASONING_ENABLED")
        self.reasoning_enabled = None
        if reasoning_enabled is not None:
            if reasoning_enabled.lower() not in ("true", "false"):
                raise ValueError("LLM_REASONING_ENABLED must be true or false")
            self.reasoning_enabled = reasoning_enabled.lower() == "true"
        provider_key = os.getenv(key_name)
        self.client = OpenAI(
            api_key=api_key or provider_key,
            base_url=base_url,
            timeout=60.0,
            max_retries=0,  # Extraction below already handles retries.
        )
        self.model = model or os.getenv("LLM_MODEL") or default_model
        logger.info(f"Initialized TweakExtractor with model: {self.model}")

    def extract_modifications(
        self,
        review: Review,
        recipe: Recipe,
        max_retries: int = 2,
    ) -> Optional[ExtractionResult]:
        """
        Extract every discrete modification from a review.

        Args:
            review: Review object containing modification text
            recipe: Original recipe being modified
            max_retries: Number of retry attempts if parsing fails

        Returns:
            ExtractionResult if extraction successful, None otherwise
        """
        prompt = build_simple_prompt(review.text, recipe.title, recipe.ingredients, recipe.instructions)

        logger.debug(
            "Extracting modification from review: {}...".format(review.text[:100])
        )

        messages = [{"role": "user", "content": prompt}]
        for attempt in range(max_retries + 1):
            raw_output = None
            modification_data = None
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format=build_modification_response_format(),
                    temperature=0.1,  # Low temperature for consistent extractions
                    max_tokens=4096,
                    **({"extra_body": {
                        "provider": {"require_parameters": True},
                        **({"reasoning": {"enabled": self.reasoning_enabled}}
                           if self.reasoning_enabled is not None else {}),
                    }}
                       if self.use_openrouter else {}),
                    **({"reasoning_effort": "none"}
                       if self.provider == "gemini" and self.reasoning_enabled is False
                       and self.model.startswith("gemini-2.5-") and "pro" not in self.model
                       else {}),
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
                result = ExtractionResult.model_validate(modification_data)
                for statement in result.statements:
                    if not statement.evidence_quote.strip() or statement.evidence_quote not in review.text:
                        raise ValueError("Statement classification requires exact nonempty source evidence")
                    if statement.classification != "performed_change":
                        if any(statement.evidence_quote in note for note in result.unresolved + result.excluded_changes):
                            raise ValueError("Future plans, suggestions and compliments must not be unresolved or excluded")
                for modification in result.modifications:
                    if result.statements and not any(
                        statement.classification == "performed_change" and
                        (modification.evidence_quote in statement.evidence_quote or
                         statement.evidence_quote in modification.evidence_quote)
                        for statement in result.statements
                    ):
                        raise ValueError("A modification requires a performed_change classification")
                    intent = modification.intent
                    if result.statements and intent is None:
                        raise ValueError("Classified ingredient changes require populated intent")
                    if intent is not None:
                        if not intent.ingredient.strip():
                            raise ValueError("Ingredient intent requires a named ingredient")
                        if intent.application is not None and (
                            not intent.application_evidence or intent.application_evidence not in review.text
                        ):
                            raise ValueError("Application requires exact supporting review evidence")
                        if intent.application is None and intent.application_evidence is not None:
                            raise ValueError("Unknown application must have application_evidence=null")
                        if (modification.modification_type == "addition" and intent.application is not None
                            and recipe.instructions and not any(edit.target == "instructions" for edit in modification.edits)):
                            raise ValueError("A review-supported addition application requires a corresponding instruction edit")
                        if modification.modification_type == "addition" and intent.application is None:
                            if any(edit.target == "instructions" for edit in modification.edits):
                                raise ValueError("An addition with unknown application must not invent instruction edits")
                            if "application unspecified" not in intent.uncertainties:
                                raise ValueError("Unknown addition application must be recorded in uncertainties")
                        if intent.amount is not None and "unspecified" in intent.amount.lower():
                            raise ValueError("An unspecified amount must be amount=null with 'amount unspecified' uncertainty")
                        if intent.amount is None and modification.modification_type != "removal" and "amount unspecified" not in intent.uncertainties:
                            raise ValueError("Missing amount must be recorded as 'amount unspecified' in uncertainties")
                        if intent.amount is not None and "amount unspecified" in intent.uncertainties:
                            raise ValueError("A known amount must not also be marked unspecified")
                    if not modification.evidence_quote.strip() or modification.evidence_quote not in review.text:
                        raise ValueError("Each modification requires an exact nonempty review excerpt")
                    if not modification.edits:
                        raise ValueError("Each actionable modification requires edits")
                    if not any(edit.target == "ingredients" for edit in modification.edits):
                        raise ValueError("Each modification must change ingredients; technique-only changes belong in excluded_changes")

                # Dry-run the exact same ordered edits used by the pipeline on a copy.
                # This validates applicability, not semantic faithfulness to the review.
                if result.modifications:
                    RecipeModifier().apply_modifications_batch(recipe, result.modifications)
                logger.info("Extracted {} modifications; {} unresolved", len(result.modifications), len(result.unresolved))
                return result

            except RateLimitError as e:
                logger.error("Provider rate limit or quota exhausted; stopping immediate retries: {}", e)
                return None

            except (BadRequestError, NotFoundError) as e:
                logger.error(
                    f"Structured-output request rejected: {e}. "
                    "Check the schema and select a model/provider supporting json_schema."
                )
                return None

            except json.JSONDecodeError as e:
                logger.warning(f"Attempt {attempt + 1}: Failed to parse JSON: {e}")
                messages = messages + [{"role": "assistant", "content": raw_output or ""},
                    {"role": "user", "content": f"Invalid JSON: {e}. Return a complete corrected ExtractionResult JSON."}]
                if attempt == max_retries:
                    logger.error(f"Max retries reached. Raw output: {raw_output}")

            except (ValidationError, ValueError) as e:
                logger.warning(f"Attempt {attempt + 1}: Validation error: {e}")
                messages = messages + [{"role": "assistant", "content": raw_output or ""},
                    {"role": "user", "content":
                     f"Validation failed: {e}. Correct the complete extraction using the original source. "
                     "Check all ingredient changes, evidence, operation fields, and sequential anchors. "
                     "Preserve unspecified amounts; ignore future plans. Do not invent details to fix an edit. "
                     "If a supported change cannot be represented faithfully, put it in unresolved."}]
                if attempt == max_retries:
                    logger.error(
                        f"Max retries reached. Invalid data: {modification_data}"
                    )

            except Exception as e:
                logger.error(f"Attempt {attempt + 1}: Unexpected error: {e}")
                if attempt == max_retries:
                    return None

        return None

    @staticmethod
    def select_review(reviews: list[Review]) -> Optional[Review]:
        """Rank featured reviews by helpful votes; ties retain scraped order.

        Known counts rank above missing counts. Rating and regex flags do not rank.
        """
        featured = [r for r in reviews if r.is_featured]
        return max(featured, key=lambda r: (r.helpful_votes is not None,
                   r.helpful_votes if r.helpful_votes is not None else -1)) if featured else None

    def extract_review_modifications(self, reviews: list[Review], recipe: Recipe):
        selected = self.select_review(reviews)
        if selected is None:
            logger.warning("No featured reviews available")
            return None, None
        logger.info("Selected featured review: {}...", selected.text[:100])
        result = self.extract_modifications(selected, recipe)
        return (result, selected) if result is not None else (None, None)

    # Legacy names retain ALL modifications through the complete result contract.
    def extract_modification(self, review: Review, recipe: Recipe, max_retries: int = 2):
        return self.extract_modifications(review, recipe, max_retries)

    def extract_single_modification(self, reviews: list[Review], recipe: Recipe):
        return self.extract_review_modifications(reviews, recipe)

    def test_extraction(self, review_text: str, recipe_data: dict):
        return self.extract_modifications(Review(text=review_text, is_featured=True), Recipe.model_validate(recipe_data))
