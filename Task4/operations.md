# Запуск CDC и отчётов BionicPRO

[Архитектура и схема](architecture.md), [SQL ClickHouse](../clickhouse/cdc.sql),
[коннектор CRM](../debezium/crm-connector.json),
[результаты проверок](../report.md#задание-4--результаты-работы).
API написан на Go. Debezium и KafkaEngine доставляют изменения источников;
Airflow публикует готовые OLAP-отчёты без чтения CRM.

## Подготовка и запуск

Команды выполняются из корня репозитория. Нужны Docker с Compose v2,
Python 3.12+, OpenSSL и сеть для загрузки закреплённых образов.
Для первого запуска также нужна сборка MinIO; она требует нескольких минут
и свободного места.

```bash
python3 scripts/prepare-local.py
docker compose build minio
docker compose build minio-init
docker compose build reports-api bionicpro-auth frontend keycloak \
  airflow-init airflow-scheduler airflow-webserver reports-seed
docker compose up -d
docker compose up -d --no-deps --wait --wait-timeout 240 \
  airflow-scheduler airflow-webserver cdn reports-api gateway
docker compose ps -a
docker compose logs debezium-init cdc-schema crm-cdc-init telemetry-cdc-init
```

При обновлении стенда заданий 1–3 первые две сборки MinIO можно пропустить,
если образы уже существуют. Подготовка сохраняет прежние секреты и добавляет
`CRM_CDC_PASSWORD` и `TELEMETRY_CDC_PASSWORD`. Compose сохраняет все старые
тома и имя проекта; новый том `kafka-data` хранит сообщения и позиции Connect.
Не удаляйте тома для применения миграций.

`crm-cdc-init` добавляет ключ владения, ограниченный аккаунт и публикацию;
`telemetry-cdc-init` настраивает второй источник. `kafka-init` создаёт топики,
`cdc-schema` применяет SQL, `debezium-init` регистрирует коннекторы. Все пять
одноразовых сервисов должны закончить с кодом 0. Сервис Connect должен быть
`healthy`; дополнительно проверьте задачи коннекторов:

```bash
docker compose exec kafka-connect curl -fsS http://localhost:8083/connectors/bionicpro-crm/status
docker compose exec kafka-connect curl -fsS http://localhost:8083/connectors/bionicpro-telemetry/status
```

Ожидается `RUNNING` и у коннектора, и у задачи. Это ещё не доказывает завершение
начального снимка; готовность определяется по данным внутри ClickHouse.

Стенд: `https://localhost:3443`; Airflow: `http://localhost:8085`.
Пароль администратора Airflow хранится в локальном `.env`.
Учебные аккаунты `user1`/`user2`, пароль `password123`, затем одноразовый
код. Доверие сертификату и настройка OTP описаны в
[задании 1](../Task1/operations.md#сертификат-и-первый-вход).
OTP — одноразовый код из приложения-аутентификатора.

## Демоданные и публикация периода

```bash
docker compose --profile tools run --rm reports-seed --days 3
docker compose exec -T clickhouse sh -c \
  'clickhouse-client --user etl --password "$CLICKHOUSE_PASSWORD" --query "SELECT * FROM reporting.cdc_readiness"'
```

После доставки CDC ожидаются `checkpoints=2`, `live_sources=2` и
`closed_through`, покрывающий нужный период. Подождите примерно 10–30 секунд;
время зависит от ресурсов и объёма начального снимка.

Задача `publish_cdc` DAG `bionicpro_reports` запускается каждую минуту.
Она автоматически добавляет вчерашний день UTC и обновляет ранее опубликованные
дни, если изменились показатели. Чтобы опубликовать три дня из вывода заполнения,
отправьте по одному запуску на каждый день:

```bash
docker compose exec airflow-scheduler airflow dags trigger bionicpro_reports --conf '{"day":"2026-09-29"}'
docker compose exec airflow-scheduler airflow dags trigger bionicpro_reports --conf '{"day":"2026-09-30"}'
docker compose exec airflow-scheduler airflow dags trigger bionicpro_reports --conf '{"day":"2026-10-01"}'
```

Пример соответствует запуску 2 октября 2026 года. Замените даты на фактические
завершённые дни. Scheduler выполняет запуски последовательно. В Airflow Grid
проверьте `success` у `publish_cdc`; задача включает запись витрины, проверку
числа строк и обновление S3-каталога. Прежний `backfill` по ежедневному
расписанию больше не применяется. Явный `day` создаёт новую версию даже при
неизменных данных; обычный минутный запуск сохраняет версии без изменений.

В браузере получите отчёт от первого дня до дня после последнего, например
`2026-09-29` → `2026-10-02`. Конечная дата не включается. У `user1` видны его
два протеза, у `user2` — только его протез. Неподготовленный день даёт `409`.
API использует `catalog/cdc-current.json`; каталог старого ETL не требуется
копировать в новый, поскольку UUID версий и исходная витрина различаются.

## Автоматические проверки

```bash
cd reports-api
go test -race ./...
go vet ./...
```

Из корня репозитория:

```bash
docker compose exec -e PYTHONPATH=/opt/airflow/dags airflow-scheduler \
  python -m pytest /opt/airflow/tests/test_etl.py -q -p no:cacheprovider
python3 -m venv .venv
.venv/bin/pip install -r bionicpro-auth/requirements-test.txt
PYTHONPATH=bionicpro-auth .venv/bin/python -m pytest \
  bionicpro-auth/tests/test_security.py bionicpro-auth/tests/test_reports.py -q
BIONICPRO_E2E=1 .venv/bin/python -m pytest -q \
  Task2/tests/test_reports_live.py Task3/tests/test_cdn_live.py Task4/tests/test_cdc_live.py
```

Нужен доступ к Docker. Тесты проверяют настоящую доставку, UPDATE/DELETE,
передачу владения на границе времени, изменение модели, повтор старой
транзакции через Kafka, запрет неполных транзакций и неоднозначных данных.
HTTPS-проверки используют реальный вход с OTP, Go, ClickHouse, MinIO и CDN.
Дополнительно проверяется отсутствие паролей и сетевого пути из Airflow/API
к исходным БД. Тестовые аккаунты и исходные записи удаляются; неизменные
версии файлов и витрины остаются до согласованной очистки истории.

Во время тестов не запускайте ручную публикацию и не получайте другие отчёты:
это меняет версии и общий счётчик запросов API. Внутренний `dags test`
предназначен для проверки и может обходить ограничения scheduler;
для обычной работы используйте `dags trigger`.

## Диагностика и восстановление

```bash
docker compose exec -T clickhouse sh -c \
  'clickhouse-client --user etl --password "$CLICKHOUSE_PASSWORD" --multiquery' <<'SQL'
SELECT topic, count() FROM reporting.cdc_log GROUP BY topic;
SELECT * FROM reporting.cdc_readiness;
SELECT count() AS invalid_rows FROM reporting.cdc_invalid_rows;
SELECT database, view, status, last_success_time, read_rows, written_rows,
       substring(exception, 1, 250) AS error FROM system.view_refreshes;
SELECT day, ready FROM reporting.cdc_report_current WHERE row_kind='period' ORDER BY day DESC LIMIT 5;
SELECT day, batch_id, row_count, published_at FROM reporting.cdc_report_periods FINAL ORDER BY day DESC LIMIT 5;
SQL
docker compose exec crm_db psql -U crm -d crm -c \
  "SELECT slot_name,active,wal_status,pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)) AS retained FROM pg_replication_slots"
docker compose exec telemetry_db psql -U telemetry -d telemetry -c \
  "SELECT slot_name,active,wal_status,pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)) AS retained FROM pg_replication_slots"
docker compose logs --tail 100 kafka-connect
docker compose exec airflow-scheduler airflow dags list-import-errors
```

- Если коннектор или его задача `FAILED`, устраните причину в PostgreSQL/Kafka,
  затем перезапустите задачу через Connect. Для обычного перезапуска достаточно
  `docker compose restart kafka-connect`: конфигурации и позиции сохраняются.
- Если `live_sources < 2`, проверьте heartbeat и доставку; новая публикация
  запрещена после двух минут отсутствия heartbeat. Старые опубликованные
  отчёты продолжают выдаваться. Время их подготовки видно в `periods` файла.
- Если день не готов при исправных heartbeat, проверьте контрольные отметки,
  `cdc_invalid_rows` и `cdc_invalid_events`. Не выставляйте `ready` или
  состояние Success вручную: это не исправляет исходные данные.
- При отказе S3 восстановите MinIO. Следующий запуск повторит каталог,
  сохранив уже записанные версии. После изменения политик повторите
  `docker compose run --rm minio-init`.
- Логи SQL/CLI инициализаторов находятся в `/tmp/log` соответствующих
  контейнеров; stdout показывает точный путь. Их можно скопировать командой
  `docker compose cp cdc-schema:/tmp/log ./log/cdc-schema`.

Для проверки возобновления доставки можно перезапустить Connect или ClickHouse,
проверить `RUNNING`, heartbeat и показатели. Не удаляйте слот, топики,
служебные offsets или `cdc_log` по отдельности. Если слот потерял WAL
(`wal_status=lost`) либо журнал/позиции утрачены, сохраните старые опубликованные
версии, подготовьте изолированное новое поколение топиков, слотов и таблиц,
выполните полный начальный снимок и сверку, затем переключите публикацию.
Автоматическое восстановление после такой потери в стенде не реализовано.

## Ручная проверка интерфейса

После доверия локальному сертификату откройте браузер и получите подготовленный
период. Проверьте свои протезы, скачивание JSON и отсутствие чужих данных.
Обновите синтетические показатели или модель протеза в источнике, дождитесь
успеха `publish_cdc` и повторите получение: изменятся показатели и путь файла;
повторное чтение нового файла перейдёт от `MISS` к `HIT`.
Запишите фактический результат в [report.md](../report.md). Скриншоты условием
задания 4 не требуются.
