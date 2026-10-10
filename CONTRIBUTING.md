# Contributing

AssetBoard is pre-alpha and developed from the specifications in `docs/specs/`. Keep contributions
focused on one feature or fix, and raise unclear or conflicting requirements before implementation.

## Set up and check

Use Python 3.13+ and `uv`:

```bash
make setup
make dev
make check
```

`make check` runs formatting/lint checks, type checking, tests, and Alembic consistency checks. Tests
must not access the network. Add or update tests for behavior changes and update English and Polish
translation catalogs when user-facing strings change.

## Feature workflow

1. Link the change to a feature ID and its specification in `docs/specs/`.
2. Follow the repository workflow in `docs/specs/00-overview.md` §8: agree on a plan before
   implementation, then implement only the approved scope.
3. Trace each acceptance criterion to a test in the PR description. Record assumptions, deviations,
   and follow-ups.
4. Run `make check` before requesting review.

CI also builds and inspects a non-editable wheel and builds/smoke-tests the container, so packaged
assets and deployment behavior are checked in addition to the Python suite.

For local development, `make dev` accepts `DEV_HOST` and `DEV_PORT`; for example,
`make dev DEV_PORT=8080`.

Architecture, security, and domain rules are in `AGENTS.md` and the overview spec. A change to an
established technical decision should include an ADR under `docs/adr/`; see the
[ADR index](docs/adr/README.md).

## Commits and pull requests

Use Conventional Commit titles for PRs and squash commits, such as `feat:`, `fix:`, `refactor:`,
`test:`, `docs:`, and `chore:`. Release Please derives versions from these commits; mark breaking
changes with `!` after the type/scope or include a `BREAKING CHANGE:` footer. Keep commits and PRs
small and reviewable. Fill in the pull request template, including the acceptance-criteria-to-tests
mapping.

Contributions are licensed under AGPL-3.0-or-later, consistent with the project license.

## Dependencies and licensing

Justify new runtime dependencies in the PR description, including purpose, license, maintenance,
and why the standard library or existing dependencies are insufficient. Dependencies and vendored
assets must be compatible with AGPL-3.0-or-later; retain required license and notice files.
