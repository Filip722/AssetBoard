# ADR-0010: Release Please and workflow-call image publishing

**Status:** Accepted
**Date:** 2026-10-10

## Context

Versioning and release image publishing should follow reviewed Conventional Commit history, with
the image built and checked consistently across release workflows.

## Decision

Use Release Please to maintain a release PR and create version tags after it is merged. The release
workflow calls the reusable Docker workflow to publish multi-architecture images to GHCR. Keep the
publishing permissions explicit and limited to the job that writes packages.

## Consequences

Maintainers decide when to merge the release PR, while Release Please computes release metadata.
Repository Actions settings must permit PR creation, and the calling workflow must grant the
reusable publisher `contents: read` and `packages: write`. Release PRs created with `GITHUB_TOKEN`
do not trigger ordinary CI workflows; see the [deployment guide](../deployment.md#release-automation-credentials)
for the available options.
