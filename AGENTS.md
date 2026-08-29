# SESHAT PORTFOLIO — правила публичного snapshot

Этот репозиторий — публичная employer-facing sanitized case study. Он не
является operational source живого Seshat и может намеренно отставать от
private runtime.

## Public boundary

- Не переносить сюда private Git history, production Compose/config/deploy,
  server coordinates, абсолютные локальные пути, логи, базы, backups, реальные
  Telegram IDs/messages или owner identifiers.
- Реальные bot/API/OAuth tokens, пароли, ключи и provider responses запрещены.
  `.env.example` содержит только плейсхолдеры.
- Обновление из private source выполняется как отдельный sanitized export с
  повторным аудитом дерева и всей достижимой public history перед push.
- Не делать force-push и не переписывать clean public history без отдельного
  решения владельца или подтверждённого credential incident.

## Truthful case-study boundary

- Не выдавать snapshot за текущий production runtime. Указывать evidence date
  и честно различать completed, current, planned и owner-gated работу.
- Не завышать multi-user/product maturity, acceptance или самостоятельное
  авторство кода.
- Сохранять явное AI-assisted disclosure и интервью-защищаемые формулировки.
- Архитектурные схемы показывают компоненты и trust boundaries без deployment
  coordinates и приватной инфраструктуры.

## Verification

- Для изменений Python запускать проверки из `README.md` и CI:

  ```bash
  uv sync --frozen
  uv run ruff check .
  uv run ruff format --check .
  uv run pytest
  ```

- Интеграционные тесты используют отдельную PostgreSQL; production database и
  внешние Telegram/NVIDIA credentials не применяются.
- Для documentation-only hygiene change обязательны secret/history scan,
  `git diff --check` и проверка, что `origin/main` обновляется fast-forward.
- Этот репозиторий не разворачивает live Seshat и не выдаёт deploy keys.
