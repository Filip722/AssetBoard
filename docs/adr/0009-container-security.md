# ADR-0009: Non-root container with read-only root filesystem

**Status:** Accepted
**Date:** 2026-10-10

## Context

The app needs persistent state but should limit the impact of a compromised process and avoid
writing mutable data into the application image.

## Decision

Run the container as UID/GID `10001:10001`, use a read-only root filesystem, drop Linux
capabilities, and allow writes only to the persistent `/data` volume and temporary `/tmp` storage.

## Consequences

Deployments must preserve `/data` and make it writable by UID/GID 10001. The container cannot
modify its installed application files at runtime; operators must use a named volume or set
appropriate ownership on bind mounts.
