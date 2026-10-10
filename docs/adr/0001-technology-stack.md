# ADR-0001: Technology stack

**Status:** Accepted
**Date:** 2026-10-10

## Context

AssetBoard needs a maintainable stack for a self-hosted finance application with a small operational
footprint, typed Python code, migrations, and testable service boundaries.

## Decision

Use Python 3.13+ managed with uv; FastAPI, Pydantic v2, SQLAlchemy 2, and Alembic; SQLite with WAL
by default while keeping services portable to PostgreSQL; and pytest, Ruff, and Pyright for quality
checks. Runtime dependencies require explicit justification.

## Consequences

The project can use Python's typing and ecosystem while remaining deployable as a single service.
Database portability requires avoiding SQLite-only service SQL and keeping migration/model metadata
consistent.
