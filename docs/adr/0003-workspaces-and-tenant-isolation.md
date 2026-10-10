# ADR-0003: Workspaces and tenant isolation

**Status:** Accepted
**Date:** 2026-10-10

## Context

Users need to separate financial records into workspaces, and every query must prevent records from
other workspaces from leaking into a request.

## Decision

Workspace-owned records carry a `workspace_id`. Access goes through repositories that enforce the
active workspace scope. Services receive a `Context` containing the user, workspace, role, and
clock; authorization is enforced in services.

## Consequences

Tenant scoping is an explicit repository responsibility and can be tested systematically. New
workspace-owned models require isolation coverage, and routes cannot bypass services to query ORM
models directly.
