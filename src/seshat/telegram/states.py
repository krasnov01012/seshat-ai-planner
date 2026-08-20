"""Состояния FSM для создания записи."""

from aiogram.fsm.state import State, StatesGroup


class ManualForm(StatesGroup):
    kind = State()
    date = State()
    time = State()
    duration = State()
    recurrence = State()
    title = State()


class Confirmation(StatesGroup):
    ready = State()
    edit_kind = State()
    edit_date = State()
    edit_time = State()
    edit_duration = State()
    edit_recurrence = State()
    edit_title = State()
    edit_reminders = State()


class TextClarification(StatesGroup):
    waiting = State()
