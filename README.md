# SDD Orchestrator

Сервис, который ведёт изменение от идеи в Jira до кода:

1. Бизнес-аналитик берёт идею, пишет спецификацию, открывает PR.
2. После merge сервис создаёт задачу системного анализа в Jira.
3. Системный аналитик пишет функциональную спецификацию, открывает PR.
4. После merge сервис создаёт задачи разработки.
5. Разработчик пишет код с агентом в IDE, открывает PR. После merge задача закрывается.

Агенты живут у людей в IDE. Этот сервис только слушает Bitbucket, проверяет PR и создаёт задачи в Jira — каждый раз ровно одну.

Работает с **Jira Data Center** и **Bitbucket Data Center**.

---

## Попробовать у себя

Нужны Python 3.12+ и [uv](https://docs.astral.sh/uv/). PostgreSQL, если её нет, команда попробует найти или поднять сама.

```bash
git clone https://github.com/andreipetrenko/ssd-research.git
cd ssd-research
uv sync
uv run sdd up
```

Откройте в браузере:

| Что | Адрес |
|---|---|
| Дашборд сервиса | http://127.0.0.1:48080/ |
| Мок Jira и Bitbucket | http://127.0.0.1:48090/ |

`.env` не нужен. Остановка — Ctrl+C.

В другом терминале можно прогнать весь процесс автоматически:

```bash
uv run python scripts/demo.py --fault --tasks 2
```

На моке останется одна задача разработки — её можно довести руками: CI → Одобрить → Merge.

---

## Подключить свои Jira и Bitbucket

Делайте первый раз на тестовых проектах, не на проде.

### Что попросить у администратора

Три вещи, один раз:

1. Учётка `svc-sdd` в **Jira** и её Personal Access Token.
   Права: в проекте идей — Browse + Link Issues; в проектах анализа и разработки — Browse, Create Issues, Link Issues, Add Comments, Transition Issues.
2. Учётка `svc-sdd` в **Bitbucket** и HTTP access token.
   Права: Read на репозиторий спецификаций и на репозиторий кода (читать PR, писать комментарии).
3. Адрес, по которому Bitbucket достучится до этого сервиса, например `https://sdd.company.ru`.
   Путь `/webhooks/bitbucket` нельзя закрывать формой логина.
4. PostgreSQL — см. ниже. Новую ставить не обязательно, если база уже есть.

Поле Labels в Jira настраивать не нужно.

### PostgreSQL

Сервису нужна PostgreSQL. Это не база Jira и не Bitbucket: отдельная база (или хотя бы место, куда можно писать таблицы `sdd_*`).

Таблицы **создаются при запуске** (`sdd serve` / `sdd up`). SQL руками накатывать не нужно: при старте выполняется `migrations/001_init.sql`. Если таблицы уже есть, повторный запуск их не пересоздаёт.

Если Postgres уже есть, в `sdd init` на вопрос «Строка PostgreSQL» вставьте свою строку:

```
postgresql+psycopg://логин:пароль@хост:5432/имя_базы
```

Пользователя именно `sdd` создавать не обязательно — подойдёт любая учётка, которой вам уже дали доступ.

Этой учётке нужно право **создавать таблицы** в выбранной базе. Если такого права нет, администратор БД один раз выполняет `migrations/001_init.sql` и выдаёт доступ к получившимся объектам:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ваш_логин;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ваш_логин;
```

После этого `sdd doctor` должен показать `[OK] PostgreSQL: подключение есть`.

### Запуск

```bash
uv sync
uv run sdd init --register-webhooks
uv run sdd serve
```

`sdd init` спросит адреса, токены, проекты и репозитории, запишет `.env` и повесит webhook. Секрет webhook придумывать не нужно.

Проверка:

```bash
uv run sdd doctor
```

Если есть пункт `[НЕТ]` — справа написано, что сделать. После правки `.env` снова `sdd doctor`, затем перезапустите `sdd serve`.

### Шаблоны в репозиториях (один раз)

Репозиторий спецификаций:

```bash
git clone ssh://git@bitbucket.company.ru/spec/business-specs.git
cd business-specs
cp -a /путь/к/ssd-research/openspec-templates/business-specs/. .
git add . && git commit -m "OpenSpec templates" && git push
```

В репозиторий кода скопируйте `AGENTS.md` и `prompts/developer.md` из `openspec-templates/application-repo/` и добавьте `.sdd/` в `.gitignore`.

### Первая идея

На ноутбуке аналитика — **личный** токен, не сервисный (PR должен быть от его имени):

```bash
export SDD_PUBLIC_URL=https://sdd.company.ru
export SDD_JIRA_BASE_URL=https://jira.company.ru
export SDD_JIRA_TOKEN=личный-токен
export SDD_BITBUCKET_BASE_URL=https://bitbucket.company.ru
export SDD_BITBUCKET_TOKEN=личный-токен

sdd start PROD-15 --author "Иванов И. И."
```

Дальше агент заполняет `business-spec.md` по `prompts/business-analyst.md`. Затем:

```bash
sdd validate openspec/changes/CHG-1042/business-spec.md --type business --change CHG-1042
sdd open-pr --change CHG-1042 --stage business --jira PROD-15 --project SPEC --repo business-specs
```

В Bitbucket: Approve → Merge. В Jira должна появиться **одна** задача системного анализа. Повторная доставка webhook вторую не создаёт.

Обычные PR без строк `Change ID` / `Stage` сервис не трогает и комментарий не пишет.

---

## Как работать каждый день

```bash
# Бизнес-аналитик — в клоне репозитория спецификаций
sdd start IDEA-123 --author "Иванова А. С."
sdd validate openspec/changes/CHG-1042/business-spec.md --type business --change CHG-1042
sdd open-pr --change CHG-1042 --stage business --jira IDEA-123

# Системный аналитик — после merge и появления задачи ANL-… в Jira
sdd start-system CHG-1042 --component loan-holidays-service
sdd open-pr --change CHG-1042 --stage system

# Разработчик — в клоне репозитория кода, после появления задач DEV-… в Jira
sdd start-dev CHG-1042 DEV-231
sdd open-pr --change CHG-1042 --stage implementation --jira DEV-231 --project APP --repo application-repo
```

`sdd open-pr` можно повторять: уже открытый PR из той же ветки он не дублирует.

Спецификации одного изменения лежат в `openspec/changes/<Change ID>/`.

---

## Если doctor пишет НЕТ

| Сообщение | Что сделать |
|---|---|
| PostgreSQL | Поправить `SDD_DATABASE_URL` (логин, пароль, хост, имя базы). Если учётка не может создавать таблицы — попросить администратора накатить `migrations/001_init.sql` |
| Jira 401 / проект не виден | Токен и права учётки `svc-sdd` |
| Типа «Story» нет | Поставить `SDD_JIRA_DEV_ISSUE_TYPE` в имя из списка, который напечатал doctor |
| Jira просит ещё поля | Вставить JSON из подсказки doctor в `SDD_JIRA_DEV_EXTRA_FIELDS` |
| Репозиторий не найден | Формат `ПРОЕКТ/имя`, как в URL Bitbucket |
| Webhook ещё не создан | `uv run sdd register-webhook --all` |
| Нет merge commit | Эта версия Bitbucket не отдаёт `properties.mergeCommit` — нужен администратор |

---

## Другие способы запуска

На сервере, без вопросов:

```bash
uv run sdd init --non-interactive --register-webhooks \
  --public-url https://sdd.company.ru \
  --database-url postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd \
  --jira-url https://jira.company.ru --jira-token TOKEN \
  --bitbucket-url https://bitbucket.company.ru --bitbucket-token TOKEN \
  --spec-repo SPEC/business-specs --code-repo LOANS/loan-service \
  --target-branch master \
  --idea-type Epic --analysis-project ARCH --dev-project LOANS --dev-type Story
uv run sdd serve
```

В Docker, когда `.env` уже есть:

```bash
docker compose --profile app up -d --build
```

---

## Тесты

```bash
uv run pytest
uv run ruff check src tests
```

Тестовая база: `SDD_TEST_DATABASE_URL` (по умолчанию `postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd_test`).
