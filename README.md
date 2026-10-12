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

Сервис не заменяет Jira, Bitbucket и CI. Он подписывается на webhook существующих репозиториев,
читает состояние PR через REST и создаёт задачи в существующих проектах Jira. Workflow, доски
и права команд не меняются. Ниже — порядок подключения к **Jira Data Center / Server** и
**Bitbucket Data Center / Server**. Cloud (atlassian.net / bitbucket.org) не проверялся.

Начните с **тестовых** проектов или тестовых экземпляров, затем подключайте один пилотный репозиторий.

### 0. Что собрать заранее

| Что нужно | Пример | Переменная |
|---|---|---|
| Базовый URL Jira | `https://jira.company.ru` | `SDD_JIRA_BASE_URL` |
| Базовый URL Bitbucket | `https://bitbucket.company.ru` | `SDD_BITBUCKET_BASE_URL` |
| Публичный URL сервиса (его должен видеть Bitbucket) | `https://sdd.company.ru` | `SDD_PUBLIC_URL` |
| Проект и тип задачи, в котором заводят идеи | проект `PROD`, тип `Epic` | `SDD_JIRA_IDEA_ISSUE_TYPE` |
| Куда создавать задачи системного анализа | проект `ARCH`, тип `Task` | `SDD_JIRA_ANALYSIS_PROJECT`, `SDD_JIRA_ANALYSIS_ISSUE_TYPE` |
| Куда создавать задачи разработки | проект команды `LOANS`, тип `Story` | `SDD_JIRA_DEV_PROJECT`, `SDD_JIRA_DEV_ISSUE_TYPE` |
| Репозиторий спецификаций | `SPEC/business-specs` | `SDD_SPEC_REPOSITORIES` |
| Пилотный репозиторий кода | `LOANS/loan-service` | `SDD_CODE_REPOSITORIES` |
| Целевая ветка merge | `master` или `develop` | `SDD_TARGET_BRANCHES` |
| Имя перехода «готово» в workflow команды | `Готово` / `Done` / `Resolve` | `SDD_JIRA_DONE_TRANSITION` |
| Обязательные поля при создании задач | компонент, `customfield_10010` | `SDD_JIRA_*_EXTRA_FIELDS` |

Формат репозитория везде `PROJECT_KEY/repository-slug`. Несколько значений — через запятую.

### 1. Jira Data Center

1. Создайте сервисную учётную запись, например `svc-sdd`, и её **Personal Access Token**
   (Profile → Personal Access Tokens). Токен передаётся как `Authorization: Bearer`.
2. Права учётки:
   - проекты анализа и разработки: Browse Projects, Create Issues, Link Issues, Add Comments, Transition Issues;
   - проект идей: Browse Projects, Link Issues (создание идей не нужно).
3. Тип задачи для идей — любой существующий (`Epic`, `Story`, `Business Idea`). Укажите точное имя
   в `SDD_JIRA_IDEA_ISSUE_TYPE`. Пустая строка отключает проверку типа.
4. Обязательные поля идеи — в `SDD_JIRA_IDEA_REQUIRED_FIELDS` (имена из REST, через запятую),
   например `summary,description` или `summary,description,customfield_12345`.
5. На экране **создания** задач анализа и разработки должно быть поле **Labels**. Сервис ставит
   служебную метку `sdd-op-<hash>` и по ней ищет задачу при повторной обработке. Без Labels Jira
   ответит 400 на `POST /rest/api/2/issue`.
6. Проверьте типы связей: `GET /rest/api/2/issueLinkType`. Нужны имена, которые стоят в
   `SDD_JIRA_RELATES_LINK_TYPE` (по умолчанию `Relates`) и `SDD_JIRA_BLOCKS_LINK_TYPE` (`Blocks`).
   Если в инсталляции другие имена — поменяйте переменные.
7. Узнайте точное имя перехода в статус «готово» в workflow задач разработки и укажите его в
   `SDD_JIRA_DONE_TRANSITION`. Если перехода нет или двигать статус не нужно — оставьте пустым:
   сервис пропустит переход и напишет предупреждение в лог.
8. Обязательные кастомные поля, компоненты, Epic Link передайте JSON-объектом в
   `SDD_JIRA_ANALYSIS_EXTRA_FIELDS` / `SDD_JIRA_DEV_EXTRA_FIELDS`. Объект сливается в `fields`
   запроса создания задачи:

   ```bash
   SDD_JIRA_DEV_EXTRA_FIELDS={"components":[{"name":"loan-service"}]}
   ```

9. Если UI Jira открывается с другого хоста, чем REST, задайте `SDD_JIRA_BROWSE_URL`
   (ссылки в комментариях и на дашборде). Иначе используется `SDD_JIRA_BASE_URL`.

Проверка типов задач (после заполнения `.env`):

```bash
uv run sdd check
```

Команда читает `GET /rest/api/2/issue/createmeta/{project}/issuetypes`.

### 2. Bitbucket Data Center

