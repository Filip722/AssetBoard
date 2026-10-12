from __future__ import annotations

from assetboard.db.models.identity import Membership
from assetboard.db.repositories.base import ScopedRepository


class MembershipRepository(ScopedRepository[Membership]):
    """Members of the current workspace (part B adds the members page on top of it)."""

    model = Membership
