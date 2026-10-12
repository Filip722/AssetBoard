"""Which test covers each public repository method (acceptance #11).

Every public method of every registered repository, and of ``UserMembershipReader``, must be
named here with the test that proves it cannot cross workspaces. A new public method without an
entry fails ``test_every_public_repository_method_is_covered``; an entry naming a method or test
that no longer exists fails too.
"""

from __future__ import annotations

ISOLATION_TEST = "test_user_b_cannot_read_update_or_delete_user_a_rows"

# Methods every ScopedRepository has, covered for every model by the parametrized check.
BASE_METHOD_COVERAGE: dict[str, str] = {
    "get": ISOLATION_TEST,
    "get_or_raise": ISOLATION_TEST,
    "list": ISOLATION_TEST,
    "update": ISOLATION_TEST,
    "delete": ISOLATION_TEST,
    "add": "test_add_refuses_rows_of_another_workspace_or_type",
}

# Public methods a subclass adds: (repository class name, method) -> covering test.
EXTRA_METHOD_COVERAGE: dict[tuple[str, str], str] = {
    ("MembershipRepository", "members"): "test_membership_queries_stay_in_their_workspace",
    ("MembershipRepository", "for_user"): "test_membership_queries_stay_in_their_workspace",
    ("MembershipRepository", "count_owners"): "test_membership_queries_stay_in_their_workspace",
    ("AuditEventRepository", "record"): "test_audit_record_writes_into_its_own_workspace",
    ("MembershipRepository", "add_member"): "test_membership_queries_stay_in_their_workspace",
    **{
        ("InvitationRepository", method): "test_invitation_transitions_stay_in_their_workspace"
        for method in (
            "create",
            "list_pending",
            "count_pending",
            "revoke",
            "mark_used",
            "record_failed_email",
        )
    },
}

READER_METHOD_COVERAGE: dict[str, str] = {
    "memberships": "test_user_membership_reader_returns_only_its_users_rows",
    "membership_in": "test_user_membership_reader_returns_only_its_users_rows",
}

# CurrentWorkspaceRepository is not a ScopedRepository (Workspace is the tenant row itself).
CURRENT_WORKSPACE_METHOD_COVERAGE: dict[str, str] = {
    "get": "test_current_workspace_repository_touches_only_its_workspace",
    "update": "test_current_workspace_repository_touches_only_its_workspace",
}

# InvitationTokenReader is scoped by token hash (the invitee has no Context yet).
TOKEN_READER_METHOD_COVERAGE: dict[str, str] = {
    "locate": "test_invitation_token_reader_finds_only_the_matching_hash",
}
