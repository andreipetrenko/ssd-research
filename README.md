# SDD Orchestrator

Integration Service для SDD MVP (технический проект «SDD MVP за 4 недели»): сквозной процесс
**бизнес-идея → OpenSpec → Bitbucket PR → Jira → AI-разработка**.

Сервис получает webhook Bitbucket Data Center, проверяет фактическое состояние PR через REST API,
валидирует спецификации OpenSpec на merge commit и создаёт задачи в Jira Data Center — ровно один раз,
даже при повторной доставке webhook и сбоях Jira. AI-агенты работают в IDE/CLI разработчика; сервис ими
не управляет и merge не выполняет.

```
Business Idea (Jira) ──sdd start──▶ business-spec.md ──PR──▶ review ──merge──▶ Jira: системный анализ
                                                                                    │
      Jira: задачи разработки ◀──merge── review ◀──PR── functional-spec.md ◀──sdd start-system
                │
                └─sdd start-dev──▶ агент: код + тесты ──PR──▶ CI + review ──merge──▶ Jira: Done
```

## Что внутри

| Путь | Назначение |
|------|------------|
| `src/sdd_orchestrator/api/webhook_handler.py` | Приём webhook, проверка HMAC-подписи, нормализация во внутренний контракт события (раздел 4.4) |
| `src/sdd_orchestrator/services/workflow_service.py` | Переходы процесса и проверки из таблицы раздела 3 |
| `src/sdd_orchestrator/services/idempotency_service.py` | Создание Jira-задач ровно один раз: ключ операции, сверка с Jira по метке |
| `src/sdd_orchestrator/services/spec_parser.py` | Валидация бизнес- и функциональной спецификации, разбор декомпозиции |
| `src/sdd_orchestrator/services/{bitbucket,jira}_client.py` | REST-клиенты Bitbucket DC и Jira DC (маршруты из разделов 4.1–4.2) |
| `src/sdd_orchestrator/workers/retry_worker.py` | Фоновый обработчик повторов с экспоненциальной задержкой |
| `src/sdd_orchestrator/cli.py` | CLI `sdd` для аналитиков, разработчиков и эксплуатации |
| `src/sdd_mocks/` | Моки Bitbucket DC и Jira DC с UI, отправкой webhook и инъекцией сбоев |
| `migrations/` | Схема PostgreSQL (раздел 5.1 + поля для повторов и метрик) |
| `openspec-templates/` | Каркасы репозиториев `business-specs` и `application-repo`: схема OpenSpec, шаблоны, `AGENTS.md`, промпты |
| `examples/credit-holidays/` | Заполненные спецификации для демо и тестов |

## Быстрый старт (локально, с моками)

