## Summary

<!-- What changes, and why. Link the issue if there is one. -->

## Test plan

<!-- How you verified it. Commands run, cases covered, what you did NOT test. -->

- [ ] `uv run pytest` passes (zero network / zero keys; LLM mocked)
- [ ] `uv run ruff check trove tests` passes
- [ ] Docs anchors checked if code cited by `docs/` moved
      (`uv run python scripts/check_page_anchors.py --at HEAD`)

## Checklist

- [ ] No secrets, local configs, or evaluation gold SQL included
- [ ] Automatic KB/memory content still lands as `pending` (no auto-inject path)
- [ ] No changes under `.github/workflows/`
- [ ] Docs updated where behavior changed
