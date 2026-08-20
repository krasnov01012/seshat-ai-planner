"""Проверки HTTP-интерфейса.

API — полноценный интерфейс, а не довесок к боту, поэтому проверяется отдельно
и на настоящей базе: контракт, аутентификация и трансляция доменных ошибок.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
import pytest_asyncio
import time_machine
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from seshat.api.app import create_app
from seshat.config import Settings
from seshat.db.enums import EntryKind, NotificationKind, NotificationStatus, OccurrenceStatus
from seshat.db.models import (
    ActiveContext,
    AuditLog,
    Entry,
    Notification,
    Occurrence,
    User,
    UserSettings,
)
from seshat.domain.ai import PreparationSource, TextClarification, TextManualFallback, TextReady
from seshat.domain.nim import NimResult
from seshat.domain.parsing import Intent, NormalizedEntry, ParsedPlan

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)

TOKEN = "test-token-value"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class StubTextPreparationService:
    def __init__(self) -> None:
        self.result = TextClarification("stub")
        self.calls: list[str] = []

    async def prepare_text(self, _session, *, text: str, **_kwargs):
        self.calls.append(text)
        return self.result


@pytest.fixture
def text_service() -> StubTextPreparationService:
    return StubTextPreparationService()


def _manual_event() -> dict[str, object]:
    return {
        "kind": "event",
        "title": "Собеседование с А2",
        "start": "2030-08-05T15:00:00",
        "reminders_min_before": [60],
    }


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="123456789:" + "AAtest-token-value-for-tests-only-xxxx",
        telegram_owner_id=123456789,
        database_url=os.environ["TEST_DATABASE_URL"],
        api_token=TOKEN,
        env="test",
    )


@pytest_asyncio.fixture
async def client(session_factory, text_service: StubTextPreparationService) -> AsyncClient:
    """Клиент поверх ASGI.

    Приложение получает фабрику сессий теста, поэтому его `commit()` остаётся
    внутри транзакции, откатываемой после теста.
    """
    config = _settings()
    app = create_app(config)
    app.state.settings = config
    app.state.session_factory = session_factory
    app.state.text_preparation_service = text_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_needs_no_auth(client: AsyncClient) -> None:
    """Монитор не должен знать секретов, чтобы проверить живость."""
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["env"] == "test"


async def test_ready_reports_database_and_migration(client: AsyncClient) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "ok"


async def test_protected_route_rejects_missing_token(client: AsyncClient) -> None:
    r = await client.get("/v1/settings")
    assert r.status_code == 401


async def test_protected_route_rejects_wrong_token(client: AsyncClient) -> None:
    r = await client.get("/v1/settings", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_protected_route_rejects_wrong_scheme(client: AsyncClient) -> None:
    r = await client.get("/v1/settings", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


async def test_entry_preview_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/v1/entries/preview", json=_manual_event())
    assert r.status_code == 401


def _text_ready() -> TextReady:
    plan = ParsedPlan(
        intent=Intent.CREATE,
        title="Собеседование",
        start=dt.datetime(2030, 8, 5, 15, 0),
        confidence=0.95,
    )
    nim = NimResult(
        plan=plan,
        model="test-model",
        latency_ms=12,
        prompt_tokens=10,
        completion_tokens=5,
        attempts=1,
        used_fallback=False,
    )
    return TextReady(
        entry=NormalizedEntry(
            kind=EntryKind.EVENT,
            title="Собеседование",
            start_at_utc=dt.datetime(2030, 8, 5, 12, 0, tzinfo=dt.UTC),
            tz="Europe/Moscow",
            local_time=dt.time(15, 0),
        ),
        source=PreparationSource.AI,
        nim=nim,
    )


async def test_text_preview_returns_typed_ready_and_can_be_confirmed(
    client: AsyncClient,
    text_service: StubTextPreparationService,
    session,
) -> None:
    text_service.result = _text_ready()

    preview = await client.post(
        "/v1/entries/parse",
        headers=AUTH,
        json={"text": "Завтра в 15:00 собеседование"},
    )
    assert preview.status_code == 200
    assert preview.json()["status"] == "ready"

    confirmed = await client.post("/v1/entries", headers=AUTH, json=preview.json())
    assert confirmed.status_code == 200
    assert confirmed.json()["created"] is True
    assert await session.scalar(select(func.count()).select_from(Entry)) == 1
    assert text_service.calls == [
        "Завтра в 15:00 собеседование",
        "Завтра в 15:00 собеседование",
    ]


async def test_text_preview_clarifies_or_offers_manual_form(
    client: AsyncClient,
    text_service: StubTextPreparationService,
) -> None:
    clarification = await client.post(
        "/v1/entries/parse", headers=AUTH, json={"text": "В следующую пятницу утром"}
    )
    assert clarification.json()["status"] == "clarification"

    from seshat.domain.ai import ManualFallbackReason

    text_service.result = TextManualFallback(ManualFallbackReason.AI_UNAVAILABLE)
    fallback = await client.post("/v1/entries/parse", headers=AUTH, json={"text": "Завтра встреча"})
    assert fallback.json()["status"] == "manual_fallback"


async def test_settings_are_created_on_first_read(client: AsyncClient) -> None:
    """Первое обращение заводит пользователя и настройки по умолчанию."""
    r = await client.get("/v1/settings", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["tz"] == "Europe/Moscow"
    assert body["quiet_from"] == "23:00:00"
    assert body["quiet_to"] == "08:00:00"
    assert body["confirm_before_save"] is True


async def test_reading_settings_twice_does_not_duplicate_user(client: AsyncClient) -> None:
    first = await client.get("/v1/settings", headers=AUTH)
    second = await client.get("/v1/settings", headers=AUTH)
    assert first.json()["user_id"] == second.json()["user_id"]


async def test_quiet_hours_can_be_changed(client: AsyncClient) -> None:
    r = await client.put(
        "/v1/settings/quiet-hours",
        headers=AUTH,
        json={"quiet_from": "22:30", "quiet_to": "07:15"},
    )
    assert r.status_code == 200
    assert r.json()["quiet_from"] == "22:30:00"
    assert r.json()["quiet_to"] == "07:15:00"


async def test_equal_quiet_bounds_are_rejected_as_400(client: AsyncClient) -> None:
    """Доменная ошибка обязана превращаться в 400, а не в 500."""
    r = await client.put(
        "/v1/settings/quiet-hours",
        headers=AUTH,
        json={"quiet_from": "23:00", "quiet_to": "23:00"},
    )
    assert r.status_code == 400
    assert "совпадать" in r.json()["detail"]


async def test_quiet_hours_reject_utc_offset(client: AsyncClient) -> None:
    response = await client.put(
        "/v1/settings/quiet-hours",
        headers=AUTH,
        json={"quiet_from": "23:00+03:00", "quiet_to": "08:00+03:00"},
    )
    assert response.status_code == 422


async def test_timezone_change_is_recorded(client: AsyncClient) -> None:
    """Preview не меняет настройки, подтверждение меняет и журналирует."""
    preview = await client.post(
        "/v1/settings/timezone/preview",
        headers=AUTH,
        json={"tz": "Europe/Amsterdam"},
    )
    assert preview.status_code == 200
    card = preview.json()
    assert card["tz_from"] == "Europe/Moscow"
    assert card["tz_to"] == "Europe/Amsterdam"

    before = await client.get("/v1/settings", headers=AUTH)
    assert before.json()["tz"] == "Europe/Moscow"

    r = await client.put(
        "/v1/settings/timezone",
        headers=AUTH,
        json={
            "tz": card["tz_to"],
            "expected_tz_from": card["tz_from"],
            "confirmation_id": card["confirmation_id"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["change"]["tz_from"] == "Europe/Moscow"
    assert body["change"]["tz_to"] == "Europe/Amsterdam"
    assert body["change"]["entries_reviewed"] is True
    assert body["applied"] is True

    current = await client.get("/v1/settings", headers=AUTH)
    assert current.json()["tz"] == "Europe/Amsterdam"

    replay = await client.put(
        "/v1/settings/timezone",
        headers=AUTH,
        json={
            "tz": card["tz_to"],
            "expected_tz_from": card["tz_from"],
            "confirmation_id": card["confirmation_id"],
        },
    )
    assert replay.status_code == 200
    assert replay.json()["applied"] is False


async def test_timezone_requires_confirmation(client: AsyncClient) -> None:
    legacy = await client.put(
        "/v1/settings/timezone", headers=AUTH, json={"tz": "Europe/Amsterdam"}
    )
    assert legacy.status_code == 422


async def test_unknown_timezone_is_rejected_as_400(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/settings/timezone/preview", headers=AUTH, json={"tz": "Europe/Atlantis"}
    )
    assert r.status_code == 400


async def test_same_timezone_is_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/settings/timezone/preview", headers=AUTH, json={"tz": "Europe/Moscow"}
    )
    assert r.status_code == 400


async def test_timezone_future_entry_review_contract(client: AsyncClient) -> None:
    entry_preview = await client.post("/v1/entries/preview", headers=AUTH, json=_manual_event())
    created = await client.post("/v1/entries", headers=AUTH, json=entry_preview.json())
    assert created.status_code == 200
    entry_id = created.json()["entry"]["id"]

    preview = await client.post(
        "/v1/settings/timezone/preview",
        headers=AUTH,
        json={"tz": "Europe/Amsterdam"},
    )
    card = preview.json()
    confirmed = await client.put(
        "/v1/settings/timezone",
        headers=AUTH,
        json={
            "tz": card["tz_to"],
            "expected_tz_from": card["tz_from"],
            "confirmation_id": card["confirmation_id"],
        },
    )
    assert confirmed.status_code == 200
    change_id = confirmed.json()["change"]["id"]
    assert confirmed.json()["review_remaining"] == 1
    assert confirmed.json()["next_review"]["entry_id"] == entry_id

    reviews = await client.get(f"/v1/timezone-changes/{change_id}/reviews", headers=AUTH)
    assert reviews.status_code == 200
    assert reviews.json()[0]["entry_id"] == entry_id

    decided = await client.put(
        f"/v1/timezone-changes/{change_id}/reviews/{entry_id}",
        headers=AUTH,
        json={"decision": "keep_local"},
    )
    assert decided.status_code == 200
    assert decided.json()["review_remaining"] == 0


async def test_openapi_is_published(client: AsyncClient) -> None:
    """Контракт публикуется: на нём будут строиться будущие клиенты."""
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/v1/settings" in paths
    assert "/v1/settings/timezone/preview" in paths
    assert "/v1/timezone-changes/{change_id}/reviews" in paths
    assert "/v1/entries/preview" in paths
    assert "/v1/entries" in paths
    assert "/v1/occurrences/{occurrence_id}/skip" in paths
    assert "/v1/occurrences/{occurrence_id}/acknowledge" in paths
    assert "/v1/notifications/{notification_id}/actions" in paths
    assert "/v1/reaction-context/resolve" in paths
    assert "/v1/my-day" in paths
    assert "/health" in paths


async def test_my_day_requires_auth_and_surfaces_missed(client: AsyncClient, session) -> None:
    assert (await client.get("/v1/my-day")).status_code == 401
    settings = await client.get("/v1/settings", headers=AUTH)
    user_id = settings.json()["user_id"]
    entry = Entry(
        user_id=user_id,
        kind=EntryKind.TASK,
        title="Пропущенная задача",
        due_at_utc=dt.datetime.now(dt.UTC),
        tz="Europe/Moscow",
    )
    session.add(entry)
    await session.flush()
    session.add(
        Occurrence(
            entry_id=entry.id,
            user_id=user_id,
            planned_at_utc=dt.datetime.now(dt.UTC),
            status=OccurrenceStatus.MISSED,
        )
    )
    await session.flush()

    response = await client.get("/v1/my-day", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["missed"][0]["title"] == "Пропущенная задача"
    assert "night" in response.json()


async def test_entry_preview_does_not_write_entry_or_audit(client: AsyncClient, session) -> None:
    r = await client.post("/v1/entries/preview", headers=AUTH, json=_manual_event())

    assert r.status_code == 200
    body = r.json()
    assert body["draft"]["kind"] == "event"
    assert body["draft"]["start_at_utc"] == "2030-08-05T12:00:00Z"
    assert await session.scalar(select(func.count()).select_from(User)) == 0
    assert await session.scalar(select(func.count()).select_from(UserSettings)) == 0
    assert await session.scalar(select(func.count()).select_from(Entry)) == 0
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_confirmed_entry_is_created_with_audit(client: AsyncClient, session) -> None:
    with time_machine.travel("2030-08-04 09:00:00+00:00", tick=False):
        preview = await client.post("/v1/entries/preview", headers=AUTH, json=_manual_event())
        payload = preview.json()

        created = await client.post("/v1/entries", headers=AUTH, json=payload)

    assert created.status_code == 200
    body = created.json()
    assert body["created"] is True
    assert body["entry"]["title"] == "Собеседование с А2"
    assert await session.scalar(select(func.count()).select_from(Entry)) == 1
    assert await session.scalar(select(func.count()).select_from(Occurrence)) == 1
    assert await session.scalar(select(func.count()).select_from(Notification)) == 2
    audit = (await session.execute(select(AuditLog))).scalar_one()
    assert audit.entity_id == body["entry"]["id"]
    assert audit.payload["confirmation_id"] == payload["confirmation_id"]


async def test_api_skips_only_one_routine_occurrence(client: AsyncClient, session) -> None:
    settings = await client.get("/v1/settings", headers=AUTH)
    user_id = settings.json()["user_id"]
    routine = Entry(
        user_id=user_id,
        kind=EntryKind.ROUTINE,
        title="Тренировка",
        start_at_utc=dt.datetime(2030, 8, 5, 5, tzinfo=dt.UTC),
        rrule="FREQ=DAILY",
        tz="Europe/Moscow",
        local_time=dt.time(8, 0),
    )
    session.add(routine)
    await session.flush()
    first = Occurrence(
        entry_id=routine.id,
        user_id=user_id,
        planned_at_utc=dt.datetime(2030, 8, 5, 5, tzinfo=dt.UTC),
    )
    second = Occurrence(
        entry_id=routine.id,
        user_id=user_id,
        planned_at_utc=dt.datetime(2030, 8, 6, 5, tzinfo=dt.UTC),
    )
    session.add_all([first, second])
    await session.flush()
    pending = Notification(
        occurrence_id=first.id,
        user_id=user_id,
        fire_at_utc=first.planned_at_utc,
        kind=NotificationKind.MAIN,
    )
    repeat = Notification(
        occurrence_id=second.id,
        user_id=user_id,
        fire_at_utc=second.planned_at_utc + dt.timedelta(minutes=15),
        kind=NotificationKind.REPEAT,
    )
    session.add_all([pending, repeat])
    await session.flush()

    response = await client.post(f"/v1/occurrences/{first.id}/skip", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"occurrence_id": first.id, "changed": True}
    await session.refresh(first)
    await session.refresh(second)
    await session.refresh(pending)
    assert first.status is OccurrenceStatus.SKIPPED
    assert second.status is OccurrenceStatus.PENDING
    assert pending.status is NotificationStatus.CANCELLED

    acknowledged = await client.post(f"/v1/occurrences/{second.id}/acknowledge", headers=AUTH)
    assert acknowledged.status_code == 200
    assert acknowledged.json() == {
        "occurrence_id": second.id,
        "repeats_cancelled": 1,
    }
    await session.refresh(repeat)
    assert repeat.status is NotificationStatus.CANCELLED


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("complete", OccurrenceStatus.DONE),
        ("snooze", OccurrenceStatus.PENDING),
        ("skip", OccurrenceStatus.SKIPPED),
        ("move", OccurrenceStatus.MOVED),
    ],
)
async def test_notification_actions_have_http_contract(
    client: AsyncClient,
    session,
    action: str,
    expected_status: OccurrenceStatus,
) -> None:
    settings = await client.get("/v1/settings", headers=AUTH)
    user_id = settings.json()["user_id"]
    now = dt.datetime(2030, 8, 5, 12, tzinfo=dt.UTC)
    entry = Entry(
        user_id=user_id,
        kind=EntryKind.EVENT,
        title="API action",
        start_at_utc=now,
        tz="Europe/Moscow",
    )
    session.add(entry)
    await session.flush()
    occurrence = Occurrence(entry_id=entry.id, user_id=user_id, planned_at_utc=now)
    session.add(occurrence)
    await session.flush()
    source = Notification(
        occurrence_id=occurrence.id,
        user_id=user_id,
        fire_at_utc=now,
        kind=NotificationKind.MAIN,
        status=NotificationStatus.SENT,
        sent_at_utc=now,
    )
    session.add(source)
    await session.flush()
    payload: dict[str, str] = {"action": action}
    if action == "move":
        payload["target_at_utc"] = "2030-08-06T12:00:00Z"

    with time_machine.travel(now + dt.timedelta(minutes=1), tick=False):
        response = await client.post(
            f"/v1/notifications/{source.id}/actions",
            headers=AUTH,
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source_notification_id"] == source.id
    assert body["action"] == action
    assert body["status"] == expected_status.value
    await session.refresh(occurrence)
    assert occurrence.status is expected_status
    if action == "snooze":
        assert body["scheduled_notification_id"] is not None
        assert body["target_at_utc"] == "2030-08-05T13:01:00Z"
    if action == "move":
        assert body["successor_occurrence_id"] is not None
        assert body["moved_count"] == 1


async def test_notification_action_requires_auth_and_valid_move_target(
    client: AsyncClient,
) -> None:
    path = "/v1/notifications/1/actions"
    assert (await client.post(path, json={"action": "complete"})).status_code == 401
    response = await client.post(path, headers=AUTH, json={"action": "move"})
    assert response.status_code == 422
    response = await client.post(
        path,
        headers=AUTH,
        json={"action": "move", "target_at_utc": "2030-08-06T12:00:00"},
    )
    assert response.status_code == 422


async def test_reaction_context_api_prefers_explicit_and_preserves_newer_active(
    client: AsyncClient,
    session,
) -> None:
    settings = await client.get("/v1/settings", headers=AUTH)
    user_id = settings.json()["user_id"]
    now = dt.datetime(2030, 8, 5, 12, tzinfo=dt.UTC)
    entries = [
        Entry(
            user_id=user_id,
            kind=EntryKind.EVENT,
            title=title,
            start_at_utc=now + dt.timedelta(hours=index + 1),
            tz="Europe/Moscow",
        )
        for index, title in enumerate(("Older", "Newer"))
    ]
    session.add_all(entries)
    await session.flush()
    occurrences = [
        Occurrence(
            entry_id=entry.id,
            user_id=user_id,
            planned_at_utc=entry.start_at_utc,
        )
        for entry in entries
    ]
    session.add_all(occurrences)
    await session.flush()
    sources = [
        Notification(
            occurrence_id=occurrence.id,
            user_id=user_id,
            fire_at_utc=now,
            kind=NotificationKind.MAIN,
            status=NotificationStatus.SENT,
            sent_at_utc=now,
            telegram_message_id=800 + index,
        )
        for index, occurrence in enumerate(occurrences)
    ]
    repeats = [
        Notification(
            occurrence_id=occurrence.id,
            user_id=user_id,
            fire_at_utc=now + dt.timedelta(minutes=15),
            kind=NotificationKind.REPEAT,
        )
        for occurrence in occurrences
    ]
    future_pending = Notification(
        occurrence_id=occurrences[0].id,
        user_id=user_id,
        fire_at_utc=now + dt.timedelta(hours=1),
        kind=NotificationKind.PRE,
    )
    session.add_all([*sources, *repeats, future_pending])
    await session.flush()
    context = ActiveContext(
        user_id=user_id,
        occurrence_id=occurrences[1].id,
        notification_id=sources[1].id,
        expires_at_utc=now + dt.timedelta(hours=3),
    )
    session.add(context)
    await session.flush()

    with time_machine.travel(now + dt.timedelta(minutes=1), tick=False):
        unknown = await client.post(
            "/v1/reaction-context/resolve",
            headers=AUTH,
            json={"notification_id": 2_000_000_000},
        )
        unsent = await client.post(
            "/v1/reaction-context/resolve",
            headers=AUTH,
            json={"notification_id": future_pending.id},
        )
        explicit = await client.post(
            "/v1/reaction-context/resolve",
            headers=AUTH,
            json={"notification_id": sources[0].id},
        )

    assert unknown.status_code == 200 and unknown.json()["resolved"] is False
    assert unsent.status_code == 200 and unsent.json()["resolved"] is False
    assert explicit.status_code == 200
    assert explicit.json() == {
        "resolved": True,
        "source": "explicit_notification",
        "occurrence_id": occurrences[0].id,
        "notification_id": sources[0].id,
        "repeats_cancelled": 1,
    }
    await session.refresh(repeats[0])
    await session.refresh(repeats[1])
    await session.refresh(context)
    assert repeats[0].status is NotificationStatus.CANCELLED
    assert repeats[1].status is NotificationStatus.PENDING
    assert context.occurrence_id == occurrences[1].id

    with time_machine.travel(now + dt.timedelta(minutes=2), tick=False):
        active = await client.post(
            "/v1/reaction-context/resolve",
            headers=AUTH,
            json={},
        )

    assert active.status_code == 200
    assert active.json()["source"] == "active"
    assert active.json()["occurrence_id"] == occurrences[1].id
    await session.refresh(repeats[1])
    assert repeats[1].status is NotificationStatus.CANCELLED
    session.expire_all()
    assert await session.get(ActiveContext, user_id) is None


async def test_reaction_context_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/v1/reaction-context/resolve", json={})
    assert response.status_code == 401


async def test_double_confirm_is_an_idempotent_replay(client: AsyncClient, session) -> None:
    preview = await client.post("/v1/entries/preview", headers=AUTH, json=_manual_event())
    payload = preview.json()

    first = await client.post("/v1/entries", headers=AUTH, json=payload)
    second = await client.post("/v1/entries", headers=AUTH, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["entry"]["id"] == second.json()["entry"]["id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert await session.scalar(select(func.count()).select_from(Entry)) == 1
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1


async def test_confirmation_id_with_changed_draft_is_rejected(client: AsyncClient) -> None:
    preview = await client.post("/v1/entries/preview", headers=AUTH, json=_manual_event())
    payload = preview.json()
    first = await client.post("/v1/entries", headers=AUTH, json=payload)
    assert first.status_code == 200

    payload["draft"]["title"] = "Другая запись"
    conflict = await client.post("/v1/entries", headers=AUTH, json=payload)

    assert conflict.status_code == 400
    assert "не соответствует" in conflict.json()["detail"]


async def test_first_confirmation_cannot_invent_a_normalized_draft(client: AsyncClient) -> None:
    preview = await client.post("/v1/entries/preview", headers=AUTH, json=_manual_event())
    payload = preview.json()
    payload["draft"]["title"] = "Подменённая запись"

    response = await client.post("/v1/entries", headers=AUTH, json=payload)

    assert response.status_code == 400
    assert "не соответствует" in response.json()["detail"]


async def test_manual_past_date_is_a_domain_error(client: AsyncClient) -> None:
    payload = _manual_event()
    payload["start"] = "2020-08-05T15:00:00"

    r = await client.post("/v1/entries/preview", headers=AUTH, json=payload)

    assert r.status_code == 400
    assert "в прошлом" in r.json()["detail"]


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (dt.time(23, 30), True),
        (dt.time(2, 0), True),
        (dt.time(7, 59), True),
        (dt.time(8, 0), False),
        (dt.time(12, 0), False),
        (dt.time(22, 59), False),
    ],
)
def test_quiet_interval_crosses_midnight(moment: dt.time, expected: bool) -> None:
    """Тихие часы почти всегда идут через полночь — наивное сравнение тут врёт."""
    from seshat.domain import is_quiet

    assert is_quiet(moment, dt.time(23, 0), dt.time(8, 0)) is expected
