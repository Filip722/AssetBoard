from __future__ import annotations

from decimal import Decimal, InvalidOperation

SUPPORTED_LOCALES = frozenset({"en", "pl"})


def normalize_locale(locale: str) -> str:
    language = locale.replace("_", "-").split("-", maxsplit=1)[0].lower()
    if language not in SUPPORTED_LOCALES:
        raise ValueError(f"Unsupported locale: {locale}")
    return language


def resolve_locale(
    *,
    user_locale: str | None = None,
    workspace_locale: str | None = None,
    accept_language: str | None = None,
    default_locale: str = "en",
) -> str:
    for locale in (user_locale, workspace_locale):
        if locale:
            try:
                return normalize_locale(locale)
            except ValueError:
                pass

    preferred: list[tuple[Decimal, int, str]] = []
    for position, language in enumerate((accept_language or "").split(",")):
        parts = language.strip().split(";")
        tag = parts[0].strip()
        quality = Decimal("1")
        if len(parts) > 1:
            parameter = parts[1].strip()
            if parameter.startswith("q="):
                try:
                    quality = Decimal(parameter[2:])
                except InvalidOperation:
                    continue
        if not quality.is_finite() or quality <= 0 or quality > 1:
            continue
        if tag == "*":
            preferred.append((quality, -position, "en"))
            continue
        try:
            normalized = normalize_locale(tag)
        except ValueError:
            continue
        preferred.append((quality, -position, normalized))

    if preferred:
        preferred.sort(reverse=True)
        return preferred[0][2]
    try:
        return normalize_locale(default_locale)
    except ValueError:
        return "en"
