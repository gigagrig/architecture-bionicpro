#!/usr/bin/env python3
"""Bind synthetic CRM customers to real local Keycloak subjects and seed telemetry."""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage()
        print(f"Ошибка аргументов: {message}")
        raise SystemExit(2)


def main() -> int:
    parser = ArgumentParser(description="Заполнить учебные CRM и телеметрию для user1/user2 из локального Keycloak.",
        epilog="Пример: python /opt/airflow/seed_demo.py --days 3\n"
               "Результат: строки в учебных PostgreSQL, JSON с интервалом в stdout. Файлы не создаются. "
               "Коды выхода: 0 — успех; 1 — ошибка; 2 — неверные аргументы. Подключения и пароли берутся из окружения Compose.")
    parser.add_argument("--days", type=int, default=3, help="Количество последних завершённых дней UTC (1–31)")
    parser.add_argument("--output", type=Path, help="Дополнительно сохранить JSON с датами в указанный файл")
    args = parser.parse_args()
    print("Начало заполнения учебных источников CRM и телеметрии", flush=True)
    try:
        if not 1 <= args.days <= 31:
            raise ValueError("days must be between 1 and 31")
        origin = os.environ["KEYCLOAK_INTERNAL_ROOT"].rstrip("/")
        end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=args.days)
        print(f"Интервал UTC: [{start.isoformat()}, {end.isoformat()}); дней: {args.days}", flush=True)
        subjects = {}
        with httpx.Client(timeout=15, headers={"X-Forwarded-Proto": "https"}) as client:
            result = client.post(origin + "/realms/master/protocol/openid-connect/token", data={
                "client_id": "admin-cli", "grant_type": "password", "username": "admin",
                "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"]})
            if result.status_code != 200:
                raise ValueError("Keycloak authentication failed")
            headers = {"Authorization": "Bearer " + result.json()["access_token"]}
            for username in ("user1", "user2"):
                result = client.get(origin + "/admin/realms/reports-realm/users", headers=headers,
                                    params={"username": username, "exact": "true"})
                if result.status_code != 200 or len(result.json()) != 1:
                    raise ValueError("Expected demo account not found")
                subjects[username] = result.json()[0]["id"]
        print("Найдены ID двух учебных аккаунтов; создание CRM и телеметрии", flush=True)
        with psycopg.connect(os.environ["CRM_DATABASE_URL"]) as crm:
            for username, subject in subjects.items():
                crm.execute("INSERT INTO customers VALUES (%s,%s) ON CONFLICT (customer_id) DO UPDATE SET keycloak_subject=EXCLUDED.keycloak_subject",
                            (username, subject))
                devices = ["demo-user1-hand", "demo-user1-spare"] if username == "user1" else ["demo-user2-hand"]
                for device in devices:
                    crm.execute("INSERT INTO prostheses VALUES (%s,'BionicPRO Demo') ON CONFLICT DO NOTHING", (device,))
                    exists = crm.execute("SELECT 1 FROM ownership WHERE prosthesis_id=%s", (device,)).fetchone()
                    if not exists:
                        crm.execute("INSERT INTO ownership VALUES (%s,%s,%s,NULL)", (device, username, datetime(2026, 1, 1, tzinfo=timezone.utc)))
            crm.execute("INSERT INTO export_checkpoint VALUES (TRUE,%s) ON CONFLICT (id) DO UPDATE SET closed_through=GREATEST(export_checkpoint.closed_through,EXCLUDED.closed_through)", (end,))
        events = 0
        with psycopg.connect(os.environ["TELEMETRY_DATABASE_URL"]) as telemetry:
            for offset in range(args.days):
                day = start + timedelta(days=offset)
                for device in ("demo-user1-hand", "demo-user2-hand"):
                    for hour in (8, 12, 18):
                        occurred = day + timedelta(hours=hour)
                        telemetry.execute("INSERT INTO telemetry VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (f"{device}-{occurred.isoformat()}", device, occurred, 10, 80.0 + hour, 100.0 - hour, hour == 18))
                        events += 1
            telemetry.execute("INSERT INTO export_checkpoint VALUES (TRUE,%s) ON CONFLICT (id) DO UPDATE SET closed_through=GREATEST(export_checkpoint.closed_through,EXCLUDED.closed_through)", (end,))
        result = json.dumps({"from": start.date().isoformat(), "to": end.date().isoformat(), "demo_event_attempts": events})
        print(result)
        if args.output:
            print(f"Сохранение интервала в {args.output.resolve()}")
            args.output.write_text(result + "\n")
            print(f"Создан или обновлён файл {args.output.resolve()}")
        print("Заполнение завершено; существующие события сохранены", flush=True)
        return 0
    except (OSError, ValueError, KeyError, httpx.HTTPError, psycopg.Error) as error:
        # Hide database DSNs and HTTP bodies, which could include credentials.
        print(f"Ошибка заполнения источников: {type(error).__name__}. Проверьте окружение и доступность сервисов.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
