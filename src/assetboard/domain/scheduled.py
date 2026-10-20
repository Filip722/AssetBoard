from enum import StrEnum


class AmountMode(StrEnum):
    FIXED = "fixed"
    ESTIMATE = "estimate"


class AmountState(StrEnum):
    ESTIMATED = "estimated"
    CONFIRMED = "confirmed"


class OccurrenceStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    SKIPPED = "skipped"


class ItemAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    ARCHIVE = "archive"
    DELETE = "delete"
