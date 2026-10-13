"""The audit log page (F1.5 part C)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent, ExternalIdentity, Membership, User, Workspace
from assetboard.services.audit_view import ACTION_LABELS, DATA_KEYS, KEY_LABELS
from tests.oidc.fake_idp import ISSUER
from tests.web.test_profile_routes import (
    LOCAL_PASSWORD,
    Harness,
    add_identity,
    add_user,
    admin,
    harness,
)

if TYPE_CHECKING:
    from conftest import WebHarness

SECRET_SUBJECT = "secret-subject-9f3a"
PROVIDER_EMAIL = "provider-only@example.net"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def h(web: WebHarness) -> Iterator[Harness]:
    with harness(web) as value:
        yield value


def home(h: Harness) -> UUID:
    with h.db() as session:
        return session.scalars(select(Workspace.id).where(Workspace.name == "Home")).one()


def other_workspace(h: Harness) -> UUID:
    with h.app.state.session_factory.begin() as session:
        workspace = Workspace(name="Elsewhere", base_currency="EUR", locale="en", timezone="UTC")
        session.add(workspace)
        session.flush()
        return workspace.id


def event(
    h: Harness,
    action: str,
    *,
    workspace_id: UUID | None = None,
    data: dict[str, Any] | None = None,
    ts: datetime = BASE,
    actor: UUID | None = None,
    entity: str | None = None,
    ip: str | None = "203.0.113.0/24",
    event_id: UUID | None = None,
) -> UUID:
    with h.app.state.session_factory.begin() as session:
        row = AuditEvent(
            id=event_id or uuid4(),
            workspace_id=workspace_id,
            actor_user_id=actor,
            actor_type="user" if actor else "system",
            action=action,
            entity_type="user",
            entity_id=entity,
            data=data or {},
            ts=ts,
            ip_prefix=ip,
        )
        session.add(row)
        session.flush()
        return row.id


def member(h: Harness, email: str, role: str, workspace_id: UUID | None = None) -> UUID:
    user_id = add_user(h, email)
    with h.app.state.session_factory.begin() as session:
        session.add(Membership(workspace_id=workspace_id or home(h), user_id=user_id, role=role))
    return user_id


def signed_in(h: Harness, email: str) -> TestClient:
    client = h.client()
    assert h.web.sign_in(client, email=email, password=LOCAL_PASSWORD).status_code == 303
    return client


def rows(page: str) -> list[str]:
    return re.findall(r"<tr data-audit-row>(.*?)</tr>", page, flags=re.S)


def planted(page: str) -> list[str]:
    """Rows of events planted on 2026-09-01 (real sign-ins of the test run are newer)."""
    return [row for row in rows(page) if "2026-09-01" in row]


def admin_id(h: Harness) -> UUID:
    return h.user(h.web.admin_email).id


# Who sees which scope


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_viewers_and_editors_get_403(h: Harness, role: str) -> None:
    member(h, "m@example.com", role)
    client = signed_in(h, "m@example.com")
    assert client.get("/audit?scope=workspace").status_code == 403
    assert client.get("/audit?scope=instance").status_code == 403
    nav = client.get("/account").text
    assert "data-audit-workspace-link" not in nav and "data-audit-instance-link" not in nav


def test_an_owner_who_is_not_an_administrator_sees_only_the_workspace_scope(h: Harness) -> None:
    member(h, "owner2@example.com", "owner")
    client = signed_in(h, "owner2@example.com")
    assert client.get("/audit?scope=workspace").status_code == 200
    assert client.get("/audit?scope=instance").status_code == 403
    nav = client.get("/workspace/members").text
    assert "data-audit-workspace-link" in nav and "data-audit-instance-link" not in nav


def test_an_administrator_without_ownership_sees_only_the_instance_scope(h: Harness) -> None:
    user_id = member(h, "admin2@example.com", "viewer")
    with h.app.state.session_factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.is_instance_admin = True
    client = signed_in(h, "admin2@example.com")
    assert client.get("/audit?scope=instance").status_code == 200
    assert client.get("/audit?scope=workspace").status_code == 403


def test_signed_out_visitors_are_sent_to_sign_in(h: Harness) -> None:
    response = h.client().get("/audit?scope=instance", follow_redirects=False)
    assert response.headers["location"].startswith("/login")


# Tenant isolation


def test_each_scope_shows_only_its_own_events(h: Harness) -> None:
    elsewhere = other_workspace(h)
    event(h, "member.removed", workspace_id=home(h), data={"role": "viewer"})
    event(h, "member.role_changed", workspace_id=elsewhere, data={"from": "viewer", "to": "editor"})
    event(h, "admin.user_activated", workspace_id=None)
    client = admin(h)  # owner of Home and instance administrator
    workspace_page = client.get("/audit?scope=workspace").text
    assert "Member removed" in workspace_page
    assert "Member role changed" not in workspace_page  # another workspace
    assert "Account activated" not in workspace_page  # instance-level
    instance_page = client.get("/audit?scope=instance").text
    assert "Account activated" in instance_page
    assert "Member removed" not in instance_page and "Member role changed" not in instance_page


# What a row shows


def test_subjects_tokens_and_provider_emails_never_appear(h: Harness) -> None:
    add_user(h, "ola@example.com")
    add_identity(h, "ola@example.com", ISSUER, SECRET_SUBJECT)
    with h.app.state.session_factory.begin() as session:
        for identity in session.scalars(select(ExternalIdentity)):
            identity.email_at_link = PROVIDER_EMAIL
    target = str(h.user("ola@example.com").id)
    planted = {
        "reason": "id_token_invalid",
        "subject": SECRET_SUBJECT,
        "email": PROVIDER_EMAIL,
        "id_token": "eyJ.secret.token",
    }
    event(h, "auth.oidc_failure", data=planted, entity=target)
    event(h, "something.unknown", data=planted, entity=target)
    event(h, "member.removed", workspace_id=home(h), data=planted)
    client = admin(h)
    pages = [
        client.get("/audit?scope=instance").text,
        client.get("/audit?scope=workspace").text,
        client.get("/admin/users").text,
    ]
    for page in pages:
        for secret in (SECRET_SUBJECT, PROVIDER_EMAIL, "eyJ.secret.token"):
            assert secret not in page
    assert "the provider&#39;s ID token was not valid" in pages[0]


def test_an_unknown_action_shows_time_and_a_generic_label_only(h: Harness) -> None:
    event(h, "something.unknown", data={"reason": "throttled", "via": "profile"})
    (row,) = planted(admin(h).get("/audit?scope=instance").text)
    assert "Other event" in row and "2026-09-01 12:00:00 UTC" in row
    assert "Reason" not in row and "too many attempts" not in row


def test_link_flow_failures_read_as_link_failures(h: Harness) -> None:
    event(h, "auth.oidc_failure", data={"reason": "id_token_invalid", "via": "profile"})
    event(h, "auth.oidc_failure", data={"reason": "id_token_invalid"}, ts=BASE - timedelta(hours=1))
    linking, sign_in = planted(admin(h).get("/audit?scope=instance").text)
    assert "Linking an identity provider failed" in linking and "sign-in refused" not in linking
    assert "Identity provider sign-in refused" in sign_in


def test_instance_rows_show_emails_and_ip_prefix_workspace_rows_show_the_member_name_only(
    h: Harness,
) -> None:
    add_user(h, "ola@example.com")
    target = str(h.user("ola@example.com").id)
    event(
        h, "admin.user_deactivated", data={"sessions_revoked": 2}, actor=admin_id(h), entity=target
    )
    event(
        h,
        "member.removed",
        workspace_id=home(h),
        data={"role": "viewer", "user_id": target},
        actor=admin_id(h),
        entity=target,
    )
    client = admin(h)
    (instance_row,) = planted(client.get("/audit?scope=instance").text)
    assert "ola@example.com" in instance_row and "203.0.113.0/24" in instance_row
    assert "Sessions ended: 2" in instance_row
    assert h.web.admin_email in instance_row  # actor with email in the instance scope
    (workspace_row,) = planted(client.get("/audit?scope=workspace").text)
    assert "Admin" in workspace_row  # the actor's display name
    # The affected member by display name only (from data["user_id"]): no email, no IP.
    assert re.search(r"data-audit-target>\s*ola\s*<", workspace_row)
    for hidden in ("ola@example.com", "203.0.113.0/24", h.web.admin_email, target):
        assert hidden not in workspace_row
    assert "Role: viewer" in workspace_row


def test_display_names_are_escaped(h: Harness) -> None:
    with h.app.state.session_factory.begin() as session:
        user = session.get(User, admin_id(h))
        assert user is not None
        user.display_name = "<script>alert(1)</script>"
    event(h, "member.removed", workspace_id=home(h), actor=admin_id(h), data={"role": "viewer"})
    page = admin(h).get("/audit?scope=workspace").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_labels_and_reasons_are_translated(h: Harness) -> None:
    event(h, "auth.proxy_failure", data={"reason": "inactive"})
    page = admin(h).get("/audit?scope=instance", headers={"Accept-Language": "pl"}).text
    assert "Odrzucono logowanie przez serwer proxy" in page
    assert "konto dezaktywowane" in page


def test_real_events_appear(h: Harness) -> None:
    client = admin(h)  # its own sign-in is an instance event
    assert "Signed in" in client.get("/audit?scope=instance").text
    # Setup records its event in the workspace it created.
    assert "First administrator created at setup" in client.get("/audit?scope=workspace").text


# Filters


def test_group_filter(h: Harness) -> None:
    event(h, "auth.login_failure", data={})
    event(h, "admin.user_activated", ts=BASE - timedelta(minutes=1))
    client = admin(h)
    failures = client.get("/audit?scope=instance&group=failures").text
    assert "Sign-in failed" in failures and "Account activated" not in failures
    accounts = client.get("/audit?scope=instance&group=accounts").text
    assert "Account activated" in accounts and "Sign-in failed" not in accounts
    assert client.get("/audit?scope=instance&group=members").status_code == 400  # wrong scope
    assert client.get("/audit?scope=workspace&group=nope").status_code == 400


def test_date_range_filter_includes_both_days(h: Harness) -> None:
    for day in (1, 2, 3, 4):
        event(h, "admin.user_activated", ts=datetime(2026, 9, day, 23, 59, tzinfo=UTC))
    client = admin(h)
    page = client.get("/audit?scope=instance&group=accounts&from=2026-09-02&to=2026-09-03").text
    days = [re.search(r"2026-09-0\d", row).group(0) for row in rows(page)]  # type: ignore[union-attr]
    assert days == ["2026-09-03", "2026-09-02"]
    assert client.get("/audit?scope=instance&from=2026-13-01").status_code == 400


def test_account_filter_in_the_instance_scope(h: Harness) -> None:
    add_user(h, "ola@example.com")
    add_user(h, "kim@example.com")
    ola = h.user("ola@example.com").id
    event(h, "admin.user_activated", entity=str(ola))
    event(h, "admin.user_deactivated", entity=str(h.user("kim@example.com").id))
    client = admin(h)
    # The Users page links each account's history by id; no email ever goes into a URL.
    users = client.get("/admin/users").text
    assert f"/audit?scope=instance&amp;account={ola}" in users
    page = client.get(f"/audit?scope=instance&group=accounts&account={ola}").text
    assert "Account activated" in page and "Account deactivated" not in page
    assert "data-audit-account" in page and "Show all accounts" in page
    unknown = "00000000-0000-0000-0000-000000000000"
    assert "data-audit-empty" in client.get(f"/audit?scope=instance&account={unknown}").text
    assert client.get("/audit?scope=instance&account=ola@example.com").status_code == 400
    # The workspace scope has no such filter; the parameter is ignored there.
    assert client.get(f"/audit?scope=workspace&account={ola}").status_code == 200


# Pagination


def test_keyset_pages_cover_every_event_once(h: Harness) -> None:
    same_moment = BASE + timedelta(days=1)
    ids = [event(h, "admin.user_activated", ts=same_moment) for _ in range(4)]
    ids += [event(h, "admin.user_activated", ts=BASE + timedelta(hours=n)) for n in range(3)]
    client = admin(h)
    url = "/audit?scope=instance&group=accounts&limit=3"
    seen: list[str] = []
    pages = 0
    while url:
        page = client.get(url).text
        pages += 1
        found = rows(page)
        assert 0 < len(found) <= 3
        seen += [re.sub(r"\s+", " ", row) for row in found]
        match = re.search(r'href="([^"]+)" data-audit-next', page)
        url = match.group(1).replace("&amp;", "&") if match else ""
        assert pages <= 5
    assert pages == 3 and len(seen) == 7


def test_a_full_last_page_has_no_older_link(h: Harness) -> None:
    for n in range(3):
        event(h, "admin.user_activated", ts=BASE + timedelta(hours=n))
    page = admin(h).get("/audit?scope=instance&group=accounts&limit=3").text
    assert len(rows(page)) == 3 and "data-audit-next" not in page


@pytest.mark.parametrize(
    "query",
    [
        "cursor=garbage",
        "cursor=123",
        "cursor=1.0123456789abcdef0123456789abcdeg",
        "cursor=99999999999999999999.0123456789abcdef0123456789abcdef",
        "limit=0",
        "limit=101",
        "limit=ten",
        "scope=everything",
    ],
)
def test_malformed_requests_are_400(h: Harness, query: str) -> None:
    scope = "" if query.startswith("scope=") else "scope=instance&"
    assert admin(h).get(f"/audit?{scope}{query}").status_code == 400


def test_every_allowlisted_key_and_action_has_a_label() -> None:
    assert {key for keys in DATA_KEYS.values() for key in keys} <= set(KEY_LABELS)
    assert set(DATA_KEYS) == set(ACTION_LABELS)


# Review follow-ups


def test_owner_as_a_role_and_as_a_revocation_reason_read_differently(h: Harness) -> None:
    event(h, "member.role_changed", workspace_id=home(h), data={"from": "editor", "to": "owner"})
    event(
        h,
        "invitation.revoked",
        workspace_id=home(h),
        data={"reason": "owner"},
        ts=BASE - timedelta(minutes=1),  # older: listed second
    )
    page = admin(h).get("/audit?scope=workspace").text
    role_change, revoked = planted(page)
    assert "From: editor" in role_change and "To: owner" in role_change
    assert "by an owner" not in role_change
    assert "Reason: by an owner" in revoked


def test_a_member_who_no_longer_resolves_reads_former_member(h: Harness) -> None:
    gone = str(uuid4())
    event(h, "member.removed", workspace_id=home(h), data={"role": "viewer", "user_id": gone})
    (row,) = planted(admin(h).get("/audit?scope=workspace").text)
    assert "Former member" in row and gone not in row


def test_times_show_seconds(h: Harness) -> None:
    event(h, "admin.user_activated", ts=BASE + timedelta(seconds=7))
    (row,) = planted(admin(h).get("/audit?scope=instance&group=accounts").text)
    assert "2026-09-01 12:00:07 UTC" in row


def test_an_unknown_action_still_shows_actor_target_and_network_but_no_details(
    h: Harness,
) -> None:
    add_user(h, "ola@example.com")
    target = str(h.user("ola@example.com").id)
    event(
        h,
        "something.unknown",
        actor=admin_id(h),
        entity=target,
        data={"reason": "throttled", "sessions_revoked": 3},
    )
    (row,) = planted(admin(h).get("/audit?scope=instance").text)
    assert "Other event" in row
    assert "Admin" in row and h.web.admin_email in row  # actor
    assert "ola@example.com" in row  # affected account
    assert "203.0.113.0/24" in row  # network
    # The details cell stays empty: no key of an unknown action is rendered.
    details = re.findall(r'<td class="px-5 py-3 text-xs">(.*?)</td>', row, flags=re.S)
    assert [cell.strip() for cell in details] == [""]
    assert "Reason" not in row and "Sessions ended" not in row


def test_a_bare_audit_opens_the_scope_the_user_may_see(h: Harness) -> None:
    owner_and_admin = admin(h)
    assert "Audit log: workspace" in owner_and_admin.get("/audit").text
    member(h, "admin2@example.com", "viewer")
    with h.app.state.session_factory.begin() as session:
        user = session.get(User, h.user("admin2@example.com").id)
        assert user is not None
        user.is_instance_admin = True
    assert "Audit log: instance" in signed_in(h, "admin2@example.com").get("/audit").text
    member(h, "viewer@example.com", "viewer")
    assert signed_in(h, "viewer@example.com").get("/audit").status_code == 403


# Delta review


def test_the_last_representable_day_leaves_the_range_open(h: Harness) -> None:
    event(h, "admin.user_activated")
    client = admin(h)
    page = client.get("/audit?scope=instance&group=accounts&to=9999-12-31")
    assert page.status_code == 200 and len(planted(page.text)) == 1
    far = client.get("/audit?scope=instance&from=9999-12-31&to=9999-12-31")
    assert far.status_code == 200 and "data-audit-empty" in far.text
    assert client.get("/audit?scope=instance&from=0001-01-01").status_code == 200


def test_the_member_column_with_the_real_writers(h: Harness) -> None:
    changed = member(h, "changed@example.com", "viewer")
    removed = member(h, "removed@example.com", "viewer")
    client = admin(h)
    with h.db() as session:
        memberships = {
            m.user_id: m.id
            for m in session.scalars(select(Membership).where(Membership.workspace_id == home(h)))
        }
    token = h.token(client, "/workspace/members")
    role = client.post(
        f"/workspace/members/{memberships[changed]}/role",
        data={"role": "editor", "csrf_token": token},
    )
    assert role.status_code in {200, 303}
    gone = client.post(
        f"/workspace/members/{memberships[removed]}/remove", data={"csrf_token": token}
    )
    assert gone.status_code in {200, 303}
    # A real invitation: created by the owner through the members page, accepted by a
    # signed-in account.
    created = client.post(
        "/workspace/invitations",
        data={
            "role": "viewer",
            "email": "",
            "expires_in_days": "7",
            "csrf_token": h.token(client, "/workspace/members"),
        },
    )
    link = re.search(r'value="[^"]*/invite/([A-Za-z0-9_-]{43})"', created.text)
    assert link is not None
    invite = link.group(1)
    add_user(h, "joiner@example.com")
    joiner = signed_in(h, "joiner@example.com")
    page = joiner.get(f"/invite/{invite}")
    accepted = joiner.post(
        f"/invite/{invite}/accept",
        data={"csrf_token": h.web.csrf(page.text)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    found = rows(client.get("/audit?scope=workspace&group=members").text)
    by_label = {
        label: row
        for row in found
        for label in ("Member role changed", "Member removed")
        if label in row
    }
    assert re.search(r"data-audit-target>\s*changed\s*<", by_label["Member role changed"])
    assert re.search(r"data-audit-target>\s*removed\s*<", by_label["Member removed"])
    invitations = rows(client.get("/audit?scope=workspace&group=invitations").text)
    (accepted_row,) = [row for row in invitations if "Invitation accepted" in row]
    assert re.search(r"data-audit-target>\s*joiner\s*<", accepted_row)
    for row in (*by_label.values(), accepted_row):
        assert "@example.com" not in row  # display names only


def test_no_dangling_separator_on_your_own_row(h: Harness) -> None:
    page = admin(h).get("/admin/users").text
    own = page.split(f'data-user="{h.web.admin_email}"')[1].split("</tr>")[0]
    actions = re.sub(r"<[^>]+>", " ", own.split('text-right whitespace-nowrap">')[1])
    words = actions.split()
    assert words == ["History", "You"], words
