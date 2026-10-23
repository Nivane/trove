# Contributing to Trove

Thanks for your interest. This guide covers the development loop, the test
contract, and a few repository rules that are load-bearing rather than stylistic.

## Development setup

Requirements: Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -m "not slow"    # quick pass
uv run pytest                  # full suite (~9000 tests, mocked LLM, zero network/keys)
```

The test suite is deliberately hermetic: **zero API keys, zero network access,
all LLM calls mocked**. A test that needs a live service must be marked
`integration` and skipped when its env var is absent (see `tests/conftest.py`).

Optional datasource extras: `uv sync --extra mysql|postgres|clickhouse|duckdb|snowflake|bigquery`.

## Before a pull request

```bash
uv run pytest
uv run ruff check trove tests
uv run python scripts/check_page_anchors.py --at HEAD   # if docs, or code cited by docs, moved
```

## Branches and commits

- Work on a feature branch and keep each change focused.
- Do not commit secrets, local configs, `.env` files, or design drafts.
- Commit messages: an imperative one-line summary, plus a body when the "why"
  is not obvious.
- Do not rewrite shared history. Feature branches are kept after merge.

## Rules that are not style preferences

- **KB anti-cheating.** Never paste evaluation gold SQL into
  `.trove/kb/<datasource>/examples.yml`. Content that `kb init` cannot generate
  deterministically is not an accuracy gain.
- **Automatic learning lands as `pending`.** Memory distillation and auto-drafted
  KB assets must not enter retrieval or prompts until an admin confirms. Do not
  add an auto-inject path — it protects the human-curated KB from pollution.
- **Evaluation runs cost money.** `scripts/eval_bird.py` calls billed LLMs. Do not
  run it in CI or as part of a casual test loop.

## Documentation

The docs site is zero-build HTML under `docs/`. Pages cite source lines
(`file.py:123`). When code moves, run the anchor checker: `--write` re-anchors
drifted line numbers, while dead links and unresolved anchors need a human.

## Security

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Do not open a public issue containing credentials, customer data, or a working
exploit.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
