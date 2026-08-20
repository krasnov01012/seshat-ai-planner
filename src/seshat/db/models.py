"""Схема данных.

Правила, которые нельзя менять в одиночку (см. AGENTS.md):

* все моменты времени — `timestamptz` в UTC; таймзона пользователя хранится
  отдельно и применяется только при отображении и расчёте повторений;
* `user_id` есть во всех таблицах с первого дня, хотя пользователь один;
* `UNIQUE (occurrence_id, fire_at_utc, kind)` на `notifications` — единственная
  надёжная защита от дублей после перезапуска;
* удаление мягкое: `deleted_at` вместо `DELETE`, плюс запись в `audit_log`.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seshat.db.base import Base
from seshat.db.enums import (
    AiOutcome,
    AuditAction,
    EntryKind,
    EntryStatus,
    Importance,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
    Persistence,
)


def _enum(python_enum: type, name: str) -> Enum:
    """VARCHAR + CHECK вместо нативного ENUM — проще расширять миграцией."""
    return Enum(
        python_enum, name=name, native_enum=False, values_callable=lambda e: [i.value for i in e]
    )


#: Момент времени в UTC. Наивных datetime в схеме нет по построению.
UtcDateTime = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())

    settings: Mapped[UserSettings] = relationship(back_populates="user", uselist=False)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    #: IANA-строка. Будет меняться при переезде — см. docs/ARCHITECTURE.md.
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")

    #: Тихие часы в местном времени. Бот не будит ни в каком режиме.
    quiet_from: Mapped[dt.time] = mapped_column(Time, default=dt.time(23, 0))
    quiet_to: Mapped[dt.time] = mapped_column(Time, default=dt.time(8, 0))

    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    digest_time: Mapped[dt.time] = mapped_column(Time, default=dt.time(8, 30))
    review_time: Mapped[dt.time] = mapped_column(Time, default=dt.time(21, 0))

    #: 0 = понедельник, как в datetime.weekday().
    week_start: Mapped[int] = mapped_column(SmallInteger, default=0)
    default_snooze_min: Mapped[int] = mapped_column(Integer, default=60)
    confirm_before_save: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="settings")

    __table_args__ = (
        CheckConstraint("week_start BETWEEN 0 AND 6", name="week_start_range"),
        CheckConstraint("default_snooze_min > 0", name="snooze_positive"),
    )


class TzChange(Base):
    """Журнал смен часового пояса.

    Нужен, чтобы объяснить задним числом, почему рутина сдвинулась,
    и чтобы отследить, разобрал ли владелец список будущих событий.
    """

    __tablename__ = "tz_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tz_from: Mapped[str] = mapped_column(String(64))
    tz_to: Mapped[str] = mapped_column(String(64))
    changed_at_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())
    entries_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)


class Entry(Base):
    """Запись: событие, задача или рутина."""

    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    kind: Mapped[EntryKind] = mapped_column(_enum(EntryKind, "entry_kind"))
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)

    start_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    due_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_min: Mapped[int | None] = mapped_column(Integer)

    #: RRULE по RFC 5545. Раскрывается материализатором на 14 дней вперёд.
    rrule: Mapped[str | None] = mapped_column(String(500))

    #: Таймзона, в которой запись создана, и исходное локальное время как оно
    #: было сказано. Без этой пары после переезда невозможно пересчитать рутины:
    #: из одного UTC-момента не восстановить, что имелось в виду «8 утра».
    tz: Mapped[str] = mapped_column(String(64))
    local_time: Mapped[dt.time | None] = mapped_column(Time)

    importance: Mapped[Importance] = mapped_column(
        _enum(Importance, "importance"), default=Importance.NORMAL
    )
    persistence: Mapped[Persistence] = mapped_column(
        _enum(Persistence, "persistence"), default=Persistence.NORMAL
    )
    #: Смещения до начала. [] = defaults из конфига; [0] = только основное.
    reminders_min_before: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list)

    status: Mapped[EntryStatus] = mapped_column(
        _enum(EntryStatus, "entry_status"), default=EntryStatus.ACTIVE
    )
    google_event_id: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now()
    )
    #: Мягкое удаление. Физический DELETE в проекте не используется.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    occurrences: Mapped[list[Occurrence]] = relationship(back_populates="entry")

    __table_args__ = (
        CheckConstraint(
            "duration_min IS NULL OR (duration_min > 0 AND duration_min <= 1440)",
            name="duration_sane",
        ),
        # Рутина без правила повторения — это не рутина.
        CheckConstraint("kind <> 'routine' OR rrule IS NOT NULL", name="routine_has_rrule"),
        Index("ix_entries_active", "user_id", "status", postgresql_where=deleted_at.is_(None)),
    )


class Occurrence(Base):
    """Конкретный экземпляр записи.

    Разовое событие — одна строка. Рутина — по строке на каждое срабатывание
    в горизонте материализации. Пропуск одного дня — это статус одной строки,
    а не исключение внутри правила повторения.
    """

    __tablename__ = "occurrences"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("entries.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    planned_at_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    status: Mapped[OccurrenceStatus] = mapped_column(
        _enum(OccurrenceStatus, "occurrence_status"), default=OccurrenceStatus.PENDING
    )
    moved_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    entry: Mapped[Entry] = relationship(back_populates="occurrences")
    notifications: Mapped[list[Notification]] = relationship(back_populates="occurrence")

    __table_args__ = (
        # Повторный прогон материализатора не должен создавать второй экземпляр
        # на тот же момент.
        UniqueConstraint("entry_id", "planned_at_utc", name="uq_occurrence_slot"),
        Index("ix_occurrences_day", "user_id", "planned_at_utc"),
    )


class Notification(Base):
    """Одно физическое уведомление.

    Планировщик задач в памяти процесса не используется: состояние живёт здесь,
    поэтому расписание восстанавливается после перезапуска по построению.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("occurrences.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    fire_at_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    kind: Mapped[NotificationKind] = mapped_column(_enum(NotificationKind, "notification_kind"))
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, "notification_status"), default=NotificationStatus.PENDING
    )
    #: Ушло ли беззвучно из-за тихих часов — попадёт в утренний дайджест.
    silent: Mapped[bool] = mapped_column(Boolean, default=False)

    sent_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    digest_included_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    digest_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    digest_next_attempt_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at_utc: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: Только короткий нормализованный код, без текста исключения и секретов.
    last_error_code: Mapped[str | None] = mapped_column(String(64))

    occurrence: Mapped[Occurrence] = relationship(back_populates="notifications")

    __table_args__ = (
        # Ключевая гарантия проекта: повторный запуск планировщика получит
        # конфликт вместо второго уведомления.
        UniqueConstraint("occurrence_id", "fire_at_utc", "kind", name="uq_notification_slot"),
        CheckConstraint("attempt_count >= 0", name="notification_attempt_count_nonnegative"),
        CheckConstraint(
            "digest_attempt_count >= 0", name="notification_digest_attempt_count_nonnegative"
        ),
        # Индекс под запрос tick-лупа: WHERE status='pending' AND fire_at_utc <= now()
        Index("ix_notifications_due", "status", "fire_at_utc"),
        Index(
            "ix_notifications_digest",
            "user_id",
            "silent",
            "digest_included_at_utc",
            "digest_next_attempt_at_utc",
        ),
    )


class ActiveContext(Base):
    """Контекст активного напоминания.

    Позволяет понять «через 20 минут» без указания, о чём речь.
    Одна строка на пользователя: контекст всегда один — последнее напоминание.
    """

    __tablename__ = "active_context"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    occurrence_id: Mapped[int] = mapped_column(ForeignKey("occurrences.id", ondelete="CASCADE"))
    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL")
    )
    expires_at_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)


class AiCall(Base):
    """Журнал обращений к модели: латентность, расход токенов, исход."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    model: Mapped[str] = mapped_column(String(120))
    latency_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[AiOutcome] = mapped_column(_enum(AiOutcome, "ai_outcome"))
    needed_clarification: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())


class AuditLog(Base):
    """Что и когда изменилось. Вместе с мягким удалением даёт откат."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[AuditAction] = mapped_column(_enum(AuditAction, "audit_action"))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=func.now())

    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)
