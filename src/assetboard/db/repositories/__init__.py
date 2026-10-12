"""Repositories. Importing this package registers every ``ScopedRepository`` subclass."""

from assetboard.db.repositories.audit import AuditEventRepository
from assetboard.db.repositories.base import (
    ForeignWorkspace,
    NotFound,
    RepositoryError,
    ScopedRepository,
    UnsupportedOperation,
)
from assetboard.db.repositories.membership import MembershipRepository
from assetboard.db.repositories.user_memberships import MembershipView, UserMembershipReader

__all__ = [
    "AuditEventRepository",
    "ForeignWorkspace",
    "MembershipRepository",
    "MembershipView",
    "NotFound",
    "RepositoryError",
    "ScopedRepository",
    "UnsupportedOperation",
    "UserMembershipReader",
]
