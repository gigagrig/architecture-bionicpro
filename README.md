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

Быстрый запуск из корня репозитория:

```bash
python3 scripts/prepare-local.py
docker compose up -d --build --wait
```

Откройте `https://localhost:3443`, предварительно доверив локальный сертификат
по [инструкции](Task1/README.md#сертификат-и-первый-вход).
Compose использует отдельный проект `bionicpro-task1`, отдельные тома и порт 3443.
Исходный `postgres-keycloak-data` не изменяется.

Для демонстрационных отчётов заполните источники и подготовьте период
по [инструкции задания 2](Task2/README.md#подготовка-и-запуск).
