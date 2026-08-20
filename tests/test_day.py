"""Read model «Мой день» на реальном PostgreSQL."""

from __future__ import annotations

import datetime as dt
import os

import pytest

from seshat.db.enums import (
    EntryKind,
    EntryStatus,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
)
from seshat.db.models import Entry, Notification, Occurrence, User, UserSettings
from seshat.domain.day import MyDay, MyDayItem, MyDayNightItem, get_my_day
from seshat.telegram.day import my_day_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)


async def test_my_day_uses_local_dst_boundaries_and_puts_missed_first(session) -> None:
    user = User(telegram_id=7001)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, tz="Europe/Amsterdam"))

    visible = Entry(user_id=user.id, kind=EntryKind.EVENT, title="Видимое", tz="Europe/Amsterdam")
    missed = Entry(user_id=user.id, kind=EntryKind.TASK, title="Пропущенное", tz="Europe/Amsterdam")
    deleted = Entry(
        user_id=user.id,
        kind=EntryKind.EVENT,
        title="Удалённое",
        tz="Europe/Amsterdam",
        deleted_at=dt.datetime(2026, 10, 24, tzinfo=dt.UTC),
    )
    inactive = Entry(
        user_id=user.id,
        kind=EntryKind.EVENT,
        title="Отменённое",
        tz="Europe/Amsterdam",
        status=EntryStatus.CANCELLED,
    )
    session.add_all([visible, missed, deleted, inactive])
    await session.flush()
    occurrences = [
        Occurrence(
            entry_id=visible.id,
            user_id=user.id,
            planned_at_utc=dt.datetime(2026, 10, 25, 2, 30, tzinfo=dt.UTC),
        ),
        Occurrence(
            entry_id=missed.id,
            user_id=user.id,
            planned_at_utc=dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC),
            status=OccurrenceStatus.MISSED,
        ),
        Occurrence(
            entry_id=deleted.id,
            user_id=user.id,
            planned_at_utc=dt.datetime(2026, 10, 25, 8, 0, tzinfo=dt.UTC),
        ),
        Occurrence(
            entry_id=inactive.id,
            user_id=user.id,
            planned_at_utc=dt.datetime(2026, 10, 25, 9, 0, tzinfo=dt.UTC),
        ),
        Occurrence(
            entry_id=visible.id,
            user_id=user.id,
            planned_at_utc=dt.datetime(2026, 10, 25, 4, 30, tzinfo=dt.UTC),
            status=OccurrenceStatus.MOVED,
        ),
    ]
    session.add_all(occurrences)
    await session.flush()
    session.add(
        Notification(
            occurrence_id=occurrences[1].id,
            user_id=user.id,
            fire_at_utc=dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC),
            kind=NotificationKind.MAIN,
            status=NotificationStatus.SENT,
            silent=True,
            sent_at_utc=dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC),
        )
    )
    await session.flush()

    day = await get_my_day(
        session,
        user.id,
        now_utc=dt.datetime(2026, 10, 25, 12, tzinfo=dt.UTC),
    )

    assert day.local_date == dt.date(2026, 10, 25)
    assert [item.title for item in day.missed] == ["Пропущенное"]
    assert [item.title for item in day.items] == ["Видимое"]
    assert day.missed[0].planned_at_local.strftime("%H:%M %z") == "02:30 +0200"
    assert day.items[0].planned_at_local.strftime("%H:%M %z") == "03:30 +0100"
    assert [item.title for item in day.night] == ["Пропущенное"]


def test_my_day_presenter_is_html_safe_and_missed_is_first() -> None:
    zone = dt.timezone(dt.timedelta(hours=3))
    missed = MyDayItem(
        occurrence_id=1,
        entry_id=1,
        kind=EntryKind.TASK,
        title="Оплатить <счёт> & чек",
        planned_at_utc=dt.datetime(2026, 8, 3, 7, tzinfo=dt.UTC),
        planned_at_local=dt.datetime(2026, 8, 3, 10, tzinfo=zone),
        status=OccurrenceStatus.MISSED,
    )
    current = MyDayItem(
        occurrence_id=2,
        entry_id=2,
        kind=EntryKind.EVENT,
        title="Созвон",
        planned_at_utc=dt.datetime(2026, 8, 3, 9, tzinfo=dt.UTC),
        planned_at_local=dt.datetime(2026, 8, 3, 12, tzinfo=zone),
        status=OccurrenceStatus.PENDING,
    )
    night = MyDayNightItem(
        notification_id=3,
        occurrence_id=1,
        title="Ночью <тихо>",
        sent_at_utc=dt.datetime(2026, 8, 3, 0, tzinfo=dt.UTC),
        sent_at_local=dt.datetime(2026, 8, 3, 3, tzinfo=zone),
    )

    text = my_day_text(
        MyDay(
            local_date=dt.date(2026, 8, 3),
            tz="Europe/Moscow",
            missed=(missed,),
            items=(current,),
            night=(night,),
        )
    )

    assert text.index("Пришло ночью") < text.index("Пропущено")
    assert text.index("Пропущено") < text.index("Сегодня")
    assert "Ночью &lt;тихо&gt;" in text
    assert "Оплатить &lt;счёт&gt; &amp; чек" in text
    assert "⚠️ 10:00" in text


def test_empty_my_day_has_neutral_text() -> None:
    assert (
        my_day_text(MyDay(dt.date(2026, 8, 3), "Europe/Moscow", (), ()))
        == "На сегодня записей нет."
    )


def test_night_digest_text_stays_below_telegram_limit() -> None:
    zone = dt.timezone(dt.timedelta(hours=3))
    night = tuple(
        MyDayNightItem(
            notification_id=index,
            occurrence_id=index,
            title="'" * 500,
            sent_at_utc=dt.datetime(2026, 8, 3, 0, index, tzinfo=dt.UTC),
            sent_at_local=dt.datetime(2026, 8, 3, 3, index, tzinfo=zone),
        )
        for index in range(3)
    )

    text = my_day_text(
        MyDay(
            local_date=dt.date(2026, 8, 3),
            tz="Europe/Moscow",
            missed=(),
            items=(),
            night=night,
        )
    )

    assert len(text) <= 3_800
    assert text.count("🌙") == 3
    assert "&#x27;" in text
