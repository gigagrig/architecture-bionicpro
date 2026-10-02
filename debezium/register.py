#!/usr/bin/env python3
"""Register JSON connector configurations without expanding or printing secrets."""
import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage()
        print(f"Argument error: {message}")
        raise SystemExit(2)


def main() -> int:
    parser = ArgumentParser(description="Register/update PostgreSQL Debezium connectors via Connect REST.",
        epilog="Example: python register.py --url http://kafka-connect:8083 --config-dir /config\n"
               "Secrets are resolved inside Connect by its env provider. No files created. "
               "Exit: 0 success, 1 failure, 2 arguments.")
    parser.add_argument("--url", default="http://kafka-connect:8083", help="Connect REST URL")
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parent, help="Directory containing *-connector.json")
    parser.add_argument("--timeout", type=int, default=120, help="Seconds to wait for running connectors (1–600)")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("timeout must be between 1 and 600")
    print(f"Start connector setup: {args.url}; configurations: {args.config_dir.resolve()}", flush=True)
    try:
        for filename in ("crm-connector.json", "telemetry-connector.json"):
            path = args.config_dir / filename
            definition = json.loads(path.read_text())
            name = definition["name"]
            request = Request(args.url.rstrip("/") + f"/connectors/{name}/config",
                data=json.dumps(definition["config"]).encode(), method="PUT", headers={"Content-Type": "application/json"})
            print(f"Apply {path}: {name}", flush=True)
            deadline = time.monotonic() + args.timeout
            while True:
                try:
                    with urlopen(request, timeout=15) as response:
                        response.read()
                    break
                except HTTPError as error:
                    if error.code not in (409, 503) or time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
            while True:
                try:
                    with urlopen(args.url.rstrip("/") + f"/connectors/{name}/status", timeout=10) as response:
                        status = json.load(response)
                except HTTPError as error:
                    if error.code not in (404, 409, 503) or time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
                    continue
                states = [status["connector"]["state"]] + [task["state"] for task in status["tasks"]]
                if "FAILED" in states:
                    raise ValueError(f"{name} failed; inspect Connect logs (credentials are not printed here)")
                if len(states) > 1 and all(state == "RUNNING" for state in states):
                    print(f"Connector running: {name}", flush=True)
                    break
                if time.monotonic() >= deadline:
                    raise ValueError(f"{name} did not start within timeout")
                time.sleep(2)
        print("Connector setup complete: 2 running connectors", flush=True)
        return 0
    except (OSError, ValueError, KeyError, HTTPError, URLError) as error:
        code = f" HTTP {error.code}" if isinstance(error, HTTPError) else ""
        print(f"Connector setup failed: {type(error).__name__}{code}; directory: {args.config_dir.resolve()}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
