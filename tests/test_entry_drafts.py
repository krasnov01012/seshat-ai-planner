"""Общий контракт draft для ручной формы и будущего AI-пути."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from seshat.db.enums import EntryKind
from seshat.domain.entries import ManualEntryInput, preview_manual_entry
from seshat.domain.parsing import Freq, NormalizedEntry, Recurrence

NOW = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)


def test_manual_undated_task_is_an_explicit_valid_draft() -> None:
    preview = preview_manual_entry(
        ManualEntryInput(kind=EntryKind.TASK, title="  Ответить   работодателю  "),
        tz="Europe/Moscow",
        now_utc=NOW,
        confirmation_id=uuid.UUID("dfac00e1-a8f2-4be7-904a-79e28f804788"),
    )

    assert preview.draft.kind is EntryKind.TASK
    assert preview.draft.title == "Ответить работодателю"
    assert preview.draft.start_at_utc is None
    assert preview.draft.due_at_utc is None


def test_manual_routine_uses_the_common_timezone_and_rrule_normalization() -> None:
    preview = preview_manual_entry(
        ManualEntryInput(
            kind=EntryKind.ROUTINE,
            title="Тренировка",
            start=dt.datetime(2026, 8, 3, 19, 0),
            recurrence=Recurrence(freq=Freq.WEEKLY, byweekday=["mon", "wed", "fri"]),
        ),
        tz="Europe/Moscow",
        now_utc=NOW,
    )

    assert preview.draft.start_at_utc == dt.datetime(2026, 8, 3, 16, 0, tzinfo=dt.UTC)
    assert preview.draft.local_time == dt.time(19, 0)
    assert preview.draft.rrule == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_manual_event_without_start_is_rejected_before_preview() -> None:
    with pytest.raises(ValidationError, match="время начала"):
        ManualEntryInput(kind=EntryKind.EVENT, title="Созвон")


def test_confirmable_draft_rejects_non_utc_moment() -> None:
    with pytest.raises(ValidationError, match="должна быть в UTC"):
        NormalizedEntry(
            kind=EntryKind.EVENT,
            title="Созвон",
            start_at_utc=dt.datetime(2030, 8, 5, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))),
            tz="Europe/Moscow",
        )