Нужны Python 3.12+, [uv](https://docs.astral.sh/uv/) и PostgreSQL.

```bash
# База данных
sudo -u postgres psql -c "CREATE USER sdd WITH PASSWORD 'sdd' CREATEDB;" -c "CREATE DATABASE sdd OWNER sdd;"

uv sync
uv run sdd mocks          # мок Bitbucket + Jira: http://127.0.0.1:48090
uv run sdd serve          # Integration Service + дашборд: http://127.0.0.1:48080 (миграции применяются при старте)

uv run python scripts/demo.py --fault --tasks 2   # сквозной сценарий из раздела 13
```

Демо создаёт Business Idea, проводит обе спецификации через PR и merge, моделирует обрыв ответа Jira
после создания задачи, повторно доставляет webhook и доводит две задачи разработки до merge.
Третью задачу можно довести вручную в UI мока: «CI: успешно» → «Одобрить» → «Merge».

- Дашборд сервиса — `http://127.0.0.1:48080/`: изменения с трассировкой Change ID → PR → Jira, журнал
  событий с ключами идемпотентности, метрики из раздела 11.1, повторная обработка и перепроверка PR.
- UI моков — `http://127.0.0.1:48090/`: PR с кнопками одобрения/CI/merge/повторной доставки,
  задачи Jira со связями, переключатели сбоев Jira, журнал доставки webhook.
- REST API — `http://127.0.0.1:48080/docs`, метрики Prometheus — `/metrics`.

## Процесс для участников

```bash
# Бизнес-аналитик (в клоне business-specs)
sdd start IDEA-123 --author "Иванова А. С."       # Change ID, ветка, каркас business-spec.md
#   … агент заполняет документ по prompts/business-analyst.md …
sdd validate openspec/changes/CHG-1042/business-spec.md --type business --change CHG-1042
sdd open-pr --change CHG-1042 --stage business --jira IDEA-123

# Системный аналитик — после merge и появления задачи ANL-… в Jira
sdd start-system CHG-1042 --component loan-holidays-service
sdd open-pr --change CHG-1042 --stage system

# Разработчик (в клоне application-repo) — после появления задач DEV-… в Jira
sdd start-dev CHG-1042 DEV-231                     # ветка + .sdd/context.md для агента
sdd open-pr --change CHG-1042 --stage implementation --jira DEV-231 --project APP --repo application-repo
```

`sdd open-pr` переиспользует уже открытый PR из той же ветки, поэтому команду можно повторять.

## Переходы и проверки

| Событие Bitbucket | Проверки | Действие |
|---|---|---|
| PR бизнес-спецификации открыт/обновлён | Change ID, этап, Business Idea, документ по шаблону | Регистрация изменения, комментарий в PR с результатом проверки |
| PR бизнес-спецификации слит | Состояние MERGED через REST, целевая ветка, одобрения, документ на merge commit | Задача системного анализа, связь с Business Idea |
| PR функциональной спецификации слит | То же + ссылка на commit согласованной бизнес-спецификации | Задачи разработки из «Декомпозиции», связи Relates/Blocks |
| PR с кодом открыт | Jira-ключ — задача разработки этого изменения | Remote link PR в Jira |
| PR с кодом слит | Одобрения и CI (`SUCCESSFUL`) | Комментарий и переход Done; изменение завершается, когда закрыты все задачи |
| На доработку / отклонён | — | Статус изменения `changes_requested` / `declined` |

Ключи идемпотентности: `CHG-1042:create-system-analysis:<sha>`, `CHG-1042:create-dev-tasks:<sha>:DEV-1`.
Перед созданием задачи сервис ищет её в Jira по метке `sdd-op-<hash>`, поэтому падение между созданием
задачи и записью в БД восстанавливается повторной обработкой без дубликата. Ошибки интеграции повторяются
(`SDD_MAX_ATTEMPTS`, задержка `SDD_RETRY_BASE_DELAY_SECONDS · 2ⁿ`); невалидные события получают статус
`rejected` и повторно обрабатываются с дашборда после исправления.

## Подключение к реальным Jira и Bitbucket

1. Скопируйте `.env.example` в `.env`, укажите `SDD_JIRA_BASE_URL`, `SDD_BITBUCKET_BASE_URL`, токены
   сервисных учётных записей (Personal Access Token, `Bearer`) и `SDD_PUBLIC_URL`.
2. Настройте проекты и типы задач (`SDD_JIRA_*_PROJECT`, `*_ISSUE_TYPE`), обязательные поля
   передайте через `SDD_JIRA_*_EXTRA_FIELDS`.
3. `sdd register-webhook --project SPEC` (и для проекта приложения); задайте `SDD_BITBUCKET_WEBHOOK_SECRET`.
4. `sdd check` — проверит типы задач через createmeta и наличие webhook.

Маршруты REST соответствуют документации DC, но версию API и доступные события нужно проверить на
установленном экземпляре (раздел 4.1): например, наличие `properties.mergeCommit` в ответе PR.

## Схема OpenSpec

В техническом проекте предложены две схемы (`business/` и `system-analysis/`). Оба документа одного
изменения лежат в одной папке `openspec/changes/<Change ID>/`, а OpenSpec привязывает к папке изменения
одну схему, поэтому здесь используется одна схема `sdd` с двумя артефактами: `business-spec` →
`functional-spec`. Проверка: `openspec schema validate sdd`.

## Тесты

```bash
uv run pytest        # unit + сквозные сценарии на моках и PostgreSQL (БД sdd_test)
uv run ruff check src tests
```

Строка подключения тестовой БД — `SDD_TEST_DATABASE_URL`
(по умолчанию `postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd_test`).

## Docker

```bash
docker build -t sdd-orchestrator .
docker run -p 48080:48080 --env-file .env sdd-orchestrator
```
