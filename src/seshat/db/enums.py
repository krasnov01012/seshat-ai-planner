"""Перечисления домена.

Хранятся как строки с CHECK-ограничением, а не как нативные типы PostgreSQL:
добавить значение в VARCHAR + CHECK — это одна миграция, а в нативный ENUM —
отдельная операция с блокировкой. Домен ещё будет расширяться (фазы 2.1 и 2.2).
"""

from __future__ import annotations

from enum import StrEnum


class EntryKind(StrEnum):
    """Тип записи. Выводится кодом, а не моделью — см. docs/DECISIONS.md."""

    EVENT = "event"
    TASK = "task"
    ROUTINE = "routine"


class EntryStatus(StrEnum):
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class OccurrenceStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"
    MOVED = "moved"
    #: Не отправлено вовремя (простой) либо не закрыто после вечернего разбора.
    MISSED = "missed"


class NotificationKind(StrEnum):
    #: Предварительное («за час»). В тихие часы сдвигается.
    PRE = "pre"
    #: Основное, в момент самого события. Не сдвигается никогда, только беззвучно.
    MAIN = "main"
    #: Повтор для persistence=important. В тихие часы откладывается.
    REPEAT = "repeat"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    MISSED = "missed"
    FAILED = "failed"


class Persistence(StrEnum):
    NORMAL = "normal"
    IMPORTANT = "important"


class Importance(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class AiOutcome(StrEnum):
    OK = "ok"
    RETRY = "retry"
    FALLBACK = "fallback"
    FAILED = "failed"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"
    TZ_CHANGE = "tz_change"
