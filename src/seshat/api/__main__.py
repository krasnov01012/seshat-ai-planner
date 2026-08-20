"""Запуск API: `python -m seshat.api`."""

from __future__ import annotations

import uvicorn

from seshat.config import load_settings
from seshat.logging_setup import setup_logging


def main() -> None:
    config = load_settings()
    setup_logging(config.log_level)
    uvicorn.run(
        "seshat.api.app:create_app",
        factory=True,
        host=config.api_host,
        port=config.api_port,
        # Заголовкам X-Forwarded-* доверяем только когда перед сервисом реально
        # стоит прокси. Иначе клиент подделает свой IP и протокол — а нам это
        # понадобится, когда сервис переедет на поддомен за HTTPS.
        proxy_headers=config.trust_proxy_headers,
        forwarded_allow_ips=config.forwarded_allow_ips,
        # Логи уже структурные и с вычисткой секретов — свой конфиг uvicorn не нужен.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
