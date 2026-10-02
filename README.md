# Recipe Enhancement Platform

Extracts community-tested changes from AllRecipes featured reviews and applies
verifiable textual edits with attribution and line-level differences. This is a
correctness prototype: structured JSON alone does not establish recipe quality.

See [assessment and results](docs/assessment.md) and the
[conversation transcript](codex/conversation.md).

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
For Gemini, set `GEMINI_API_KEY`, `LLM_PROVIDER=gemini`, and
`LLM_MODEL=gemini-3.8-flash`. It uses Google's compatible endpoint with the same
structured-output schema. `LLM_REASONING_ENABLED=false` disables thinking for
Gemini 2.5 Flash; Gemini 3 models use their default thinking settings. Set `LLM_PROVIDER` explicitly when keeping multiple keys;
otherwise OpenRouter takes precedence, followed by Gemini and OpenAI. Free models are subject
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

Original scraped recipes contain `featured_tweaks` and reviews with heuristic
`has_modification` flags. Selection uses featured designation rather than those
flags. Helpful votes rank reviews when supplied; the sample dataset has none, so
featured order is preserved. Star ratings are not treated as vote counts.

Output schema v2 includes one `modifications_applied` entry per discrete change,
with an exact `evidence_quote`, source review, and its associated edits. `line_diffs`
contains original-to-final line indexes; change-record indexes within each
modification refer to the intermediate edit state. Existing v1 sample files remain
historical examples until regenerated. Enhanced recipes include:

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

1. **Tweak Extraction**: Selects one featured review deterministically and extracts all discrete performed ingredient changes with necessary instruction updates. Ambiguous ingredient changes remain `unresolved`; technique-only changes are recorded in `excluded_changes` and do not block publication.
2. **Recipe Modification**: Applies exact unique edits transactionally. Missing, ambiguous, or unchanged replacements fail rather than silently creating partial results.
3. **Enhanced Recipe Generation**: Saves only fully applied, actionable results with per-modification attribution and original-to-final line differences.

Missing featured reviews and reviews without actual changes are skipped. Unresolved
changes, extraction errors, or unapplicable edits fail without writing a new output.
An existing output is not deleted when a later run fails; check the current run's
report and output timestamp. Batch reports distinguish successes, skips, and failures;
the `all` command exits unsuccessfully if any input fails. Reviews are not combined,
avoiding conflicting advice; arbitrary cooking or ingredient consistency is not
guaranteed and must be evaluated independently.

## Development

```bash
# Add dependencies
uv add <package_name>

# Run tests
uv run python -m unittest discover -s tests -q

# Run live single-recipe validation
uv run python src/test_pipeline.py single

```

Named additions and extra amounts may be retained as `(amount unspecified)` when
their application is clear; no quantity is invented. Unknown substitutions or unclear
application remain unresolved. Extraction dry-runs edits on a copy and sends validation
errors back to the model on retry. These checks verify edit applicability, not semantic
accuracy; inspect generated changes against source reviews.

Each applied modification retains `intent` with ingredient identity, amount,
review-supported application and explicit uncertainties. Null application on an
addition prevents invented instruction edits. Statement classifications distinguish
performed changes from ignored future plans, hypothetical suggestions and compliments.
Ingredient-only additions with unknown application are partial recipe guidance;
inspect their uncertainties before cooking. Legacy records may omit intent metadata.
