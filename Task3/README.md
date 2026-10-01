# Отчёты через S3 и CDN

[Архитектура и схема](architecture.md), [Go API](../reports-api/cache.go),
[Nginx CDN](../nginx/cdn.conf), [результаты проверок](../report.md#задание-3--результаты-работы).

## Запуск и обновление стенда

Команды выполняются из корня репозитория. Нужны Docker Compose, сеть и
ресурсы для первой Go-компиляции MinIO. MinIO и `mc` собираются из фиксированных
официальных исходников: первая сборка занимает несколько минут. Собирайте
их последовательно; кеш компиляции временно размещается в памяти.

```bash
python3 scripts/prepare-local.py
docker compose build minio
docker compose build minio-init
docker compose build reports-api bionicpro-auth frontend airflow-scheduler airflow-webserver airflow-init reports-seed
docker compose up -d --wait --wait-timeout 240
docker compose exec gateway nginx -t
docker compose exec gateway nginx -s reload
docker compose exec cdn nginx -t
```

`minio-init` создаёт закрытый бакет `bionicpro-reports` и ограниченных
пользователей API/ETL. Повторная инициализация сохраняет файлы; прежние секреты,
тома и имя Compose-проекта также сохраняются. S3 и CDN не публикуют порты.
Интерфейс: `https://localhost:3443`. Доверие сертификату и OTP — в
[инструкции задания 1](../Task1/README.md#сертификат-и-первый-вход).

Если витрина уже подготовлена в задании 2, опубликуйте каталог без повторного ETL:

```bash
docker compose exec airflow-scheduler airflow tasks test bionicpro_reports publish_catalog 2026-10-01
```

Дата задаёт контекст теста задачи; каталог включает все опубликованные дни.
Без каталога API отвечает `503`. Регулярный DAG теперь состоит из трёх задач:
`build_mart → publish → publish_catalog`. Все три должны завершиться успешно.

Для нового стенда заполните источники и подготовьте дни:

```bash
docker compose --profile tools run --rm reports-seed --days 3
docker compose exec airflow-scheduler airflow dags backfill bionicpro_reports \
  --start-date 2026-09-28 --end-date 2026-09-30
```

Замените даты на дни из вывода заполнения. Повторная обработка 30 сентября:

```bash
docker compose exec airflow-scheduler airflow dags test bionicpro_reports 2026-10-01T12:00:00+00:00
```

Ручной тест использует полдень **следующего** дня UTC. Различия дат backfill
и теста — в [задании 2](../Task2/README.md#подготовка-периода).
Не запускайте параллельную обработку одного дня. При сбое S3 проверьте
MinIO и журнал `publish_catalog`, затем повторите только эту задачу через
Airflow Grid → Clear либо `tasks test`. Не выставляйте `success` вручную.
`docker compose logs minio-init` показывает этапы и пути журналов `mc`;
журналы находятся в `/tmp/log` контейнера инициализатора.

## Получение отчёта

1. Войдите как `user1` (учебный пароль `password123`, затем OTP).
2. Выберите подготовленный период, максимум 31 день UTC. Последний день
   не включается: для 28–30 сентября укажите `2026-09-28` и `2026-10-01`.
3. Нажмите «Получить отчёт». Интерфейс получит ссылку и загрузит таблицу с CDN.
4. Повторите тот же период: Go найдёт файл S3 без запросов к ClickHouse.
5. Нажмите «Скачать отчёт JSON». Кнопка получает свежую ссылку и скачивает
   фактический файл CDN. Если ETL успел обновить данные, скачивание отражает
   актуальную версию на момент нового запроса.

Ответ API содержит `download_url` и `expires_at`; строки находятся в файле.
Ссылка действует пять минут и требует сессию владельца. Чужая или изменённая
ссылка даёт `403`, отсутствие сессии — `401`. Токены и ключи S3 остаются на сервере.

## Автоматические проверки

```bash
cd reports-api
go test -race ./...
go vet ./...
```

Из корня, используя окружение проверки задания 2:

```bash
PYTHONPATH=bionicpro-auth .venv/bin/python -m pytest \
  bionicpro-auth/tests/test_security.py bionicpro-auth/tests/test_reports.py -q
docker compose exec -e PYTHONPATH=/opt/airflow/dags airflow-scheduler \
  python -m pytest /opt/airflow/tests/test_etl.py -q
BIONICPRO_E2E=1 PYTHONPATH=bionicpro-auth .venv/bin/python -m pytest \
  Task3/tests/test_cdn_live.py Task2/tests/test_reports_live.py -q -s
```

Сквозные тесты создают временные аккаунты с OTP и синтетические источники,
выполняют настоящий DAG, затем удаляют аккаунты и записи источников.
Снимки витрины и S3-файлы временных аккаунтов остаются учебными артефактами
до очистки. Нужен доступ к Docker; HTTPS проверяется по локальному сертификату.

Тест задания 3 сравнивает число успешных запросов аккаунта `reports_api` в
`system.query_log` ClickHouse: первый запрос добавляет один, повторный — ноль.
Также проверяются `MISS → HIT`, отказ чужому пользователю при тёплом кеше,
новый URL/данные после ETL и отказ после выхода. Во время теста не выполняйте
другие запросы отчётов: они изменят общий счётчик.

## Ручная проверка

После доверия сертификату откройте браузер и DevTools → Network.

1. Получите отчёт. В JSON `/api/reports` должен быть относительный
   `download_url`, без строк, токенов и адреса MinIO.
2. В запросе `/cdn/reports/...` проверьте `200` и `X-Report-Cache: MISS`
   для нового файла. Повторное чтение того же файла должно дать `HIT`.
   Уже прогретый файл сразу даст `HIT`; новый ETL создаёт путь для проверки `MISS`.
3. Скачайте JSON кнопкой и сопоставьте протезы и показатели с таблицей.
4. Скопируйте ссылку и выйдите: она должна давать `401`, включая прогретый файл.
   Войдите как `user2` и откройте ту же ссылку: ожидается `403`.
5. Повторите ETL того же дня и получите отчёт: путь должен измениться,
   новое чтение дать `MISS`, затем `HIT`. В Airflow Grid проверьте три зелёные задачи.

Фактический результат ручной проверки запишите в [report.md](../report.md).
