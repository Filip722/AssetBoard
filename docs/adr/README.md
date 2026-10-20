# Architecture decision records

The template defines the required section structure. New ADRs need a Status and Date. Status values
are `Proposed`, `Accepted`, or `Superseded by ADR-NNNN` (with the replacing ADR's number).

The decisions below are accepted unless noted otherwise.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-technology-stack.md) | Technology stack | Accepted |
| [0002](0002-money-and-decimals.md) | Money and decimal values | Accepted |
| [0003](0003-workspaces-and-tenant-isolation.md) | Workspaces and tenant isolation | Accepted |
| [0004](0004-server-rendered-ui.md) | Server-rendered user interface | Accepted |
| [0005](0005-agpl-and-branding.md) | AGPL license and centralized branding | Accepted |
| [0006](0006-uuid-primary-keys.md) | UUID primary keys | Accepted |
| [0007](0007-sqlite-transaction-control.md) | SQLite transaction control | Accepted |
| [0008](0008-digest-pinned-base-images.md) | Digest-pinned base images | Accepted |
| [0009](0009-container-security.md) | Non-root container with read-only root filesystem | Accepted |
| [0010](0010-release-automation.md) | Release Please and workflow-call image publishing | Accepted |
| [0011](0011-tzdata-dependency.md) | Locked timezone database data | Accepted |
| [0012](0012-local-authentication.md) | Local authentication, server-side sessions and first-run setup | Accepted |
| [0013](0013-invitation-links.md) | Invitation links | Accepted |
| [0014](0014-oidc-sign-in.md) | OIDC sign-in | Accepted |
| [0015](0015-proxy-header-authentication.md) | Reverse-proxy header authentication | Accepted |
| [0016](0016-workspace-archive-format.md) | Workspace archive format (export, import and restore) | Proposed |
| [0017](0017-local-totp.md) | Local TOTP and workspace enforcement | Proposed |

New records should use [the ADR template](template.md), the next available number, and include
Context, Decision, and Consequences sections.
