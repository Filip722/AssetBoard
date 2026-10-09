from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Integer, MetaData, String, inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import Numeric

from assetboard.clock import FixedClock
from assetboard.db.base import (
    CONSTRAINT_NAMING_CONVENTION,
    IdMixin,
    SoftDeleteMixin,
    TimestampMixin,
    WorkspaceScopedMixin,
)
from assetboard.db.engine import create_database_engine, database_is_ready
from assetboard.db.money import money_composite
from assetboard.db.types import DecimalType, UtcDateTime
from assetboard.domain.money import Money


class ScratchBase(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


class ScratchWorkspace(ScratchBase, IdMixin, TimestampMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchRecord(ScratchBase, IdMixin, TimestampMixin, SoftDeleteMixin, WorkspaceScopedMixin):
    __tablename__ = "scratch_records"

    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Money] = money_composite("amount_minor", "currency")
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)


def is_supported_decimal(value: Decimal) -> bool:
    exponent = value.as_tuple().exponent
    return (
        isinstance(exponent, int) and exponent >= -18 and (value.is_zero() or value.adjusted() < 20)
    )


@pytest.fixture
def scratch_session(tmp_path: Path, fixed_clock) -> Iterator[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    ScratchBase.metadata.create_all(engine)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        info={"clock": fixed_clock},
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        ScratchBase.metadata.drop_all(engine)
        engine.dispose()


def test_sqlite_engine_sets_required_pragmas(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
    finally:
        engine.dispose()


def test_database_engine_readiness_check(database_engine) -> None:
    assert database_is_ready(database_engine)


@pytest.mark.parametrize(
    "quantity",
    [
        Decimal("0.00924"),
        Decimal("0.123456789012345678"),
        Decimal("-123.45"),
    ],
)
def test_base_and_mixins_have_stable_constraints_and_uuid_keys(
    scratch_session, fixed_clock, quantity
) -> None:
    assert CONSTRAINT_NAMING_CONVENTION["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )
    workspace = ScratchWorkspace(name="Synthetic workspace")
    scratch_session.add(workspace)
    scratch_session.flush()

    record = ScratchRecord(
        workspace_id=workspace.id,
        amount=Money(1234, "EUR"),
        quantity=quantity,
    )
    scratch_session.add(record)
    scratch_session.flush()
    scratch_session.expire(record)

    reloaded = scratch_session.get(ScratchRecord, record.id)
    assert reloaded is not None
    assert reloaded.id == record.id
    assert reloaded.workspace_id == workspace.id
    assert reloaded.amount == Money(1234, "EUR")
    assert reloaded.quantity == quantity
    assert reloaded.created_at == fixed_clock.now()
    assert reloaded.updated_at == fixed_clock.now()
    assert reloaded.created_at.utcoffset() is not None
    assert reloaded.updated_at.utcoffset() is not None
    assert reloaded.deleted_at is None
    assert isinstance(ScratchRecord.__table__.c.deleted_at.type, UtcDateTime)


def test_timestamp_updated_at_uses_injected_clock(scratch_session, fixed_clock) -> None:
    workspace = ScratchWorkspace(name="Synthetic workspace")
    scratch_session.add(workspace)
    scratch_session.flush()
    record = ScratchRecord(
        workspace_id=workspace.id,
        amount=Money(1, "EUR"),
        quantity=Decimal("1"),
    )
    scratch_session.add(record)
    scratch_session.flush()
    created_at = record.created_at

    later_clock = FixedClock("2026-01-02T03:04:05+00:00")
    scratch_session.info["clock"] = later_clock
    record.quantity = Decimal("2")
    scratch_session.flush()
    scratch_session.expire(record)
    reloaded = scratch_session.get(ScratchRecord, record.id)

    assert reloaded is not None
    assert reloaded.created_at == fixed_clock.now()
    assert reloaded.updated_at == later_clock.now()
    assert reloaded.updated_at > created_at


def test_timestamp_listener_preserves_explicit_insert_timestamps(scratch_session) -> None:
    workspace = ScratchWorkspace(name="Synthetic workspace")
    scratch_session.add(workspace)
    scratch_session.flush()
    created_at = datetime.fromisoformat("2025-12-31T12:00:00+00:00")
    updated_at = datetime.fromisoformat("2026-01-01T12:00:00+00:00")
    record = ScratchRecord(
        workspace_id=workspace.id,
        amount=Money(1, "EUR"),
        quantity=Decimal("1"),
        created_at=created_at,
        updated_at=updated_at,
    )

    scratch_session.add(record)
    scratch_session.flush()

    assert record.created_at == created_at
    assert record.updated_at == updated_at


def test_workspace_scoped_mixin_enforces_foreign_key(scratch_session) -> None:
    scratch_session.add(
        ScratchRecord(
            workspace_id=uuid4(),
            amount=Money(1, "EUR"),
            quantity=Decimal("1"),
        )
    )
    with pytest.raises(IntegrityError):
        scratch_session.flush()
    scratch_session.rollback()


def test_naming_convention_generates_expected_foreign_key_and_index_names(scratch_session) -> None:
    inspector = inspect(scratch_session.connection())

    foreign_keys = inspector.get_foreign_keys("scratch_records")
    indexes = inspector.get_indexes("scratch_records")

    assert foreign_keys[0]["name"] == "fk_scratch_records_workspace_id_workspaces"
    assert indexes[0]["name"] == "ix_scratch_records_workspace_id"


@given(
    value=st.decimals(
        min_value=Decimal("-99999999999999999999.999999999999999999"),
        max_value=Decimal("99999999999999999999.999999999999999999"),
        allow_nan=False,
        allow_infinity=False,
    ).filter(is_supported_decimal)
)
@settings(max_examples=100)
def test_decimal_type_bind_and_result_round_trip(value: Decimal) -> None:
    decimal_type = DecimalType()
    dialect = sqlite.dialect()

    bound_value = decimal_type.process_bind_param(value, dialect)
    result_value = decimal_type.process_result_value(bound_value, dialect)

    assert isinstance(bound_value, str)
    assert result_value == value


def test_decimal_type_persists_fixed_point_text_and_normalizes_negative_zero(
    scratch_session,
) -> None:
    workspace = ScratchWorkspace(name="Synthetic workspace")
    scratch_session.add(workspace)
    scratch_session.flush()
    quantities = [Decimal("0.00000001"), Decimal("1E+2"), Decimal("-0.000")]
    scratch_session.add_all(
        ScratchRecord(
            workspace_id=workspace.id,
            amount=Money(1, "EUR"),
            quantity=quantity,
        )
        for quantity in quantities
    )
    scratch_session.flush()

    stored_values = set(
        scratch_session.connection()
        .exec_driver_sql("SELECT quantity FROM scratch_records")
        .scalars()
    )

    assert stored_values == {"0.00000001", "100", "0"}


def test_decimal_type_rejects_ordering_comparisons() -> None:
    with pytest.raises(TypeError, match="ordering comparisons"):
        _ = ScratchRecord.quantity > Decimal("1")


def test_decimal_type_rejects_float_input_and_uses_postgres_numeric() -> None:
    decimal_type = DecimalType()
    with pytest.raises(TypeError, match="float"):
        decimal_type.process_bind_param(0.1, sqlite.dialect())  # type: ignore[arg-type]

    postgres_type = decimal_type.load_dialect_impl(postgresql.dialect())
    assert isinstance(postgres_type, Numeric)
    assert postgres_type.precision == 38
    assert postgres_type.scale == 18


def test_decimal_type_rejects_values_outside_portable_precision() -> None:
    with pytest.raises(ValueError, match="38 digits"):
        DecimalType().process_bind_param(Decimal("0.0000000000000000001"), sqlite.dialect())


def test_utc_datetime_rejects_naive_values_and_returns_aware_utc() -> None:
    utc_type = UtcDateTime()
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_type.process_bind_param(datetime(2026, 1, 1), sqlite.dialect())

    loaded = utc_type.process_result_value(
        datetime(2026, 1, 1),
        sqlite.dialect(),
    )
    assert loaded is not None
    assert loaded.utcoffset() is not None
