#!/usr/bin/env python3
"""Prepare TLS and add missing local secrets while preserving existing values."""

import argparse
import base64
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage()
        print(f"Ошибка аргументов: {message}")
        raise SystemExit(2)


def main() -> int:
    parser = ArgumentParser(description="Создать TLS-сертификат и добавить недостающие секреты заданий 1–2 в .env. Существующие значения и сертификаты сохраняются.",
                            epilog="Пример: python3 scripts/prepare-local.py --root .\nВыход: .env, .local/tls/*, log/openssl_*.log. Коды: 0 — успех, 1 — ошибка подготовки, 2 — ошибка аргументов.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Корень репозитория")
    parser.add_argument("--logs-dir", type=Path, default=Path("log"), help="Каталог журналов внешних команд (по умолчанию log)")
    args = parser.parse_args()
    root = args.root.resolve()
    print(f"Подготовка стенда: {root}")
    try:
        if not (root / "docker-compose.yaml").is_file():
            raise ValueError(f"Не найден {root / 'docker-compose.yaml'}")
        env = root / ".env"
        if not env.exists():
            values = {key: secrets.token_urlsafe(36) for key in (
                "KEYCLOAK_DB_PASSWORD", "PROFILE_DB_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "LDAP_ADMIN_PASSWORD", "AUTH_CLIENT_SECRET")}
            values["TOKEN_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            values.update(PUBLIC_ORIGIN="https://localhost:3443", YANDEX_ENABLED="false",
                          YANDEX_CLIENT_ID="not-configured", YANDEX_CLIENT_SECRET="not-configured")
            fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write("".join(f"{key}={value}\n" for key, value in values.items()))
            print(f"Создан {env} (0600); значения секретов не выводятся")
        else:
            print(f"Сохранён существующий {env}")
        existing = {line.split("=", 1)[0] for line in env.read_text().splitlines() if "=" in line}
        additions = {name: secrets.token_urlsafe(36) for name in (
            "CRM_DB_PASSWORD", "TELEMETRY_DB_PASSWORD", "AIRFLOW_DB_PASSWORD",
            "AIRFLOW_ADMIN_PASSWORD", "CLICKHOUSE_ETL_PASSWORD", "CLICKHOUSE_REPORTS_PASSWORD",
            "AIRFLOW_WEBSERVER_SECRET") if name not in existing}
        if "AIRFLOW_FERNET_KEY" not in existing:
            additions["AIRFLOW_FERNET_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        if additions:
            with env.open("a") as output:
                output.write("\n" + "".join(f"{name}={value}\n" for name, value in additions.items()))
            env.chmod(0o600)
            print(f"Добавлены отсутствующие настройки задания 2 в {env}; существующие значения сохранены")
        tls = root / ".local/tls"
        if not tls.exists():
            tls.mkdir(parents=True)
            print(f"Создан каталог {tls}")
        cert, key = tls / "localhost.crt", tls / "localhost.key"
        if cert.exists() != key.exists():
            raise ValueError(f"Неполная пара сертификат/ключ в {tls}; восстановите отсутствующий файл")
        if not cert.exists():
            if not shutil.which("openssl"):
                raise ValueError("Не найден openssl")
            logs = args.logs_dir.resolve()
            if not logs.exists():
                logs.mkdir(parents=True)
                print(f"Создан каталог {logs}")
            log = logs / f"openssl_{time.strftime('%Y%m%d_%H%M%S')}_localhost_{os.getpid()}.log"
            print(f"Создание {cert} и {key}; журнал: {log}")
            with log.open("w") as output:
                result = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "365",
                                         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                                         "-addext", "subjectAltName=DNS:localhost,DNS:ldap,IP:127.0.0.1",
                                         "-addext", "basicConstraints=critical,CA:TRUE"], stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise ValueError(f"Ошибка openssl; см. {log}")
            key.chmod(0o600)
            print(f"Установлены права 0600: {key}")
        else:
            print(f"Сохранены существующие сертификаты в {tls}")
        print("Подготовка завершена. Запуск: docker compose up -d --build")
        return 0
    except (OSError, ValueError) as error:
        print(f"Ошибка подготовки {root}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
