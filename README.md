# Recipe Enhancement Platform

Automatically enhances recipes by analyzing and applying community-tested modifications from AllRecipes.com. Uses LLM processing to extract meaningful recipe tweaks and apply them with full citation tracking.

## Installation

This project uses [`uv`](https://docs.astral.sh/uv/) for fast, reliable Python package management.

### Prerequisites

- Python 3.13+
- `uv` package manager

## Setup

```bash
# Install dependencies
uv sync --locked
```

### Environment Variables

Create a `.env` file in the project root:

```env
OPENROUTER_API_KEY=your-openrouter-api-key-here
# Optional: choose a specific model instead of the free router
LLM_MODEL=openrouter/free
# For models with optional reasoning, disable it for this extraction task:
# LLM_REASONING_ENABLED=false
```

OpenRouter uses `https://openrouter.ai/api/v1` and defaults to the free model router.
Alternatively, set `OPENAI_API_KEY` to use OpenAI (default model: `gpt-4o-mini`).
When both keys are present, OpenRouter takes precedence. Free models are subject
to availability and rate limits. The commands below make live API calls.

Extraction uses strict JSON-schema structured outputs derived from the Pydantic
models, with a 4,096-token output budget. Choose a model that supports structured
outputs; OpenRouter routing requires compatible endpoints. Responses are still
validated locally, and truncated responses are discarded before parsing.
Token usage is logged without reasoning text. Token-limit failures stop immediately
instead of repeating the same request. `LLM_REASONING_ENABLED=false` is supported
only by endpoints where reasoning is optional; leave it unset for mandatory
reasoning models.

## Usage

### 1. Scrape Recipes (Optional - data already provided)

```bash
uv run python src/scraper_v2.py
```

### 2. Run Recipe Enhancement Pipeline

```bash
# Test single recipe (chocolate chip cookies)
uv run python src/test_pipeline.py single

# Process all recipes
uv run python src/test_pipeline.py all
```

## Output

### Enhanced Recipes

Enhanced recipes are saved in the repository root's `data/enhanced/`, regardless
of the working directory:

- `enhanced_[recipe_id]_[recipe-name].json` - Individual enhanced recipes with modifications applied
- `pipeline_summary_report.json` - Summary of all processing results

### Data Structure

Original scraped recipes in `data/` directory contain reviews with `has_modification: true` flags. Enhanced recipes include:

```json
{
  "recipe_id": "10813_enhanced",
  "title": "Best Chocolate Chip Cookies (Community Enhanced)",
  "ingredients": ["1 cup butter", "1 additional egg yolk", ...],
  "modifications_applied": [
    {
      "source_review": {
        "text": "I added an extra egg yolk for chewier texture",
        "rating": 5
      },
      "modification_type": "addition",
      "reasoning": "Improves texture and chewiness",
      "changes_made": [...]
    }
  ],
  "enhancement_summary": {
    "total_changes": 1,
    "change_types": ["addition"],
    "expected_impact": "Chewier texture and improved consistency"
  }
}
```

## How It Works

The LLM Analysis Pipeline processes recipes in 3 steps:

1. **Tweak Extraction**: Selects one random review with modifications and uses the configured LLM to extract structured changes
2. **Recipe Modification**: Applies changes to the original recipe using fuzzy string matching
3. **Enhanced Recipe Generation**: Creates enhanced version with full citation tracking back to source review

Each run produces one enhanced recipe per original recipe, with complete attribution showing exactly what changed and why.

## Development

```bash
# Add dependencies
uv add <package_name>

# Run tests
uv run python -m unittest discover -s tests -q

# Run live single-recipe validation
uv run python src/test_pipeline.py single
```
