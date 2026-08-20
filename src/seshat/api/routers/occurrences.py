"""Действия над отдельными экземплярами расписания."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from seshat.api.deps import AuthDep, SessionDep, SettingsDep
from seshat.domain import users
from seshat.domain.delivery import (
    ActiveReactionResult,
    ReactionContextSource,
    acknowledge_occurrence,
    react_to_reaction_context,
)
from seshat.domain.reactions import (
    ReactionAction,
    ReactionResult,
)
from seshat.domain.reactions import (
    apply_notification_action as apply_domain_notification_action,
)
from seshat.domain.scheduling import ReminderDefaults, skip_occurrence

router = APIRouter(prefix="/v1", tags=["occurrences"], dependencies=[Depends(AuthDep.dependency)])


class OccurrenceActionOut(BaseModel):
    occurrence_id: int
    changed: bool


class OccurrenceAcknowledgeOut(BaseModel):
    occurrence_id: int
    repeats_cancelled: int


class ReactionContextResolveIn(BaseModel):
    notification_id: int | None = Field(default=None, gt=0, le=2_147_483_647)


class ReactionContextResolveOut(BaseModel):
    resolved: bool
    source: ReactionContextSource | None
    occurrence_id: int | None
    notification_id: int | None
    repeats_cancelled: int


def _context_out(result: ActiveReactionResult) -> ReactionContextResolveOut:
    return ReactionContextResolveOut(
        resolved=result.reacted,
        source=result.source,
        occurrence_id=result.occurrence_id,
        notification_id=result.notification_id,
        repeats_cancelled=result.cancelled,
    )


@router.post(
    "/reaction-context/resolve",
    response_model=ReactionContextResolveOut,
    summary="Разрешить явный reply или активный контекст напоминания",
)
async def resolve_reaction_context(
    payload: ReactionContextResolveIn,
    session: SessionDep,
    config: SettingsDep,
) -> ReactionContextResolveOut:
    user = await users.get_or_create_user(
        session,
        config.telegram_owner_id,
        default_tz=config.default_tz,
    )
    result = await react_to_reaction_context(
        session,
        user.id,
        notification_id=payload.notification_id,
        reacted_at_utc=dt.datetime.now(dt.UTC),
    )
    return _context_out(result)


class NotificationActionIn(BaseModel):
    action: ReactionAction
    target_at_utc: dt.datetime | None = None

    @model_validator(mode="after")
    def _move_needs_target(self) -> NotificationActionIn:
        if (self.action is ReactionAction.MOVE) != (self.target_at_utc is not None):
            raise ValueError("target_at_utc is required only for move")
        if self.target_at_utc is not None and (
            self.target_at_utc.tzinfo is None or self.target_at_utc.utcoffset() is None
        ):
            raise ValueError("target_at_utc must include a UTC offset")
        return self


class NotificationActionOut(BaseModel):
    source_notification_id: int
    occurrence_id: int
    action: ReactionAction
    changed: bool
    status: str
    cancelled_notifications: int
    scheduled_notification_id: int | None
    successor_occurrence_id: int | None
    target_at_utc: dt.datetime | None
    moved_count: int


def _reminder_defaults(config: SettingsDep) -> ReminderDefaults:
    return ReminderDefaults(
        event_pre_min=config.default_event_reminders_min,
        task_pre_min=config.default_task_reminders_min,
        task_morning_local=config.default_task_morning_local,
        routine_pre_min=config.default_routine_reminders_min,
    )


def _action_out(result: ReactionResult) -> NotificationActionOut:
    return NotificationActionOut(
        source_notification_id=result.source_notification_id,
        occurrence_id=result.occurrence_id,
        action=result.action,
        changed=result.changed,
        status=result.status.value,
        cancelled_notifications=result.cancelled_notifications,
        scheduled_notification_id=result.scheduled_notification_id,
        successor_occurrence_id=result.successor_occurrence_id,
        target_at_utc=result.target_at_utc,
        moved_count=result.moved_count,
    )


@router.post(
    "/notifications/{notification_id}/actions",
    response_model=NotificationActionOut,
    summary="Обработать действие по отправленному уведомлению без AI",
)
async def apply_notification_action(
    notification_id: int,
    payload: NotificationActionIn,
    session: SessionDep,
    config: SettingsDep,
) -> NotificationActionOut:
    user = await users.get_or_create_user(
        session,
        config.telegram_owner_id,
        default_tz=config.default_tz,
    )
    now = dt.datetime.now(dt.UTC)
    result = await apply_domain_notification_action(
        session,
        user.id,
        notification_id,
        payload.action,
        reacted_at_utc=now,
        target_at_utc=payload.target_at_utc,
        defaults=_reminder_defaults(config),
    )
    return _action_out(result)


@router.post(
    "/occurrences/{occurrence_id}/skip",
    response_model=OccurrenceActionOut,
    summary="Пропустить один экземпляр рутины",
)
async def skip_routine_occurrence(
    occurrence_id: int,
    session: SessionDep,
    config: SettingsDep,
) -> OccurrenceActionOut:
    user = await users.get_or_create_user(
        session,
        config.telegram_owner_id,
        default_tz=config.default_tz,
    )
    result = await skip_occurrence(
        session,
        user_id=user.id,
        occurrence_id=occurrence_id,
        now_utc=dt.datetime.now(dt.UTC),
    )
    return OccurrenceActionOut(
        occurrence_id=result.occurrence_id,
        changed=result.changed,
    )


@router.post(
    "/occurrences/{occurrence_id}/acknowledge",
    response_model=OccurrenceAcknowledgeOut,
    summary="Подтвердить реакцию и остановить повторы",
)
async def acknowledge(
    occurrence_id: int,
    session: SessionDep,
    config: SettingsDep,
) -> OccurrenceAcknowledgeOut:
    user = await users.get_or_create_user(
        session,
        config.telegram_owner_id,
        default_tz=config.default_tz,
    )
    result = await acknowledge_occurrence(
        session,
        user.id,
        occurrence_id,
        reacted_at_utc=dt.datetime.now(dt.UTC),
    )
    return OccurrenceAcknowledgeOut(
        occurrence_id=result.occurrence_id,
        repeats_cancelled=result.cancelled,
    )


__all__ = ["router"]
