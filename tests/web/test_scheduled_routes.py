from uuid import uuid4

from assetboard.clock import FixedClock
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.web.scheduled_routes import _rule


def form(owner, **overrides):
    page = owner.client.get("/recurring/new")
    # Seeded categories are expense first; choose the option from the actual form.
    import re

    category = re.search(r'<option value="([a-f0-9-]{36})"', page.text)
    assert category
    return {
        "name": "Car insurance",
        "direction": "expense",
        "category_id": category.group(1),
        "amount": "1800",
        "amount_mode": "estimate",
        "start_date": "2026-01-01",
        "freq": "monthly",
        "interval": "1",
        "month_day": "31",
        "month": "1",
        "weekend_policy": "none",
        **overrides,
    }


def test_crud_preview_pause_resume_archive_and_delete(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    data = form(owner)
    response = owner.post("/recurring/preview", data)
    assert response.status_code == 200
    for day in ("2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30", "2026-07-31", "2026-08-31"):
        assert day in response.text
    created = owner.post("/recurring", data)
    assert created.status_code == 303
    listing = owner.client.get("/recurring")
    assert "Car insurance" in listing.text and "Estimate" in listing.text
    with owner.db() as session:
        from sqlalchemy import select

        row = session.scalars(select(ScheduledItem)).one()
        item_id = row.id
    edit = owner.client.get(f"/recurring/{item_id}/edit")
    assert edit.status_code == 200 and "1800" in edit.text
    assert owner.post(f"/recurring/{item_id}/edit", data | {"name": "Updated"}).status_code == 303
    for action in ("pause", "resume", "archive"):
        assert owner.post(f"/recurring/{item_id}/{action}").status_code == 303
    assert "Archived" in owner.client.get("/recurring").text
    assert owner.post("/recurring", data | {"name": "Delete me"}).status_code == 303
    with owner.db() as session:
        delete_id = session.scalars(
            select(ScheduledItem.id).where(ScheduledItem.name == "Delete me")
        ).one()
    assert owner.post(f"/recurring/{delete_id}/delete").status_code == 303
    assert "Delete me" not in owner.client.get("/recurring").text


def test_invalid_input_csrf_roles_and_other_workspace_ids(owner):
    data = form(owner)
    for change in (
        {"name": ""},
        {"amount": "nan"},
        {"interval": "0"},
        {"start_date": "bad"},
        {"category_id": ""},
    ):
        assert owner.post("/recurring", data | change).status_code == 400
    assert owner.post("/recurring/preview", data | {"freq": "weekly"}).status_code == 200
    assert (
        "Choose valid recurrence dates"
        in owner.post("/recurring/preview", data | {"freq": "weekly"}).text
    )
    assert owner.client.post("/recurring", data=data).status_code == 403
    assert owner.client.post("/recurring/preview", data=data).status_code == 403
    assert owner.client.get("/recurring/bad/edit").status_code == 404
    assert owner.post(f"/recurring/{uuid4()}/delete").status_code == 404
    assert owner.post(f"/recurring/{uuid4()}/unexpected").status_code == 404
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    assert owner.post("/recurring", data).status_code == 303
    with owner.db() as session:
        from sqlalchemy import select

        item_id = session.scalars(select(ScheduledItem.id)).one()
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    assert "Car insurance" not in owner.client.get("/recurring").text
    assert owner.client.get(f"/recurring/{item_id}/edit").status_code == 404
    assert owner.post(f"/recurring/{item_id}/pause").status_code == 404
    owner.post("/workspace/switch", {"workspace_id": str(owner.home_id)})
    owner.set_admin_role("viewer")
    assert owner.client.get("/recurring").status_code == 200
    assert owner.client.get("/recurring/new").status_code == 403
    assert owner.post("/recurring", data).status_code == 403
    assert owner.post(f"/recurring/{item_id}/pause").status_code == 403


def test_locale_amount_and_weekly_form(owner):
    owner.client.headers["accept-language"] = "pl"
    assert "Nowa pozycja cykliczna" in owner.client.get("/recurring/new").text
    data = form(
        owner, amount="1 234,56", freq="weekly", weekday="0", max_count="2", end_date="2026-12-31"
    )
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    assert owner.post("/recurring", data).status_code == 303
    with owner.db() as session:
        from sqlalchemy import select

        item = session.scalars(select(ScheduledItem)).one()
        assert item.amount_minor == 123456 and item.weekdays == [0]
    assert _rule(data | {"weekdays": "0"}).weekdays == (0,)
