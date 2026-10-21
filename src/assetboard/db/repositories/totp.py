from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import UserSession
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode


class TotpRepository:
    def __init__(self, session: Session, user_id: UUID) -> None:
        self.session = session
        self.user_id = user_id

    def factor(self, *, lock: bool = False) -> TotpFactor | None:
        statement = select(TotpFactor).where(TotpFactor.user_id == self.user_id)
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def add_factor(self, **values: object) -> TotpFactor:
        factor = TotpFactor(user_id=self.user_id, **values)
        self.session.add(factor)
        return factor

    def clear(self) -> None:
        self.session.execute(
            delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == self.user_id)
        )
        self.session.execute(delete(TotpFactor).where(TotpFactor.user_id == self.user_id))

    def add_recovery_codes(self, hashes: list[str]) -> None:
        self.session.add_all(
            TotpRecoveryCode(user_id=self.user_id, code_hash=value) for value in hashes
        )

    def replace_recovery_codes(self, hashes: list[str]) -> None:
        self.session.execute(
            delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == self.user_id)
        )
        self.add_recovery_codes(hashes)

    def consume_recovery_code(self, code_hash: str) -> bool:
        code = self.session.scalar(
            select(TotpRecoveryCode)
            .where(
                TotpRecoveryCode.user_id == self.user_id, TotpRecoveryCode.code_hash == code_hash
            )
            .with_for_update()
        )
        if code is None:
            return False
        self.session.delete(code)
        return True

    def update_password_sessions(self, keep_hash: str, *, verified: bool) -> None:
        rows = self.session.scalars(
            select(UserSession).where(
                UserSession.user_id == self.user_id, UserSession.auth_method == "password"
            )
        )
        for row in rows:
            if row.id_hash == keep_hash:
                row.second_factor_verified = verified
            else:
                self.session.delete(row)

    def owns_session(self, session_hash: str) -> bool:
        row = self.session.get(UserSession, session_hash)
        return row is not None and row.user_id == self.user_id
