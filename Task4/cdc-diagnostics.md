# Диагностика и восстановление CDC

Команды выполняются из корня репозитория для запущенного локального стенда.
Запуск и подготовка данных описаны в [общем руководстве](../operations.md),
поток изменений — в [архитектуре CDC](architecture.md).

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

## Автоматические проверки

Проверки Go, серверной логики, публикации и отчётности собраны в
[общем руководстве](../operations.md#автоматические-проверки).

