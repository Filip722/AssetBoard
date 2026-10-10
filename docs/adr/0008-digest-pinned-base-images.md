# ADR-0008: Digest-pinned base images

**Status:** Accepted
**Date:** 2026-10-10

## Context

Container tags can move, so a tag alone does not guarantee that a build uses the reviewed base
image. At the same time, fixed digests need a maintenance process to receive upstream security and
bug fixes.

## Decision

Pin Docker base images to explicit versions and image digests. Configure Dependabot to propose
Docker dependency updates, and review and merge digest bumps through the normal CI checks.

## Consequences

Build inputs are reproducible between updates and updates remain visible as reviewable changes.
Maintainers must review Dependabot proposals and keep supported architectures available at the
selected digest.
