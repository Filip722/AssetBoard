"""The message and ``Retry-After`` for an attempt refused by the sign-in throttle.

Shown only when a throttle bucket actually blocked the attempt; an ordinary failure keeps its
own message. On sign-in the account bucket is keyed by the normalized email, counted the same
way for unknown emails and never reset by the owner, so it reveals no account (ADR-0012).
"""

from __future__ import annotations

import math
from datetime import datetime

from assetboard.i18n import gettext, ngettext


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


THROTTLED_LATER = _("Too many attempts. Try again later.")


def wait_seconds(until: datetime | None, now: datetime) -> int:
    """Whole seconds until the block ends (0 when unknown or already over)."""
    if until is None:
        return 0
    return max(0, math.ceil((until - now).total_seconds()))


def throttled_message(until: datetime | None, now: datetime, locale: str) -> str:
    """The wait in whole minutes (rounded up, at least 1), or "later" if it is unknown."""
    seconds = wait_seconds(until, now)
    if seconds <= 0:
        return gettext(THROTTLED_LATER, locale)
    minutes = max(1, math.ceil(seconds / 60))
    return ngettext(
        "Too many attempts. Try again in %(num)d minute.",
        "Too many attempts. Try again in %(num)d minutes.",
        minutes,
        locale,
    ) % {"num": minutes}


def retry_after(until: datetime | None, now: datetime) -> str | None:
    """The ``Retry-After`` header value in seconds, or ``None`` if the wait is unknown."""
    seconds = wait_seconds(until, now)
    return str(seconds) if seconds > 0 else None
