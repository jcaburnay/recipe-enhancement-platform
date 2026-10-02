# Casper Studios assessment

## Goal and assumptions

Improve the existing pipeline so community tweaks become traceable recipe changes.
Prioritize extraction and edit reliability within the assignment's limited time budget;
UI, deployment and a separate evaluation framework are outside this iteration.

- Support ingredient substitutions, quantity changes, additions and removals, with
  necessary instruction updates. Technique-only changes are recorded in
  `excluded_changes`. This deliberately narrows the assignment's broader scope.
- Select one featured review per recipe to avoid conflicting advice. Rank by helpful
  votes when available; otherwise preserve scraped order. Star ratings are not votes.
- Apply changes actually performed. Ignore future plans, conditional suggestions and
  compliments. Community testimony supports attribution, not proof of improvement.
- Preserve approximate amounts and allow named additions with unspecified quantities.
  Record missing amount/application details explicitly; never invent them. An addition
  with unknown application can appear in ingredients without invented method steps,
  so the output may remain incomplete cooking guidance.
- Unknown ingredient identity, such as “different frosting,” remains `unresolved`
  and blocks publication. Direct substitutions retain original amounts unless changed
  by the reviewer. This is an assumption, not a verified culinary equivalence.

## Diagnosis and implementation

The original pipeline treated a review as one modification, used inconsistent review
selection and fragile matching, and could report edits without reliable attribution.
Running it exposed truncated responses, invalid edits, omitted changes and invented
quantities. Working-directory-dependent paths also created two output directories.

1. **Make API access work.** Added OpenRouter and direct Gemini support while retaining
   OpenAI. Provider/model configuration lives in environment variables. Free-model
   availability and quotas affected testing; the latest runs use Gemini 3.1 Flash-Lite.
   Kept a 4,096-token output budget and reject truncated responses.
2. **Strengthen extraction.** Use strict JSON-schema structured outputs derived from
   Pydantic. Return separate modifications—for example, adding an egg and halving
   sugar are two changes. Each includes exact review evidence and ingredient intent:
   identity, amount, application, application evidence and uncertainties. Classify
   source statements before proposing edits to separate performed changes from plans,
   suggestions and compliments.
3. **Validate and apply reliably.** Dry-run ordered edits on a copy. Require unique
   exact matches, correct operation fields and meaningful replacements. Apply the
   batch transactionally and reject direct reversals. Invalid responses receive the
   actual validation error on retry; rate-limit errors stop immediate retries.
4. **Keep outputs inspectable.** Save attribution per modification and original-to-final
   line differences. Retain intent, uncertainties and exclusions in enhanced JSON.
   Use the repository-root `data/enhanced/` path and distinguish enhanced, skipped
   and failed inputs in the batch report.
5. **Test the code and inspect the AI.** Added offline extraction, edit and integration
   tests, including manually labeled synthetic cases. Compared live outputs against
   their selected source reviews and revised the contract around observed mistakes.

Structured outputs enforce response shape; exact evidence and edit checks improve
reliability but cannot prove complete extraction or semantic correctness. Python
applies the edits; the AI still interprets the review and proposes them.

## Results and limitations

**52 offline tests pass.** They use controlled responses without API calls and verify
code behavior, not live model accuracy. The latest live batch processed six inputs:
**2 enhanced, 2 skipped, 2 failed**.

| Recipe | Latest observed result |
| --- | --- |
| Cookies | Added one egg yolk and updated mixing instructions; scoop technique excluded. |
| Soup | Extracted four ingredient changes, updated yam references, retained half-and-half and added a cream drizzle. Vegetable application remains explicitly unknown. |
| Apple cake | Blocked because replacement frosting is unidentified; future apple addition ignored. |
| Nikujaga | Rejected a fabricated evidence excerpt after retries. An older enhanced file remains stale. |
| Jam and marinade | Skipped because there are no featured reviews. |

Failures should not be forced into successful outputs. Existing generated files survive
failed reruns, so consult the latest report and timestamps. Unknown application details,
model variability and retained cooking timings mean generated recipes still need review;
culinary suitability, particularly for canned-yam substitutions, was not established.

## Next improvements

Use independent held-out reviews to measure change completeness, quantity accuracy
and ingredient-to-instruction consistency. Tighten the link between intent and generated
edits, and distinguish complete recipes from outputs needing application clarification.
Helpful-vote scraping and resolving conflicting reviews can follow once extraction is
reliable across more examples.

## Run the checks

```sh
uv run python -m unittest discover -s tests -q  # offline tests
uv run python src/test_pipeline.py single     # live cookie check
uv run python src/test_pipeline.py all        # live batch; nonzero exit on failures
```

`codex/conversation.md` contains the exported user/assistant conversation for the
agent-trajectory deliverable.
