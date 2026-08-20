"""Текущий локальный день пользователя."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from seshat.api.deps import AuthDep, SessionDep, SettingsDep
from seshat.db.enums import EntryKind, OccurrenceStatus
from seshat.domain import users
from seshat.domain.day import get_my_day

router = APIRouter(prefix="/v1", tags=["day"], dependencies=[Depends(AuthDep.dependency)])


class MyDayItemOut(BaseModel):
    occurrence_id: int
    entry_id: int
    kind: EntryKind
    title: str
    planned_at_utc: dt.datetime
    planned_at_local: dt.datetime
    status: OccurrenceStatus


class MyDayNightItemOut(BaseModel):
    notification_id: int
    occurrence_id: int
    title: str
    sent_at_utc: dt.datetime
    sent_at_local: dt.datetime


class MyDayOut(BaseModel):
    local_date: dt.date
    tz: str
    missed: list[MyDayItemOut]
    items: list[MyDayItemOut]
    night: list[MyDayNightItemOut]


@router.get("/my-day", response_model=MyDayOut, summary="Показать текущий локальный день")
async def my_day(session: SessionDep, config: SettingsDep) -> MyDayOut:
    user = await users.get_or_create_user(
        session,
        config.telegram_owner_id,
        default_tz=config.default_tz,
    )
    result = await get_my_day(session, user.id, now_utc=dt.datetime.now(dt.UTC))
    return MyDayOut(
        local_date=result.local_date,
        tz=result.tz,
        missed=[MyDayItemOut.model_validate(item, from_attributes=True) for item in result.missed],
        items=[MyDayItemOut.model_validate(item, from_attributes=True) for item in result.items],
        night=[
            MyDayNightItemOut.model_validate(item, from_attributes=True) for item in result.night
        ],
    )


__all__ = ["router"]
