## What and why

<!-- What changes, and the problem it solves. Link issues and ADRs. -->

## Requirements

<!-- Requirement and gate ids this change implements or evidences, e.g. REQ09, G06. -->

## Evidence checklist

- [ ] New or changed modules carry `__requirements__`; new tests carry `@pytest.mark.req(...)`
- [ ] `pytest`, `ruff check src tests examples` and `mypy` pass
- [ ] `air schema check` passes. Contract changes bump the contract version: new optional
      field = minor; removed or retyped field = major. Run `air schema export` and commit
      the new schema file; never edit a published one.
- [ ] `air requirements matrix --check` passes; any claim change was made with
      `air requirements claim ... --by ...`
- [ ] Runtime paths that can command hardware still go through admission; nothing new can
      set VERIFIED without observations
- [ ] Non-obvious decisions have an ADR in `docs/decisions/`
- [ ] Deprecations use `@deprecated(since=..., removal=...)` and are in the changelog

## Agent-authored changes

<!-- If an AI agent wrote part of this change, say so. A human maintainer reviews it like any other change. -->
