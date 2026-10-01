# BionicPRO

Учебный проект по архитектуре ПО. [Условие](sprint-9-assignment.md).

Решение задания 1:

- [Архитектура и ограничения](Task1/architecture.md).
- [Диаграмма C4 в draw.io](Task1/bionicpro-security.drawio).
- [Запуск, проверки и подключение Яндекс ID](Task1/README.md).
- [Результаты проверок и открытые вопросы](report.md).

Решение задания 2:

- [Архитектура отчётности и диаграмма draw.io](Task2/architecture.md).
- [Запуск, подготовка витрины и получение отчёта](Task2/README.md).
- [Сервис отчётов на Go](reports-api/main.go) и [Airflow DAG](airflow/dags/report_etl.py).

Решение задания 3:

- [Хранение в S3, доступ и обновление кеша CDN](Task3/architecture.md).
- [Запуск, переход с задания 2 и проверки](Task3/README.md).
- [Go: хранение отчётов и проверка скачивания](reports-api/cache.go), [Nginx CDN](nginx/cdn.conf).

Быстрый запуск из корня репозитория:

```bash
python3 scripts/prepare-local.py
docker compose build minio
docker compose build minio-init
docker compose up -d --build --wait
```

Откройте `https://localhost:3443`, предварительно доверив локальный сертификат
по [инструкции](Task1/README.md#сертификат-и-первый-вход).
Compose использует отдельный проект `bionicpro-task1`, отдельные тома и порт 3443.
Исходный `postgres-keycloak-data` не изменяется.

Для демонстрационных отчётов заполните источники и подготовьте период
по [инструкции задания 2](Task2/README.md#подготовка-и-запуск).
Для ранее подготовленной витрины опубликуйте каталог версий по
[инструкции задания 3](Task3/README.md#запуск-и-обновление-стенда).
