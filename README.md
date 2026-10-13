# SDD Orchestrator

Сервис для процесса **бизнес-идея → OpenSpec → Bitbucket PR → Jira → AI-разработка**.

Он слушает webhook Bitbucket Data Center, проверяет PR и один раз создаёт задачи в Jira Data Center. Агенты работают у аналитика и разработчика в IDE; сервис ими не управляет и сам merge не делает.

```
Business Idea (Jira) ──sdd start──▶ business-spec.md ──PR──▶ merge──▶ Jira: системный анализ
                                                                                 │
      Jira: задачи разработки ◀──merge── PR ◀── functional-spec.md ◀──sdd start-system
                │
                └─sdd start-dev──▶ код ──PR──▶ CI + review ──merge──▶ Jira: Done
```

## Запуск у себя — две команды

Нужны Python 3.12+ и [uv](https://docs.astral.sh/uv/). PostgreSQL, если её нет, команда попробует поднять сама (локальный Postgres или Docker).

```bash
uv sync
uv run sdd up
```

Откройте:

- дашборд — http://127.0.0.1:48080/
- мок Jira и Bitbucket — http://127.0.0.1:48090/

Прогнать сценарий целиком (в другом терминале):

```bash
uv run python scripts/demo.py --fault --tasks 2
```

Остановка — Ctrl+C. Файл `.env` для этого не нужен: по умолчанию сервис ходит в моки.

## Подключить свои Jira и Bitbucket

Работает с **Jira / Bitbucket Data Center**. Облако Atlassian не проверялось. Первый раз лучше на тестовых проектах.

Попросите у администратора один раз:

1. Учётку `svc-sdd` в Jira и её Personal Access Token. Права: в проекте идей — Browse + Link Issues; в проектах анализа и разработки — Browse, Create Issues, Link Issues, Add Comments, Transition Issues.
2. Учётку `svc-sdd` в Bitbucket и HTTP access token с правом Read на репозиторий спецификаций и репозиторий кода.
3. Адрес, по которому Bitbucket достучится до сервиса, например `https://sdd.company.ru`. Путь `/webhooks/bitbucket` нельзя закрывать формой логина.

Дальше:

```bash
uv run sdd init --register-webhooks
uv run sdd serve
```

`sdd init` задаст несколько вопросов, запишет `.env` (права 600) и повесит webhook на ваши репозитории. Секрет webhook придумывать не нужно. Labels в Jira не используются.

Если что-то не так — команда напишет, что поправить:

```bash
uv run sdd doctor
```

Неинтерактивно (сервер):

```bash
uv run sdd init --non-interactive --register-webhooks \
  --public-url https://sdd.company.ru \
  --database-url postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd \
  --jira-url https://jira.company.ru --jira-token TOKEN \
  --bitbucket-url https://bitbucket.company.ru --bitbucket-token TOKEN \
  --spec-repo SPEC/business-specs --code-repo LOANS/loan-service \
  --target-branch master \
  --idea-type Epic --analysis-project ARCH --dev-project LOANS --dev-type Story
```

Весь сервис в Docker, когда `.env` уже есть:

```bash
docker compose --profile app up -d --build
```

### Репозиторий спецификаций

Один раз скопируйте шаблон:

```bash
git clone ssh://git@bitbucket.company.ru/spec/business-specs.git
cd business-specs
cp -a /путь/к/ssd-research/openspec-templates/business-specs/. .
git add . && git commit -m "OpenSpec templates" && git push
```

В репозиторий кода положите `AGENTS.md` и `prompts/developer.md` из `openspec-templates/application-repo/` и добавьте `.sdd/` в `.gitignore`.

### Проверка на одной идее

На ноутбуке аналитика — **личный** токен (PR создаётся от его имени):

```bash
export SDD_PUBLIC_URL=https://sdd.company.ru
export SDD_JIRA_BASE_URL=https://jira.company.ru
export SDD_JIRA_TOKEN=личный-токен
export SDD_BITBUCKET_BASE_URL=https://bitbucket.company.ru
export SDD_BITBUCKET_TOKEN=личный-токен

sdd start PROD-15 --author "Иванов И. И."
sdd validate openspec/changes/CHG-1042/business-spec.md --type business --change CHG-1042
sdd open-pr --change CHG-1042 --stage business --jira PROD-15 --project SPEC --repo business-specs
```

В Bitbucket: Approve → Merge. В Jira появится одна задача системного анализа. Повторный webhook вторую не создаёт. Обычные PR без строк `Change ID` / `Stage` сервис молча пропускает.

### Если doctor пишет НЕТ

| Сообщение | Что сделать |
|---|---|
| PostgreSQL | Повторите `uv run sdd up` или поправьте `SDD_DATABASE_URL` |
| Jira 401 / проект не виден | Токен и права учётки `svc-sdd` |
| Типа «Story» нет | Поставьте `SDD_JIRA_DEV_ISSUE_TYPE` в имя из списка, который напечатал doctor |
| Jira просит ещё поля | Вставьте JSON из подсказки doctor в `SDD_JIRA_DEV_EXTRA_FIELDS` |
| Репозиторий не найден | Формат `ПРОЕКТ/имя`, как в URL Bitbucket |
| Webhook ещё не создан | `uv run sdd register-webhook --all` |
| Нет merge commit | Версия Bitbucket не отдаёт `properties.mergeCommit` — нужен администратор |

## Ежедневные команды

```bash
# Бизнес-аналитик (клон business-specs)
sdd start IDEA-123 --author "Иванова А. С."
sdd validate openspec/changes/CHG-1042/business-spec.md --type business --change CHG-1042
sdd open-pr --change CHG-1042 --stage business --jira IDEA-123

# Системный аналитик — после merge и задачи ANL-… в Jira
sdd start-system CHG-1042 --component loan-holidays-service
sdd open-pr --change CHG-1042 --stage system

# Разработчик (клон application-repo) — после задач DEV-… в Jira
sdd start-dev CHG-1042 DEV-231
sdd open-pr --change CHG-1042 --stage implementation --jira DEV-231 --project APP --repo application-repo
```

`sdd open-pr` переиспользует уже открытый PR из той же ветки.

Идемпотентность — маркер `SDD-OP:…` в описании задачи, поле Labels не нужно.

## Схема OpenSpec

Оба документа одного изменения лежат в `openspec/changes/<Change ID>/`. Используется схема `sdd` с артефактами `business-spec` → `functional-spec`. Проверка: `openspec schema validate sdd`.

## Тесты

```bash
uv run pytest
uv run ruff check src tests
```

Тестовая база — `SDD_TEST_DATABASE_URL` (по умолчанию `postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd_test`).
