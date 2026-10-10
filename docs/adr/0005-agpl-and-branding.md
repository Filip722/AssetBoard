# ADR-0005: AGPL license and centralized branding

**Status:** Accepted
**Date:** 2026-10-10

## Context

AssetBoard is self-hosted software whose modifications should remain available to users, and the
working project name may change.

## Decision

License the project under AGPL-3.0-or-later. Keep display name, environment prefix, CLI name, and
user-agent branding centralized in `src/assetboard/branding.py`; do not embed the product name in
templates or user-facing code.

## Consequences

Contributors and dependencies must be compatible with the project license. A future rename can be
made through the branding module and package/container metadata rather than scattered UI strings.
