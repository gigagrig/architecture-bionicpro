#!/usr/bin/env python3
"""Configure the local Yandex broker or export a sanitised, re-importable realm."""
import argparse
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Настроить Яндекс ID из .env или экспортировать локальный realm без секретов.",
        epilog="Примеры: python3 scripts/keycloak-config.py configure-yandex; python3 scripts/keycloak-config.py export. Код 0 — успех, 1 — ошибка.")
    parser.add_argument("action", choices=["configure-yandex", "export"], help="Действие с локальным Keycloak")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Корень репозитория")
    parser.add_argument("--output", type=Path, help="JSON экспорта (по умолчанию keycloak/keycloak-results-export.json)")
    args = parser.parse_args()
    root = args.root.resolve()
    print(f"Начало {args.action}; корень: {root}")
    try:
        env_path = root / ".env"
        values = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if line and not line.startswith("#"))
        origin = values["PUBLIC_ORIGIN"].rstrip("/")
        context = ssl.create_default_context(cafile=str(root / ".local/tls/localhost.crt"))
        def request(method: str, path: str, payload=None, token=None, form=False):
            headers = {}
            data = None
            if payload is not None:
                data = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, context=context, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        auth = request("POST", "/identity/realms/master/protocol/openid-connect/token", {
            "client_id": "admin-cli", "grant_type": "password", "username": "admin",
            "password": values["KEYCLOAK_ADMIN_PASSWORD"]}, form=True)
        token = auth["access_token"]
        base = "/identity/admin/realms/reports-realm"
        if args.action == "configure-yandex":
            if values.get("YANDEX_ENABLED", "false").lower() != "true":
                raise ValueError("Укажите YANDEX_ENABLED=true в .env")
            if any(values.get(key, "not-configured") in ("", "not-configured") for key in ("YANDEX_CLIENT_ID", "YANDEX_CLIENT_SECRET")):
                raise ValueError("Заполните YANDEX_CLIENT_ID и YANDEX_CLIENT_SECRET в .env")
            provider = request("GET", base + "/identity-provider/instances/yandex", token=token)
            provider["enabled"] = True
            provider["config"]["clientId"] = values["YANDEX_CLIENT_ID"]
            provider["config"]["clientSecret"] = values["YANDEX_CLIENT_SECRET"]
            request("PUT", base + "/identity-provider/instances/yandex", provider, token=token)
            print("Изменён провайдер yandex в realm reports-realm. Пересоздайте bionicpro-auth для обновления кнопки входа.")
        else:
            output = (args.output or root / "keycloak/keycloak-results-export.json").resolve()
            realm = request("POST", base + "/partial-export?exportClients=true&exportGroupsAndRoles=true", token=token)
            # Keep only reproducible synthetic accounts from the starter repository.
            original = json.loads((root / "keycloak/realm-export.json").read_text())
            realm["users"] = original["users"]
            for user in realm["users"]:
                user["requiredActions"] = ["CONFIGURE_TOTP"]
            for client in realm["clients"]:
                if client["clientId"] == "bionicpro-auth":
                    client["secret"] = "${AUTH_CLIENT_SECRET}"
                    client["redirectUris"] = ["${PUBLIC_ORIGIN}/auth/callback"]
                elif client.get("secret") == "**********":
                    # A masked Admin API value is not a reusable client secret.
                    client.pop("secret")
            for component in realm.get("components", {}).get("org.keycloak.storage.UserStorageProvider", []):
                component["config"]["bindCredential"] = ["${LDAP_ADMIN_PASSWORD}"]
            for provider in realm.get("identityProviders", []):
                if provider["alias"] == "yandex":
                    provider["enabled"] = "${YANDEX_ENABLED}"
                    provider["config"]["clientId"] = "${YANDEX_CLIENT_ID}"
                    provider["config"]["clientSecret"] = "${YANDEX_CLIENT_SECRET}"
            # Drop deploy-specific URLs, replacing public origin anywhere it occurs.
            text = json.dumps(realm, ensure_ascii=False, indent=2).replace(origin, "${PUBLIC_ORIGIN}")
            # A secret must never be committed by this export.
            for name in ("AUTH_CLIENT_SECRET", "LDAP_ADMIN_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD",
                         "KEYCLOAK_DB_PASSWORD", "PROFILE_DB_PASSWORD", "TOKEN_ENCRYPTION_KEY", "YANDEX_CLIENT_SECRET"):
                value = values.get(name, "")
                if value and value != "not-configured" and value in text:
                    raise ValueError(f"Экспорт содержит значение {name}; запись отменена")
            output.write_text(text + "\n")
            print(f"Записан очищенный экспорт: {output}")
        print("Работа завершена")
        return 0
    except urllib.error.HTTPError as error:
        print(f"Ошибка Keycloak: HTTP {error.code}; тело ответа скрыто")
    except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(f"Ошибка работы с {root}: {type(error).__name__}: {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
