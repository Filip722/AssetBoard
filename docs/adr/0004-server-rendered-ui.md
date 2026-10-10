# ADR-0004: Server-rendered user interface

**Status:** Accepted
**Date:** 2026-10-10

## Context

The first version should be straightforward to host and usable without a separate JavaScript
application build or runtime service.

## Decision

Render pages with Jinja2 and enhance interactions with HTMX and Alpine.js. Build CSS with the
standalone Tailwind CLI, keep third-party browser assets vendored and licensed, and use Apache
ECharts for charts. Keep templates autoescaped and avoid inline scripts for CSP compatibility.

## Consequences

The web app remains a single deployable service with modest browser-side behavior. Vendored assets
need version, license, notice, and hash records; generated assets must be reproducible in the build.
