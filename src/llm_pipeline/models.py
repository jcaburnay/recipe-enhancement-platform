"""
Pydantic data models for the LLM Analysis Pipeline.

These models define the structure for recipe modifications, enhanced recipes,
and all intermediate data formats used throughout the pipeline.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ModificationEdit(BaseModel):
    """Individual atomic edit operation for a recipe modification."""

    model_config = ConfigDict(extra="forbid")

    target: Literal["ingredients", "instructions"] = Field(
        description="Whether this edit applies to ingredients or instructions"
    )
    operation: Literal["replace", "add_after", "remove"] = Field(
        default="replace",
        description="Type of operation: replace text, add after target, or remove",
    )
    find: str = Field(description="Text to find in the recipe")
    replace: Optional[str] = Field(
        default=None, description="Replacement text (required for replace operations)"
    )
    add: Optional[str] = Field(
        default=None, description="Text to add (required for add_after operations)"
    )


class IngredientIntent(BaseModel):
    """Evidence-grounded ingredient details, independent of textual edits."""

    model_config = ConfigDict(extra="forbid")
    ingredient: str = Field(description="One ingredient identity, not a combined list")
    amount: Optional[str] = Field(description="Explicit or calculated amount; singular an egg means 1 egg; null when unspecified")
    application: Optional[str] = Field(description="How/when used, only when supported by review; null when unknown")
    application_evidence: Optional[str] = Field(description="Exact review excerpt supporting application, or null")
    uncertainties: List[str] = Field(description="Missing amount/application details retained visibly without guessing")


class ReviewStatement(BaseModel):
    """Classification of a review excerpt before edit generation."""

    model_config = ConfigDict(extra="forbid")
    evidence_quote: str
    classification: Literal["performed_change", "future_plan", "hypothetical_suggestion", "compliment"]


class ModificationObject(BaseModel):
    """Structured modification parsed from a review."""

    model_config = ConfigDict(extra="forbid")

    modification_type: Literal[
        "ingredient_substitution",
        "quantity_adjustment",
        "addition",
        "removal",
    ] = Field(description="Category of modification")

    reasoning: str = Field(description="Why this modification improves the recipe")
    evidence_quote: str = Field(default="", description="Exact review excerpt supporting this discrete modification")

    intent: Optional[IngredientIntent] = Field(default=None, description="Populate for every ingredient modification; null only for legacy data")
    edits: List[ModificationEdit] = Field(description="List of atomic edits to apply")


class ExtractionResult(BaseModel):
    """All actionable changes from one review, plus changes needing clarification."""

    model_config = ConfigDict(extra="forbid")
    statements: List[ReviewStatement] = Field(default_factory=list, description="Classify relevant performed changes, future plans, suggestions and compliments before proposing edits")
    modifications: List[ModificationObject]
    excluded_changes: List[str] = Field(default_factory=list, description="Performed technique-only changes outside ingredient scope; include exact review evidence and explain exclusion")
    unresolved: List[str] = Field(description="Actual changes that cannot be applied without inventing details; explain why")


class SourceReview(BaseModel):
    """Reference to the original review that suggested the modification."""

    text: str = Field(description="Full text of the original review")
    reviewer: Optional[str] = Field(description="Username of the reviewer")
    rating: Optional[int] = Field(description="Star rating given by reviewer")
    is_featured: bool = False
    helpful_votes: Optional[int] = None


class ChangeRecord(BaseModel):
    """Record of a specific change made to the recipe."""

    type: Literal["ingredient", "instruction"] = Field(
        description="Type of element that was changed"
    )
    from_text: str = Field(description="Original text before modification")
    to_text: str = Field(description="New text after modification")
    operation: Literal["replace", "add", "remove"] = Field(
        description="Type of operation performed"
    )
    before_index: Optional[int] = Field(default=None, description="Zero-based line index immediately before this edit")
    after_index: Optional[int] = Field(default=None, description="Zero-based line index immediately after this edit")


class ModificationApplied(BaseModel):
    """Full record of a modification that was applied to a recipe."""

    source_review: SourceReview = Field(
        description="Review that suggested this modification"
    )
    modification_type: str = Field(description="Category of modification")
    reasoning: str = Field(description="Why this modification was applied")
    evidence_quote: str = ""
    intent: Optional[IngredientIntent] = None
    changes_made: List[ChangeRecord] = Field(
        description="Detailed list of changes made"
    )


class EnhancementSummary(BaseModel):
    """Summary of all modifications applied to a recipe."""

    total_changes: int = Field(description="Total number of changes made")
    change_types: List[str] = Field(description="Types of modifications applied")
    expected_impact: str = Field(
        description="Expected improvement from these modifications"
    )


class EnhancedRecipe(BaseModel):
    """Recipe with community modifications applied and full attribution."""

    recipe_id: str = Field(description="Enhanced recipe ID")
    original_recipe_id: str = Field(description="ID of the original recipe")
    title: str = Field(description="Enhanced recipe title")

    # Enhanced recipe content
    ingredients: List[str] = Field(description="Modified ingredients list")
    instructions: List[str] = Field(description="Modified instructions list")

    # Attribution and tracking
    statements: List[ReviewStatement] = Field(default_factory=list, description="Source statement classifications, including ignored plans and suggestions")
    modifications_applied: List[ModificationApplied] = Field(
        description="Full record of all modifications applied"
    )
    enhancement_summary: EnhancementSummary = Field(
        description="Summary of all enhancements"
    )
    excluded_changes: List[str] = Field(default_factory=list, description="Review changes excluded from ingredient-only scope")
    line_diffs: List[ChangeRecord] = Field(default_factory=list, description="Original-to-final line differences; indexes refer to the original and final lists")

    # Optional metadata
    description: Optional[str] = Field(description="Enhanced recipe description")
    servings: Optional[str] = Field(description="Number of servings")
    prep_time: Optional[str] = Field(description="Preparation time")
    cook_time: Optional[str] = Field(description="Cooking time")
    total_time: Optional[str] = Field(description="Total time")

    # Generation metadata
    created_at: str = Field(description="When this enhanced recipe was created")
    pipeline_version: str = Field(
        default="1.0.0", description="Version of the pipeline that created this"
    )


class Recipe(BaseModel):
    """Base recipe model for input data."""

    recipe_id: str
    title: str
    ingredients: List[str]
    instructions: List[str]
    description: Optional[str] = None
    servings: Optional[str] = None
    rating: Optional[Dict[str, Any]] = None
    # Include other fields as needed


class Review(BaseModel):
    """Review model for input data."""

    text: str
    rating: Optional[int] = None
    username: Optional[str] = None
    has_modification: bool = False
    is_featured: bool = False
    helpful_votes: Optional[int] = None
