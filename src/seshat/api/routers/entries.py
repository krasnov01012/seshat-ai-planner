"""Preview и подтверждённое создание записей."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request

from seshat.api.deps import AuthDep, SessionDep, SettingsDep
from seshat.api.schemas import (
    EntryConfirmIn,
    EntryCreateOut,
    EntryDraftSchema,
    EntryOut,
    EntryPreviewOut,
    ManualEntryIn,
    TextClarificationOut,
    TextEntryIn,
    TextManualFallbackOut,
    TextPreparationOut,
    TextReadyOut,
)
from seshat.domain import users
from seshat.domain.ai import TextClarification, TextManualFallback, TextReady
from seshat.domain.entries import (
    ConfirmationConflictError,
    prepare_manual_entry,
    preview_manual_entry,
)
from seshat.domain.planning import create_planned_entry
from seshat.domain.scheduling import ReminderDefaults

router = APIRouter(prefix="/v1", tags=["entries"], dependencies=[Depends(AuthDep.dependency)])


async def _current_user(session: SessionDep, config: SettingsDep):
    return await users.get_or_create_user(
        session, config.telegram_owner_id, default_tz=config.default_tz
    )


@router.post(
    "/entries/parse",
    response_model=TextPreparationOut,
    summary="Подготовить карточку из обычного текста",
)
async def preview_text(
    payload: TextEntryIn,
    request: Request,
    session: SessionDep,
    config: SettingsDep,
) -> TextPreparationOut:
    user = await _current_user(session, config)
    user_settings = await users.get_settings(session, user.id)
    # Журнал AI пишется отдельной транзакцией, поэтому новый user должен стать
    # виден до первого ai_calls INSERT.
    await session.commit()
    result = await request.app.state.text_preparation_service.prepare_text(
        session,
        user_id=user.id,
        text=payload.text,
        tz=user_settings.tz,
        now_utc=dt.datetime.now(dt.UTC),
    )
    if isinstance(result, TextReady):
        return TextReadyOut(
            confirmation_id=uuid.uuid4(),
            text=payload.text,
            draft=EntryDraftSchema.model_validate(result.entry.model_dump()),
        )
    if isinstance(result, TextClarification):
        return TextClarificationOut(prompt="Уточни план: укажи точные дату и время.")
    if isinstance(result, TextManualFallback):
        return TextManualFallbackOut(prompt="Не смог разобрать. Заполни запись через ручную форму.")
    raise TypeError("неизвестный результат подготовки текста")


@router.post(
    "/entries/preview",
    response_model=EntryPreviewOut,
    summary="Подготовить карточку ручной записи",
)
async def preview_manual(
    payload: ManualEntryIn, session: SessionDep, config: SettingsDep
) -> EntryPreviewOut:
    user = await users.find_user_by_telegram_id(session, config.telegram_owner_id)
    tz = config.default_tz
    if user is not None:
        tz = (await users.get_settings(session, user.id)).tz
    preview = preview_manual_entry(
        payload,
        tz=tz,
        now_utc=dt.datetime.now(dt.UTC),
    )
    return EntryPreviewOut(
        confirmation_id=preview.confirmation_id,
        manual=payload,
        draft=EntryDraftSchema.model_validate(preview.draft.model_dump()),
    )


@router.post(
    "/entries",
    response_model=EntryCreateOut,
    summary="Подтвердить и создать запись",
)
async def confirm_entry(
    payload: EntryConfirmIn,
    request: Request,
    session: SessionDep,
    config: SettingsDep,
) -> EntryCreateOut:
    now_utc = dt.datetime.now(dt.UTC)
    user = await _current_user(session, config)
    user_settings = await users.get_settings(session, user.id)
    if payload.manual is not None:
        draft = prepare_manual_entry(
            payload.manual,
            tz=user_settings.tz,
            now_utc=now_utc,
        )
    else:
        await session.commit()
        prepared = await request.app.state.text_preparation_service.prepare_text(
            session,
            user_id=user.id,
            text=payload.text or "",
            tz=user_settings.tz,
            now_utc=now_utc,
        )
        if not isinstance(prepared, TextReady):
            raise ConfirmationConflictError("текст больше не образует подтверждаемую карточку")
        draft = prepared.entry
    if draft.model_dump() != payload.draft.model_dump():
        raise ConfirmationConflictError(
            "карточка подтверждения не соответствует полям ручной формы"
        )
    result = await create_planned_entry(
        session,
        user.id,
        draft,
        confirmation_id=payload.confirmation_id,
        now_utc=now_utc,
        horizon_days=config.materialize_horizon_days,
        late_lookback_minutes=config.late_delivery_threshold_min,
        defaults=ReminderDefaults(
            event_pre_min=config.default_event_reminders_min,
            task_pre_min=config.default_task_reminders_min,
            task_morning_local=config.default_task_morning_local,
            routine_pre_min=config.default_routine_reminders_min,
        ),
    )
    return EntryCreateOut(
        created=result.created,
        entry=EntryOut.model_validate(result.entry),
    )
