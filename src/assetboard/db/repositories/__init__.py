"""Repositories. Importing this package registers every ``ScopedRepository`` subclass."""

from assetboard.db.repositories.audit import (
    AuditCursor,
    AuditEventRepository,
    AuditFilter,
    InstanceAuditReader,
)
from assetboard.db.repositories.base import (
    ForeignWorkspace,
    NotFound,
    RepositoryError,
    ScopedRepository,
    UnsupportedOperation,
)
from assetboard.db.repositories.category import CategoryRepository
from assetboard.db.repositories.invitation import (
    InvitationLocation,
    InvitationRepository,
    InvitationTokenReader,
)
from assetboard.db.repositories.membership import MembershipRepository, WorkspaceMember
from assetboard.db.repositories.month_state import MonthStateRepository
from assetboard.db.repositories.user_memberships import MembershipView, UserMembershipReader
from assetboard.db.repositories.workspace import UPDATABLE_FIELDS, CurrentWorkspaceRepository

__all__ = [
    "UPDATABLE_FIELDS",
    "AuditCursor",
    "AuditEventRepository",
    "AuditFilter",
    "CategoryRepository",
    "InstanceAuditReader",
    "CurrentWorkspaceRepository",
    "ForeignWorkspace",
    "InvitationLocation",
    "InvitationRepository",
    "InvitationTokenReader",
    "MembershipRepository",
    "MembershipView",
    "MonthStateRepository",
    "NotFound",
    "RepositoryError",
    "ScopedRepository",
    "UnsupportedOperation",
    "UserMembershipReader",
    "WorkspaceMember",
]
