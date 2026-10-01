#!/usr/bin/env python3
"""Prepare local secrets and a TLS certificate without overwriting existing files."""

import argparse
import base64
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать .env и сертификат HTTPS/LDAPS для локального стенда. Существующие файлы сохраняются.",
                                     epilog="Пример: python3 scripts/prepare-local.py --root .\nВыход: .env, .local/tls/*, log/openssl_*.log. Код 0 — успех, 1 — ошибка.")
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
