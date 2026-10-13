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

Чтобы подключить сервис к настоящим Jira и Bitbucket Data Center, сразу переходите к разделу
[«Как подключить к настоящим Jira и Bitbucket»](#как-подключить-к-настоящим-jira-и-bitbucket).
Ниже — локальный запуск на моках, без корпоративных систем.

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

## Как подключить к настоящим Jira и Bitbucket

Эта инструкция для человека, который настраивает интеграцию первый раз.
Делайте шаги **по порядку**. После каждого шага есть строка «Готово, если…» — не переходите дальше,
пока она не выполняется.

Что получится в конце: сервис слушает события pull request в Bitbucket. Когда человек сливает
согласованную спецификацию, сервис сам создаёт следующую задачу в Jira. Когда сливают код —
обновляет статус задачи разработки. Jira, Bitbucket и CI не заменяются: доски, workflow и права
команд остаются как были.

Инструкция рассчитана на **Jira Data Center / Server** и **Bitbucket Data Center / Server**
(корпоративный сервер вида `https://jira.компания.ru`). Облако Atlassian (`*.atlassian.net`,
`bitbucket.org`) не проверялось.

**Первый пилот делайте на тестовых проектах Jira и на двух репозиториях**, а не на всём рабочем
пространстве команды. Когда одно изменение пройдёт от идеи до кода — можно подключать боевой репозиторий.

В инструкции ниже везде используются такие примеры. Подставьте свои:

| Что | Пример в инструкции | Как узнать своё значение |
|---|---|---|
| Адрес Jira | `https://jira.company.ru` | То, что в адресной строке браузера, без `/browse/...` |
| Адрес Bitbucket | `https://bitbucket.company.ru` | То же, без `/projects/...` |
| Адрес сервиса | `https://sdd.company.ru` | Имя, которое вы сами зададите на шаге 4 |
| Проект идей | ключ `PROD` | Буквы в ключе задачи, например у `PROD-15` ключ проекта — `PROD` |
| Тип задачи-идеи | `Epic` | Поле «Тип» на карточке задачи |
| Проект системного анализа | `ARCH` | Куда обычно ставят задачи аналитикам |
| Тип задачи анализа | `Task` | Как в Jira называется обычная задача в этом проекте |
| Проект разработки | `LOANS` | Проект команды в Jira |
| Тип задачи разработки | `Story` | Как у вас заводят работу разработчику |
| Репозиторий спецификаций | проект `SPEC`, репозиторий `business-specs` | Создадите на шаге 2 |
| Репозиторий кода | проект `LOANS`, репозиторий `loan-service` | URL вида `.../projects/LOANS/repos/loan-service` |
| Целевая ветка | `master` | Ветка, в которую у вас вливают PR (часто `master` или `develop`) |

### Шаг 1. Создайте сервисную учётку в Jira и выдайте права

Сервису нужна **отдельная** учётка. Не используйте личный логин: токен попадёт на сервер,
а при увольнении человека интеграция сломается.

1. Попросите администратора Jira создать пользователя, например `svc-sdd` (отображаемое имя
   «SDD Orchestrator»). Почта — служебная, не личная.
2. Войдите в Jira **этим** пользователем (или попросите админа) и создайте токен:
   - откройте аватар справа вверху → **Profile**;
   - слева найдите **Personal Access Tokens** (в части версий: **Account settings → Security**);
   - **Create token**;
   - имя: `sdd-orchestrator`;
   - срок — по политике компании (лучше с датой окончания, занесите её в календарь);
   - создайте токен и **сразу скопируйте** его в менеджер паролей. Повторно Jira его не покажет.
3. Выдайте этой учётке права в трёх проектах (Project settings → Permissions, или через
   permission scheme). Нужны не «админ проекта», а конкретные действия:

   | Проект | Что должна уметь учётка | Зачем |
   |---|---|---|
   | Идей (`PROD`) | Browse Projects, Link Issues | Прочитать идею и связать с задачей анализа |
   | Анализа (`ARCH`) | Browse Projects, Create Issues, Link Issues, Add Comments | Создать задачу системному аналитику |
   | Разработки (`LOANS`) | Browse Projects, Create Issues, Link Issues, Add Comments, Transition Issues | Создать задачи разработки и после merge кода перевести их в «Готово» |

4. Проверьте, что учётка вообще видит проект: выйдите из своего аккаунта, войдите как `svc-sdd`,
   откройте `https://jira.company.ru/browse/PROD` (подставьте свой ключ). Должен открыться проект,
   а не «у вас нет прав».

**Готово, если:** токен лежит в менеджере паролей, `svc-sdd` открывает три нужных проекта.

### Шаг 2. Подготовьте типы задач и экраны в Jira

Сервис создаёт задачи так, как если бы человек нажал Create в Jira. Если на экране создания
обязательное поле, которого сервис не заполняет, Jira ответит ошибкой и задача не появится.

1. Откройте **Create** в проекте идей. Запомните:
   - точное имя типа задачи (как в выпадающем списке: `Epic`, `Story`, `Business Idea` — буква в букву);
   - какие поля помечены звёздочкой как обязательные.
   Имя типа потом пойдёт в `SDD_JIRA_IDEA_ISSUE_TYPE`, обязательные поля — в
   `SDD_JIRA_IDEA_REQUIRED_FIELDS`. Для начала хватит `summary,description`. Если кроме них
   обязателен ещё компонент или кастомное поле — запишите его **id** (см. пункт 5).
2. Повторите Create в проекте анализа и в проекте разработки. Запишите имена типов
   (`Task`, `Story` и т.д.).
3. На экране создания задач анализа и разработки должно быть поле **Labels** (Метки).
   Сервис ставит служебную метку вида `sdd-op-a1b2c3…` и по ней потом находит задачу,
   если Jira «отвалилась» сразу после создания. Без Labels создание сломается с HTTP 400.
   - как проверить: Create → если Labels нет на форме, попросите администратора Jira добавить
     поле Labels на **Create screen** этого типа задачи;
   - как проверить после правки: снова Create под `svc-sdd` — поле видно.
4. Узнайте, как у вас называются связи между задачами. В адресной строке браузера, будучи
   залогиненым в Jira, откройте:

   `https://jira.company.ru/rest/api/2/issueLinkType`

   В JSON найдите `"name": "Relates"` и `"name": "Blocks"`. Если имена другие
   (например, «Связывает» / «Блокирует») — запишите их: они пойдут в
   `SDD_JIRA_RELATES_LINK_TYPE` и `SDD_JIRA_BLOCKS_LINK_TYPE`.
5. Если при Create в проекте разработки Jira требует компонент, Epic Link или кастомное поле:
   - откройте любую существующую задачу этого типа;
   - в адресной строке замените путь на
     `https://jira.company.ru/rest/api/2/issue/LOANS-1?expand=names`
     (подставьте ключ реальной задачи);
   - в JSON найдите поле: обычные называются `components`, кастомные — `customfield_12345`.
   Эти пары «имя → значение» потом вставите в `SDD_JIRA_DEV_EXTRA_FIELDS` как JSON.
6. Узнайте имя кнопки, которой задачу разработки закрывают. Откройте задачу в статусе
   «в работе» и посмотрите список переходов (кнопки вверху карточки): `Done`, `Готово`,
   `Resolve Issue`. Нужно **имя кнопки**, не имя статуса. Его запишите в
   `SDD_JIRA_DONE_TRANSITION`. Если сервис не должен менять статус — оставьте поле пустым.

**Готово, если:** вы знаете ключи трёх проектов, точные имена трёх типов задач, на Create
экране анализа и разработки видно Labels, и у вас записаны имена связей и перехода «готово».

### Шаг 3. Создайте сервисную учётку в Bitbucket

1. Попросите администратора Bitbucket создать пользователя `svc-sdd` (можно то же имя, что в Jira —
   это разные системы, учётки не общие).
2. Войдите как `svc-sdd` и создайте токен: аватар → **Manage account** → **HTTP access tokens**
   (в части версий: **Personal access tokens**) → **Create token**.
   - имя: `sdd-orchestrator`;
   - права: **Project read** + возможность комментировать pull request. Merge, admin и удаление
     репозитория **не нужны** — сервис сам ничего не сливает;
   - скопируйте токен в менеджер паролей.
3. Выдайте `svc-sdd` доступ **Read** на два репозитория (шаг 5 и шаг 6):
   Repository settings → **Permissions** → Add user `svc-sdd` → Read.
   Комментарии к PR обычно входят в Read; если бот не сможет комментировать, поднимите до Write.

**Готово, если:** токен Bitbucket сохранён, `svc-sdd` открывает оба будущих репозитория без 404.

### Шаг 4. Поднимите сервис во внутренней сети

Сервис — это одна программа. Ей нужны PostgreSQL и HTTPS-адрес, до которого дотянется Bitbucket.

1. Возьмите Linux-сервер или контейнер во **внутренней** сети (не в интернет).
2. Попросите инфраструктуру:
   - DNS-имя, например `sdd.company.ru`, которое смотрит на этот сервер;
   - сертификат HTTPS (внутренний CA подойдёт, если Bitbucket ему доверяет);
   - reverse proxy (nginx / ingress) с TLS на 443.
3. Установите PostgreSQL (отдельная база, не общая «свалка» без схемы). Под пользователем postgres:

   ```bash
   createuser sdd
   createdb -O sdd sdd
   psql -c "ALTER USER sdd WITH PASSWORD 'придумайте-сложный-пароль';"
   ```

   Если база уже есть и вам дали только схему `sdd` внутри неё, строка подключения будет такой:

   `postgresql+psycopg://sdd:ПАРОЛЬ@db.company.ru:5432/platform?options=-csearch_path%3Dsdd`

4. На сервере должны открываться исходящие HTTPS до Jira и Bitbucket и входящий 443 от Bitbucket
   до сервиса. С машины сервиса проверьте:

   ```bash
   curl -I https://jira.company.ru
   curl -I https://bitbucket.company.ru
   ```

   Оба должны ответить не «connection refused» и не «Could not resolve host».
5. На proxy закройте дашборд и API логином (SSO или Basic Auth): пути `/`, `/api/*`, `/docs`,
   `/metrics`. Путь **`/webhooks/bitbucket` закрывать формой логина нельзя** — Bitbucket не умеет
   вводить пароль. Его защищает секрет подписи (шаг 7).
6. Скопируйте этот репозиторий на сервер, установите Python 3.12+ и [uv](https://docs.astral.sh/uv/):

   ```bash
   git clone https://github.com/andreipetrenko/ssd-research.git
   cd ssd-research
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv sync
   ```

7. Создайте файл настроек (ещё не заполняйте токены — это шаг 8):

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

   Файл `.env` **не коммитьте** и не кладите в общий чат.

**Готово, если:** по `https://sdd.company.ru` proxy отвечает (пока хоть ошибкой 502 — значит DNS
и сертификат живые), PostgreSQL принимает подключение пользователя `sdd`, с сервера открываются
Jira и Bitbucket.

### Шаг 5. Создайте репозиторий спецификаций в Bitbucket

Это отдельный git-репозиторий, в нём будут только документы, не код приложения.

1. В Bitbucket откройте нужный проект (или создайте проект `SPEC`) → **Create repository**.
   Имя: `business-specs`. Ветка по умолчанию — та же, что целевая (`master` или `develop`).
2. Склонируйте пустой репозиторий к себе и скопируйте в него шаблон из этого проекта:

   ```bash
   git clone ssh://git@bitbucket.company.ru/spec/business-specs.git
   cd business-specs
   cp -a /путь/к/ssd-research/openspec-templates/business-specs/. .
   git add .
   git commit -m "Initial OpenSpec templates for SDD"
   git push -u origin master    # или origin develop — как у вас называется основная ветка
   ```

3. Repository settings → **Branch permissions** для `master`/`develop`:
   - запретите прямой push всем, кроме, при необходимости, администраторов;
   - включите merge check «минимум 1 approval» (или столько, сколько укажете в `SDD_MIN_APPROVALS`).
4. Repository settings → **Default reviewers**: добавьте того, кто согласовывает бизнес- и
   системные спецификации.
5. Убедитесь, что `svc-sdd` имеет Read на этот репозиторий (шаг 3).

**Готово, если:** в браузере открывается
`https://bitbucket.company.ru/projects/SPEC/repos/business-specs/browse` и видны папки
`openspec/` и `prompts/`.

### Шаг 6. Добавьте файлы агента в репозиторий кода

Код приложения остаётся в своём репозитории. Туда нужно положить три файла из шаблона,
чтобы разработчик запускал агента одинаково.

1. Склонируйте пилотный репозиторий кода.
2. Скопируйте из `openspec-templates/application-repo/` файлы:
   - `AGENTS.md` — в корень;
   - `openspec/config.yaml` — в `openspec/config.yaml` (папку создайте, если её нет);
   - `prompts/developer.md` — в `prompts/developer.md`.
3. В `.gitignore` добавьте строку `.sdd/` (там будет локальный контекст агента, в git он не нужен).
4. Закоммитьте и влейте обычным PR. Это разовая подготовка, не часть процесса SDD.

**Готово, если:** в репозитории кода на основной ветке есть `AGENTS.md` и `prompts/developer.md`.

### Шаг 7. Заполните `.env` на сервере

Откройте `.env` на сервере сервиса и замените значения. Строки с `#` — комментарии, их можно не трогать.

```bash
# Куда Bitbucket будет стучаться. Без слэша в конце.
SDD_PUBLIC_URL=https://sdd.company.ru
SDD_HOST=0.0.0.0
SDD_PORT=48080

# База, которую создали на шаге 4
SDD_DATABASE_URL=postgresql+psycopg://sdd:ПАРОЛЬ@127.0.0.1:5432/sdd

# Bitbucket. Токен — тот, что создали под svc-sdd на шаге 3.
SDD_BITBUCKET_BASE_URL=https://bitbucket.company.ru
SDD_BITBUCKET_TOKEN=вставьте-токен-bitbucket
# Секрет webhook придумайте сами (длинная случайная строка) и сохраните — он же понадобится в UI Bitbucket.
SDD_BITBUCKET_WEBHOOK_SECRET=придумайте-длинную-случайную-строку

# Формат: КЛЮЧ_ПРОЕКТА/имя-репозитория  (как в URL Bitbucket)
SDD_SPEC_REPOSITORIES=SPEC/business-specs
SDD_CODE_REPOSITORIES=LOANS/loan-service
SDD_TARGET_BRANCHES=master
SDD_MIN_APPROVALS=1
# Если ваш CI не пишет статус сборки в Bitbucket — поставьте false, иначе задачи не закроются.
SDD_REQUIRE_CI=true

# Jira. Токен — тот, что создали под svc-sdd на шаге 1.
SDD_JIRA_BASE_URL=https://jira.company.ru
SDD_JIRA_TOKEN=вставьте-токен-jira
SDD_JIRA_IDEA_ISSUE_TYPE=Epic
SDD_JIRA_IDEA_REQUIRED_FIELDS=summary,description
SDD_JIRA_ANALYSIS_PROJECT=ARCH
SDD_JIRA_ANALYSIS_ISSUE_TYPE=Task
SDD_JIRA_DEV_PROJECT=LOANS
SDD_JIRA_DEV_ISSUE_TYPE=Story
SDD_JIRA_RELATES_LINK_TYPE=Relates
SDD_JIRA_BLOCKS_LINK_TYPE=Blocks
SDD_JIRA_DONE_TRANSITION=Готово
```

Пояснения, которые чаще всего нужны:

- Если Jira при создании Story требует компонент, добавьте строку
  `SDD_JIRA_DEV_EXTRA_FIELDS={"components":[{"name":"loan-service"}]}`
  — имя компонента должно совпадать с тем, что в выпадающем списке Jira.
- Если UI Jira открывается с другого адреса, чем API, укажите `SDD_JIRA_BROWSE_URL`.
  Иначе оставьте пустым.
- Если у вас нет PAT и разрешён только логин/пароль, вместо `SDD_JIRA_TOKEN` заполните
  `SDD_JIRA_USERNAME` и `SDD_JIRA_PASSWORD` (то же для Bitbucket). Если заполнен токен,
  логин и пароль игнорируются.
- Несколько репозиториев или веток пишутся через запятую без пробелов:
  `SDD_TARGET_BRANCHES=master,develop`.

**Готово, если:** в `.env` нет значений `http://127.0.0.1:48090` и нет пустых токенов.

### Шаг 8. Запустите сервис и проверьте, что он живой

```bash
cd ssd-research
uv run sdd serve
```

В логе не должно быть traceback. В другом окне:

```bash
curl -s https://sdd.company.ru/health
```

Ожидается JSON вида `{"status":"ok","database":"ok","retry_worker":true}`.
Если `database` не `ok` — неверный `SDD_DATABASE_URL` или PostgreSQL не запущен.

Затем:

```bash
uv run sdd check
```

Ожидается, что по проектам Jira команда напишет `OK` напротив ваших типов задач.
Строка про webhook Bitbucket может сказать «НЕТ» — это нормально: `sdd check` ищет webhook
на уровне **проекта**, а вы повесите его на **репозиторий** на следующем шаге.

Через браузер откройте `https://sdd.company.ru/` (после логина на proxy). Должен открыться
дашборд «SDD Orchestrator», в шапке — «PostgreSQL: доступна».

Чтобы сервис переживал перезагрузку сервера, поставьте его под systemd или в Docker:

```bash
docker build -t sdd-orchestrator .
docker run -d --name sdd --restart unless-stopped \
  -p 127.0.0.1:48080:48080 --env-file /путь/к/.env sdd-orchestrator
```

Proxy в этом случае смотрит на `127.0.0.1:48080`.

**Готово, если:** `/health` отвечает `ok`, дашборд открывается, `sdd check` видит типы задач Jira.

### Шаг 9. Повесьте webhook на каждый репозиторий

Webhook — это «звонок» из Bitbucket в сервис: «PR открыли / одобрили / слили».
Вешайте его **на репозиторий**, не на весь проект: иначе сервис получит события всех PR команды.

Повторите для `business-specs` и для репозитория кода.

1. Откройте репозиторий в Bitbucket → шестерёнка **Repository settings** → **Webhooks** →
   **Create webhook**.
2. Заполните:
   - Name: `SDD Orchestrator`;
   - URL: `https://sdd.company.ru/webhooks/bitbucket` (ваш `SDD_PUBLIC_URL` + этот путь);
   - Secret: **точно та же** строка, что в `SDD_BITBUCKET_WEBHOOK_SECRET` (лишний пробел сломает проверку);
   - Active: включено;
   - SSL/TLS: включено, если у вас нормальный сертификат.
3. Отметьте события (названия в UI могут быть чуть другими, смысл тот же):
   - Pull request opened
   - Pull request from-ref updated (обновление исходной ветки)
   - Pull request merged
   - Pull request declined
   - Pull request reviewer needs work
   - Pull request reviewer approved
4. Сохраните. Нажмите **Test** / **View recent deliveries** и посмотрите код ответа.
   Сервис на проверочный ping отвечает `{"status":"pong"}` и HTTP 200.
   - `401 invalid webhook signature` — секрет в UI и в `.env` различаются, перезапустите сервис
     после правки `.env`;
   - `Connection refused` / timeout — Bitbucket не доходит до `sdd.company.ru` (firewall, DNS, TLS);
   - `404` — в URL опечатка, нужен именно путь `/webhooks/bitbucket`.

Команда `uv run sdd register-webhook --project SPEC` создаёт webhook на **весь проект**.
Для пилота в живом пространстве команды её лучше не использовать.

**Готово, если:** у обоих репозиториев в списке webhook есть `SDD Orchestrator`, тест отвечает 200.

### Шаг 10. Проверьте, что ваша версия Bitbucket отдаёт merge commit

После слияния PR сервис ещё раз читает его через API. Ему нужно поле с хешем merge-коммита.

1. В Bitbucket откройте любой **уже слитый** PR (можно старый, из любого репозитория).
   Запомните число в URL — это id, например `42`.
2. На своей машине (нужен ваш личный или сервисный токен):

   ```bash
   curl -sS -H "Authorization: Bearer ТОКЕН_BITBUCKET" \
     "https://bitbucket.company.ru/rest/api/latest/projects/LOANS/repos/loan-service/pull-requests/42" \
     | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('state'), (d.get('properties') or {}).get('mergeCommit'))"
   ```

3. Ожидается что-то вроде `MERGED {'id': 'abc123...', ...}`.
   Если `None` — напишите администратору Bitbucket: в этой версии в REST нет `properties.mergeCommit`.
   Пока поле не появится, сервис не сможет надёжно обработать merge (будет повторять попытки).

**Готово, если:** команда выше печатает `MERGED` и объект с `id`.

### Шаг 11. Решите вопрос с CI

После слияния PR с кодом сервис спрашивает Bitbucket: «сборка этого коммита зелёная?».

1. Откройте любой недавний PR в репозитории кода. Если под описанием или на вкладке Builds
   есть статус Bamboo/Jenkins — CI уже пишет в Bitbucket, оставляйте `SDD_REQUIRE_CI=true`.
2. Если статусов нет:
   - либо попросите тех, кто сопровождает CI, включить публикацию Build Status
     (у Bamboo это обычно уже есть, у Jenkins — плагин Bitbucket Server Integration);
   - либо в `.env` поставьте `SDD_REQUIRE_CI=false` и перезапустите сервис.
     Тогда задача закроется после merge и approvals, без проверки сборки.

Для репозитория спецификаций сборка не обязательна. Если хотите, чтобы «дырявый» документ
нельзя было слить, добавьте в pipeline:

```bash
uv run sdd validate openspec/changes/CHG-XXXX/business-spec.md --type business --change CHG-XXXX
```

и сделайте этот job required build в Bitbucket.

**Готово, если:** либо статусы CI видны на PR, либо в `.env` стоит `SDD_REQUIRE_CI=false`.

### Шаг 12. Поставьте CLI себе на ноутбук

Команды `sdd start` и `sdd open-pr` аналитик и разработчик запускают у себя.
PR создаётся **от их имени**, поэтому здесь нужен **личный** токен, не `svc-sdd`.

1. Установите Python 3.12+ и uv (как на шаге 4).
2. Установите CLI из этого репозитория:

   ```bash
   uv tool install git+https://github.com/andreipetrenko/ssd-research.git
   sdd --help
   ```

   Если `uv tool` недоступен:

   ```bash
   git clone https://github.com/andreipetrenko/ssd-research.git
   cd ssd-research && uv sync
   alias sdd='uv run sdd'     # или добавляйте `uv run` перед каждой командой
   ```

3. Создайте **личный** PAT в Jira и в Bitbucket (как на шагах 1 и 3, но под своим логином).
   В Bitbucket личному токену нужно право **создавать PR** в двух репозиториях.
4. Пропишите в `~/.bashrc` или `~/.zshrc` (и откройте новый терминал):

   ```bash
   export SDD_PUBLIC_URL=https://sdd.company.ru
   export SDD_BITBUCKET_BASE_URL=https://bitbucket.company.ru
   export SDD_BITBUCKET_TOKEN=ваш-личный-токен-bitbucket
   export SDD_JIRA_BASE_URL=https://jira.company.ru
   export SDD_JIRA_TOKEN=ваш-личный-токен-jira
   ```

**Готово, если:** `sdd --help` печатает список команд, в переменных окружения нет токена `svc-sdd`.

### Шаг 13. Прогоните одно тестовое изменение от идеи до кода

Делайте на тестовой идее. Не берите живую боевую задачу.

1. В Jira создайте задачу того типа, который указали в `SDD_JIRA_IDEA_ISSUE_TYPE`.
   Заполните все обязательные поля (как минимум название и описание). Запомните ключ, например `PROD-15`.
2. Склонируйте `business-specs` и в его каталоге выполните:

   ```bash
   sdd start PROD-15 --author "Иванов И. И."
   ```

   Ожидается: напечатан Change ID (`CHG-1042` или следующий номер), создана ветка
   `feature/CHG-1042-business-spec`, появился файл
   `openspec/changes/CHG-1042/business-spec.md`.
   На дашборде сервиса в списке «Изменения» появилась карточка.

   Если команда ругается, что не заполнено поле — допишите его в Jira и повторите `sdd start`
   (повторно новый Change ID не выдаст, вернёт тот же).
3. Откройте `business-spec.md` и заполните все разделы (сами или с агентом по
   `prompts/business-analyst.md`). Плейсхолдеры в угловых скобках `<...>` и слова TODO быть не должны.
   Проверка:

   ```bash
   sdd validate openspec/changes/CHG-1042/business-spec.md --type business --change CHG-1042
   ```

   Должно напечатать `OK`. Если список замечаний — исправьте файл и повторите, пока не станет OK.
4. Откройте PR:

   ```bash
   sdd open-pr --change CHG-1042 --stage business --jira PROD-15 --project SPEC --repo business-specs
   ```

   Команда сама допишет в описание PR строки `Change ID`, `Stage` и `Jira`. Не удаляйте их.
   В Bitbucket появится PR. На дашборде у изменения этап станет «на ревью».
5. В Bitbucket: ревьюер нажимает **Approve**, затем **Merge** в `master`/`develop`.
   Через несколько секунд в Jira проекте анализа должна появиться **ровно одна** новая задача
   с заголовком вроде `[SDD] Системный анализ: CHG-1042 — …`.
   В описании — Change ID, ссылка на файл спецификации и commit.
6. Проверьте, что повтор не плодит задачи. В Bitbucket: Repository settings → Webhooks →
   ваш webhook → недавняя доставка `pr:merged` → **Resend**.
   В Jira по-прежнему одна задача анализа. На дашборде число «Предотвращено дубликатов» выросло.
7. В каталоге `business-specs`:

   ```bash
   sdd start-system CHG-1042 --component loan-service
   ```

   Заполните `functional-spec.md` (или возьмите образец `examples/credit-holidays/functional-spec.md`
   и замените `{{CHANGE_ID}}`, `{{BUSINESS_COMMIT}}`, `{{ANALYSIS_KEY}}` на значения с дашборда).
   В разделе «Декомпозиция» оставьте задачи в виде:

   ```
   - DEV-1: Короткое название
   - DEV-2: Вторая задача
   - Зависимости:
     - DEV-2 после DEV-1
   ```

   ```bash
   sdd validate openspec/changes/CHG-1042/functional-spec.md --type functional --change CHG-1042
   sdd open-pr --change CHG-1042 --stage system --project SPEC --repo business-specs
   ```

   Снова Approve → Merge. В проекте разработки появятся задачи `LOANS-…` (по одной на каждую
   строку DEV-N), связанные с задачей анализа.
8. Склонируйте репозиторий кода, возьмите ключ первой задачи разработки (например `LOANS-88`):

   ```bash
   sdd start-dev CHG-1042 LOANS-88
   ```

   Появится ветка `feature/CHG-1042-LOANS-88-implementation` и файл `.sdd/context.md`.
   Сделайте минимальное изменение (хоть комментарий) и тесты по желанию, затем:

   ```bash
   sdd open-pr --change CHG-1042 --stage implementation --jira LOANS-88 \
     --project LOANS --repo loan-service
   ```

   Дождитесь зелёного CI (если не отключали), Approve, Merge.
   Задача `LOANS-88` должна перейти в статус, который вы указали в `SDD_JIRA_DONE_TRANSITION`.
   На дашборде у изменения этап «Разработка», пока не закрыты остальные DEV-задачи;
   когда закроете все — «Готово».

**Готово, если:** одно изменение видно на дашборде со связкой идея → два PR спецификаций →
задачи разработки → PR с кодом, и повторный Resend webhook не создал вторую задачу анализа.

### Если что-то пошло не так

| Что видите | Что проверить первым |
|---|---|
| `sdd start`: 404, задача не найдена | Ключ идеи опечатан; `svc-sdd` не видит проект; неверный `SDD_JIRA_BASE_URL` |
| `sdd start`: не заполнено поле | Откройте задачу в Jira и заполните поля из `SDD_JIRA_IDEA_REQUIRED_FIELDS` |
| `sdd start`: неверный тип задачи | Имя в `SDD_JIRA_IDEA_ISSUE_TYPE` должно совпадать с типом **буква в букву** |
| Дашборд пустой после merge | Webhook: Recent deliveries — был ли `pr:merged` и какой HTTP-код |
| Webhook 401 | Секрет в UI Bitbucket ≠ `SDD_BITBUCKET_WEBHOOK_SECRET`, сервис не перезапускали |
| Webhook timeout | С Bitbucket нет маршрута до `sdd.company.ru`, или proxy режет `/webhooks/bitbucket` |
| Задача в Jira не создалась, на дашборде событие `failed` | Откройте событие: в тексте ошибки Jira обычно пишет, какое поле обязательно. Добавьте его в `SDD_JIRA_*_EXTRA_FIELDS` и нажмите «Обработать повторно» |
| Задача создалась, но после Resend появилась вторая | На Create-экране нет Labels, сверка по метке не сработала. Добавьте Labels и не создавайте задачи руками «на всякий случай» |
| Событие `rejected`, комментарий «PR не принят» | В описании PR нет строк `Change ID:` и `Stage:`. Откройте PR через `sdd open-pr`, не руками |
| После merge кода задача не закрылась | Нет approvals; CI не `SUCCESSFUL` (или поставьте `SDD_REQUIRE_CI=false`); имя перехода в `SDD_JIRA_DONE_TRANSITION` не совпадает с кнопкой в Jira |
| `sdd check` пишет, что webhook нет | Для репозиторного webhook это ожидаемо. Смотрите UI репозитория, не вывод `sdd check` |

Логи сервиса печатаются в тот терминал, где запущен `sdd serve` (или `docker logs sdd`).
Ищите строку с `bitbucket pr:` и номером PR.

На дашборде справа — журнал событий. У упавшего события есть кнопка **«Обработать повторно»**:
она ещё раз читает актуальный PR из Bitbucket и заново вызывает Jira. Базу руками править не нужно.

### Чего сервис пока не умеет (чтобы не удивиться)

- Любой PR в подключённом репозитории **без** строк `Change ID` / `Stage` получит комментарий
  «PR не принят в процесс». Поэтому пилот — на отдельном `business-specs` и на заранее
  оговорённом репозитории кода, не на общем репозитории, где каждый день идут обычные PR.
- Все задачи разработки создаются в одном проекте (`SDD_JIRA_DEV_PROJECT`). Разные команды
  с разными пространствами Jira из коробки не разводятся.
- Дашборд сам по себе без пароля. Закрывайте его на reverse proxy (шаг 4).
- Сервис не запускает AI-агентов и не нажимает Merge. Агента запускает человек в IDE,
  Merge нажимает ревьюер в Bitbucket.

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
