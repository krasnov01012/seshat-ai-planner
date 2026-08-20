"""Настройки пользователя: таймзона и тихие часы."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status

from seshat.api.deps import AuthDep, SessionDep, SettingsDep
from seshat.api.schemas import (
    QuietHoursIn,
    SettingsOut,
    TimezoneConfirmIn,
    TimezoneConfirmOut,
    TimezoneIn,
    TimezonePreviewOut,
    TimezoneReviewIn,
    TimezoneReviewItemOut,
    TimezoneReviewOut,
)
from seshat.domain import users
from seshat.domain.scheduling import ReminderDefaults
from seshat.domain.timezones import (
    TimezoneConflictError,
    confirm_timezone_change,
    list_timezone_reviews,
    preview_timezone_change,
    rebuild_timezone_horizon,
    review_timezone_entry,
)

router = APIRouter(prefix="/v1", tags=["settings"], dependencies=[Depends(AuthDep.dependency)])


def _reminder_defaults(config: SettingsDep) -> ReminderDefaults:
    return ReminderDefaults(
        event_pre_min=config.default_event_reminders_min,
        task_pre_min=config.default_task_reminders_min,
        task_morning_local=config.default_task_morning_local,
        routine_pre_min=config.default_routine_reminders_min,
    )


async def _current_user_id(session: SessionDep, config: SettingsDep) -> int:
    """Владелец бота.

    Пока пользователь один: multi-tenant приходит в фазе 2.6 второго роадмапа.
    Схема уже несёт `user_id` везде, поэтому переход не потребует миграции данных.
    """
    user = await users.get_or_create_user(
        session, config.telegram_owner_id, default_tz=config.default_tz
    )
    return user.id


@router.get("/settings", response_model=SettingsOut, summary="Текущие настройки")
async def read_settings(session: SessionDep, config: SettingsDep) -> SettingsOut:
    user_id = await _current_user_id(session, config)
    return SettingsOut.model_validate(await users.get_settings(session, user_id))


@router.put("/settings/quiet-hours", response_model=SettingsOut, summary="Тихие часы")
async def set_quiet_hours(
    payload: QuietHoursIn, session: SessionDep, config: SettingsDep
) -> SettingsOut:
    user_id = await _current_user_id(session, config)
    updated = await users.update_quiet_hours(session, user_id, payload.quiet_from, payload.quiet_to)
    return SettingsOut.model_validate(updated)


@router.post(
    "/settings/timezone/preview",
    response_model=TimezonePreviewOut,
    summary="Подготовить подтверждение смены часового пояса",
)
async def preview_timezone(
    payload: TimezoneIn, session: SessionDep, config: SettingsDep
) -> TimezonePreviewOut:
    user_id = await _current_user_id(session, config)
    preview = await preview_timezone_change(
        session,
        user_id,
        payload.tz,
        now_utc=dt.datetime.now(dt.UTC),
    )
    return TimezonePreviewOut.model_validate(preview.model_dump())


@router.put(
    "/settings/timezone",
    response_model=TimezoneConfirmOut,
    summary="Подтвердить смену часового пояса",
    description=(
        "Рутины после смены следуют новому местному времени, разовые события "
        "и дедлайны сохраняют абсолютный момент. Спорные случаи владелец "
        "разбирает отдельно — см. docs/ARCHITECTURE.md."
    ),
)
async def set_timezone(
    payload: TimezoneConfirmIn, session: SessionDep, config: SettingsDep
) -> TimezoneConfirmOut:
    user_id = await _current_user_id(session, config)
    try:
        result = await confirm_timezone_change(
            session,
            user_id,
            payload.tz,
            expected_tz_from=payload.expected_tz_from,
            confirmation_id=payload.confirmation_id,
            now_utc=dt.datetime.now(dt.UTC),
        )
    except TimezoneConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await rebuild_timezone_horizon(
        session,
        user_id,
        now_utc=dt.datetime.now(dt.UTC),
        horizon_days=config.materialize_horizon_days,
        defaults=_reminder_defaults(config),
    )
    return TimezoneConfirmOut.model_validate(result.model_dump())


@router.get(
    "/timezone-changes/{change_id}/reviews",
    response_model=list[TimezoneReviewItemOut],
    summary="Неразобранные события после смены таймзоны",
)
async def timezone_reviews(
    change_id: int, session: SessionDep, config: SettingsDep
) -> list[TimezoneReviewItemOut]:
    user_id = await _current_user_id(session, config)
    items = await list_timezone_reviews(session, user_id, change_id)
    return [TimezoneReviewItemOut.model_validate(item.model_dump()) for item in items]


@router.put(
    "/timezone-changes/{change_id}/reviews/{entry_id}",
    response_model=TimezoneReviewOut,
    summary="Решить, как перенести одну будущую запись",
)
async def review_timezone(
    change_id: int,
    entry_id: int,
    payload: TimezoneReviewIn,
    session: SessionDep,
    config: SettingsDep,
) -> TimezoneReviewOut:
    user_id = await _current_user_id(session, config)
    try:
        result = await review_timezone_entry(
            session,
            user_id,
            change_id,
            entry_id,
            payload.decision,
            now_utc=dt.datetime.now(dt.UTC),
        )
    except TimezoneConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await rebuild_timezone_horizon(
        session,
        user_id,
        now_utc=dt.datetime.now(dt.UTC),
        horizon_days=config.materialize_horizon_days,
        defaults=_reminder_defaults(config),
    )
    return TimezoneReviewOut.model_validate(result.model_dump())