1. Создайте репозиторий спецификаций (или используйте существующий) и наполните его содержимым
   `openspec-templates/business-specs/`. В настройках репозитория: default reviewers, merge check
   «минимум N approvals» (`N` = `SDD_MIN_APPROVALS`), запрет прямого push в целевую ветку.
2. В пилотный репозиторий кода добавьте `AGENTS.md`, `openspec/config.yaml` и `prompts/developer.md`
   из `openspec-templates/application-repo/`. В `.gitignore` — строку `.sdd/`.
3. Сервисная учётка Bitbucket, например `svc-sdd`, с Personal Access Token. Достаточно прав
   **чтения** на оба репозитория плюс комментарии к PR (Read + комментирование). Merge сервис
   не выполняет.
4. Webhook вешайте **на репозиторий**, не на весь проект: иначе придут события всех PR команды.
   Repository settings → Webhooks → Create webhook:
   - URL: `{SDD_PUBLIC_URL}/webhooks/bitbucket` (например `https://sdd.company.ru/webhooks/bitbucket`);
   - Secret: случайная строка, та же в `SDD_BITBUCKET_WEBHOOK_SECRET`;
   - события: `pr:opened`, `pr:from_ref_updated`, `pr:merged`, `pr:declined`,
     `pr:reviewer:needs_work`, `pr:reviewer:approved`;
   - SSL / TLS включён.
   Кнопка Test connection должна получить ответ сервиса (`{"status":"pong"}` на `diagnostics:ping`).

   `sdd register-webhook --project SPEC` создаёт webhook **проекта**, не репозитория. Для пилота
   в рабочем пространстве команды используйте UI репозитория, как выше. Команда `sdd check`
   сейчас тоже ищет только webhook проекта — после настройки репозиторного webhook она может
   написать «НЕТ»; это не ошибка конфигурации репозитория.

5. Проверьте на **вашей** версии Bitbucket, что в ответе слитого PR есть merge commit:

   ```http
   GET /rest/api/latest/projects/{key}/repos/{slug}/pull-requests/{id}
   ```

   Ожидается `properties.mergeCommit.id`. Без этого поля сервис не сможет построить ключ
   идемпотентности и будет повторять обработку (`NotReady`).

6. Соглашение об именах веток не обязательно. Если ветка не `feature/<CHG>-business-spec` /
   `…-system-spec` / `…-<JIRA>-implementation`, в описании PR должны быть строки:

   ```
   Change ID: CHG-1042
   Stage: business_specification
   Jira: IDEA-123
   ```

   Для кода `Stage: implementation` и `Jira: DEV-231`. Команда `sdd open-pr` пишет эти строки сама.

### 3. CI

1. Статусы сборок репозитория **кода** должны попадать в Bitbucket Build Status API
   (`/rest/build-status/1.0/commits/{sha}`), состояния `SUCCESSFUL` / `FAILED` / `INPROGRESS`.
   Bamboo делает это из коробки, Jenkins — через плагин Bitbucket Server Integration.
2. Если CI статусы в Bitbucket не публикует, задайте `SDD_REQUIRE_CI=false`. Иначе после merge
   кода сервис будет ждать сборку и не закроет задачу.
3. Для репозитория спецификаций (необязательно) добавьте required build:

   ```bash
   uv run sdd validate openspec/changes/<CHG>/business-spec.md --type business --change <CHG>
   uv run sdd validate openspec/changes/<CHG>/functional-spec.md --type functional --change <CHG>
   ```

### 4. Инфраструктура сервиса

1. Один экземпляр (VM, Docker или существующий Kubernetes). Образ: `Dockerfile`.
2. PostgreSQL — отдельная база или схема. Для схемы `sdd`:

   ```bash
   SDD_DATABASE_URL=postgresql+psycopg://sdd:***@db.company.ru:5432/platform?options=-csearch_path%3Dsdd
   ```

   Таблицы создаются при старте (`SDD_AUTO_MIGRATE=true`). Чтобы накатывать миграции отдельно:
   `uv run sdd migrate` и `SDD_AUTO_MIGRATE=false`.
3. Reverse proxy с внутренним TLS, DNS вида `sdd.company.ru`. Bitbucket должен достучаться до
   сервиса по HTTPS; сервис — до REST Jira и Bitbucket и до PostgreSQL.
4. Секреты (токены Jira/Bitbucket, секрет webhook, пароль БД) — в корпоративном secret manager
   или в `--env-file`, не в git.
5. Дашборд и `/api/*` сейчас **без аутентификации**. Для пилота закройте их SSO / Basic Auth
   на reverse proxy. Путь `/webhooks/bitbucket` уже проверяет HMAC-подпись, его не закрывайте
   формой логина.
6. Мониторинг: `/health` — liveness; `/metrics` — Prometheus. Имеет смысл алертить на
   `sdd_events{status="dead"}` и `sdd_events{status="failed"}`.

Запуск сервиса без моков:

```bash
cp .env.example .env          # заполните URL, токены, проекты, репозитории
uv sync
uv run sdd serve              # слушает SDD_HOST:SDD_PORT, по умолчанию 127.0.0.1:48080
```

Или Docker:

