"""Слой предметной области.

Единственное место, где живёт бизнес-логика. `api` и `bot` — адаптеры над ним
и своей логики не содержат. Это и делает проект API-first: любая возможность,
доступная боту, доступна и по HTTP, потому что реализация у них общая.
"""

from seshat.domain.entries import (
    ConfirmationConflictError,
    CreateEntryResult,
    EntryDraft,
    EntryPreview,
    EntryValidationError,
    ManualEntryInput,
    create_entry,
    prepare_manual_entry,
    preview_entry,
    preview_manual_entry,
)
from seshat.domain.timezones import (
    TimezoneChangePreview,
    TimezoneChangeResult,
    TimezoneConflictError,
    TimezoneReviewDecision,
    TimezoneReviewItem,
    TimezoneReviewResult,
    TimezoneScheduleResult,
    confirm_timezone_change,
    find_pending_timezone_change,
    list_timezone_reviews,
    preview_timezone_change,
    rebuild_timezone_horizon,
    review_timezone_entry,
)
from seshat.domain.users import (
    DomainError,
    UnknownTimezoneError,
    find_user_by_telegram_id,
    get_or_create_user,
    get_settings,
    is_quiet,
    update_quiet_hours,
    validate_tz,
)

__all__ = [
    "ConfirmationConflictError",
    "CreateEntryResult",
    "DomainError",
    "EntryDraft",
    "EntryPreview",
    "EntryValidationError",
    "ManualEntryInput",
    "TimezoneChangePreview",
    "TimezoneChangeResult",
    "TimezoneConflictError",
    "TimezoneReviewDecision",
    "TimezoneReviewItem",
    "TimezoneReviewResult",
    "TimezoneScheduleResult",
    "UnknownTimezoneError",
    "confirm_timezone_change",
    "create_entry",
    "find_pending_timezone_change",
    "find_user_by_telegram_id",
    "get_or_create_user",
    "get_settings",
    "is_quiet",
    "list_timezone_reviews",
    "prepare_manual_entry",
    "preview_entry",
    "preview_manual_entry",
    "preview_timezone_change",
    "rebuild_timezone_horizon",
    "review_timezone_entry",
    "update_quiet_hours",
    "validate_tz",
]
