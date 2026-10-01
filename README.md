# BionicPRO

Учебный проект по архитектуре ПО. [Условие](sprint-9-assignment.md).

Решение задания 1:

- [Архитектура и ограничения](Task1/architecture.md).
- [Диаграмма C4 в draw.io](Task1/bionicpro-security.drawio).
- [Запуск, проверки и подключение Яндекс ID](Task1/README.md).
- [Результаты проверок и открытые вопросы](report.md).

Быстрый запуск из корня репозитория:

```bash
python3 scripts/prepare-local.py
docker compose up -d --build --wait
```

Откройте `https://localhost:3443`, предварительно доверив локальный сертификат
по [инструкции](Task1/README.md#сертификат-и-первый-вход).
Compose использует отдельный проект `bionicpro-task1`, отдельные тома и порт 3443.
Исходный `postgres-keycloak-data` не изменяется.