```bash
docker build -t sdd-orchestrator .
docker run --rm -p 48080:48080 --env-file .env sdd-orchestrator
```

### 5. Файл `.env`

Скопируйте `.env.example` и замените моковые URL. Минимальный рабочий набор:

```bash
SDD_PUBLIC_URL=https://sdd.company.ru
SDD_DATABASE_URL=postgresql+psycopg://sdd:***@db.company.ru:5432/sdd

SDD_BITBUCKET_BASE_URL=https://bitbucket.company.ru
SDD_BITBUCKET_TOKEN=***
SDD_BITBUCKET_WEBHOOK_SECRET=***
SDD_SPEC_REPOSITORIES=SPEC/business-specs
SDD_CODE_REPOSITORIES=LOANS/loan-service
SDD_TARGET_BRANCHES=master
SDD_MIN_APPROVALS=1
SDD_REQUIRE_CI=true

SDD_JIRA_BASE_URL=https://jira.company.ru
SDD_JIRA_TOKEN=***
SDD_JIRA_IDEA_ISSUE_TYPE=Epic
SDD_JIRA_IDEA_REQUIRED_FIELDS=summary,description
SDD_JIRA_ANALYSIS_PROJECT=ARCH
SDD_JIRA_ANALYSIS_ISSUE_TYPE=Task
SDD_JIRA_DEV_PROJECT=LOANS
SDD_JIRA_DEV_ISSUE_TYPE=Story
SDD_JIRA_DONE_TRANSITION=Готово
SDD_JIRA_DEV_EXTRA_FIELDS={"components":[{"name":"loan-service"}]}
```

Вместо PAT можно задать basic auth: `SDD_JIRA_USERNAME` / `SDD_JIRA_PASSWORD` и то же для Bitbucket.
Если указан токен, username/password не используются.

Если шаблоны путей к спецификациям в вашем репозитории другие, задайте
`SDD_BUSINESS_SPEC_PATH` и `SDD_FUNCTIONAL_SPEC_PATH` (плейсхолдер `{change_id}`).

### 6. Клиенты аналитиков и разработчиков

CLI ходит в живые Jira/Bitbucket с **личной** учётной записью: PR создаётся от имени владельца токена.

```bash
export SDD_PUBLIC_URL=https://sdd.company.ru
export SDD_BITBUCKET_BASE_URL=https://bitbucket.company.ru
export SDD_BITBUCKET_TOKEN=          # личный PAT
export SDD_JIRA_BASE_URL=https://jira.company.ru
export SDD_JIRA_TOKEN=               # личный PAT, нужен для sdd start-dev
```

Дальше — те же команды, что в разделе «Процесс для участников», с вашими ключами проектов:

```bash
sdd start IDEA-123 --author "Иванова А. С."
sdd open-pr --change CHG-1042 --stage business --jira IDEA-123 --project SPEC --repo business-specs

sdd start-system CHG-1042 --component loan-service
sdd open-pr --change CHG-1042 --stage system --project SPEC --repo business-specs

sdd start-dev CHG-1042 DEV-231
sdd open-pr --change CHG-1042 --stage implementation --jira DEV-231 --project LOANS --repo loan-service
```

### 7. Проверка на тестовом изменении

1. Создайте в Jira задачу-идею с заполненными обязательными полями.
2. `sdd start <KEY>` — в ответе Change ID и имя ветки. На дашборде сервиса появляется изменение.
3. Заполните бизнес-спецификацию, `sdd validate`, `sdd open-pr --stage business`.
4. В Bitbucket: approve → merge. В Jira должна появиться **ровно одна** задача системного анализа
   со ссылкой на Change ID и commit спецификации.
5. Повторите webhook (Webhooks → View details → Resend): вторая задача **не** создаётся.
   На дашборде растёт «Предотвращено дубликатов».
6. То же для функциональной спецификации — появляются связанные задачи разработки.
7. Для одной задачи разработки: `sdd start-dev`, PR, CI `SUCCESSFUL`, approve, merge — задача
   переходит в «готово».

Если Jira недоступна в момент merge, событие уходит в повтор (`failed` → обработчик повторов).
После восстановления токена или сети задача создаётся без ручных правок БД. Если Jira успела
создать задачу, но ответ до сервиса не дошёл, повторная обработка находит её по метке `sdd-op-…`
и дубликат не создаёт.

### 8. Ограничения текущего кода на живых репозиториях

Имейте в виду до пилота в общем репозитории команды:

1. PR без строк `Change ID` / `Stage` в подключённом репозитории сервис **отклоняет комментарием**.
   Обычные PR команды получат сообщение «PR не принят в процесс». Для общего репозитория это нужно
   сначала поменять (молча игнорировать такие PR).
2. Задачи разработки всегда создаются в одном `SDD_JIRA_DEV_PROJECT`. Несколько командных
   пространств из коробки не разводятся.
3. Дашборд и REST API без встроенной аутентификации — закрывайте на proxy.
4. `sdd register-webhook` / `sdd check` работают с webhook **проекта**. Репозиторный webhook
   настраивается вручную (шаг 2.4).

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
