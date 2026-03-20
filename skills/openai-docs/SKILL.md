# OpenAI Docs

Use this skill when the user asks how to build with OpenAI products or APIs and the answer needs current official guidance.

## Workflow

1. Prefer current official OpenAI docs over bundled notes.
2. Use only official OpenAI sources for online lookup. In OpenCompany this means the available browsing/search tools should stay on official OpenAI domains.
3. Load bundled notes from `resources/references/` only when they are directly relevant, then re-check volatile guidance against current official docs before answering.
4. Answer concisely and cite the official source you relied on.

## Reference map

- `resources/references/latest-model.md`
  Use for model-selection questions, then verify against current docs.
- `resources/references/upgrading-to-gpt-5p4.md`
  Use for explicit GPT-5.4 upgrade planning.
- `resources/references/gpt-5p4-prompting-guide.md`
  Use when the user needs prompt rewrites or GPT-5.4 prompting adjustments.

## Rules

- Treat OpenAI docs as the source of truth.
- Keep quotes short; prefer paraphrase with citations.
- If bundled notes and current docs differ, say so and follow current docs.
- If official docs do not cover the user's need, state that clearly and give the next best official pointer.
