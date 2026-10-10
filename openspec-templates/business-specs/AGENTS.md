# Инструкции AI-агентам: репозиторий спецификаций

Этот репозиторий содержит бизнес- и функциональные спецификации процесса SDD (OpenSpec, схема `sdd`).
Агент работает в IDE/CLI вместе с бизнес-аналитиком или системным аналитиком; человек проверяет результат
и отвечает за согласование.

## Структура изменения

```
openspec/changes/<Change ID>/
├── business-spec.md     # бизнес-аналитик + агент, ветка feature/<Change ID>-business-spec
└── functional-spec.md   # системный аналитик + агент, ветка feature/<Change ID>-system-spec
```

Шаблоны: `openspec/schemas/sdd/templates/`. Каркас документа и ветку создают команды
`sdd start <IDEA-KEY>` и `sdd start-system <Change ID>`.

## Обязательные правила (раздел 7.3 технического проекта)

1. Не придумывай API, поля данных и бизнес-правила, отсутствующие в источниках.
2. Ссылайся на файлы и разделы, подтверждающие технические факты.
3. Разделяй факты, предложения и допущения. Допущения помечай словом «Допущение:».
4. Не изменяй спецификации вне папки текущего изменения.
5. Критерии приёмки пиши в формате Дано / Когда / Тогда, включая негативные сценарии.
6. Не включай секреты и персональные данные в документы и промпты.
7. Не создавай Jira-задачи напрямую — это делает Integration Service после merge.
8. Не выполняй merge и не меняй права доступа.

## Перед открытием PR

```bash
sdd validate openspec/changes/<Change ID>/business-spec.md --type business --change <Change ID>
sdd validate openspec/changes/<Change ID>/functional-spec.md --type functional --change <Change ID>
sdd open-pr --change <Change ID> --stage business --jira <IDEA-KEY>      # или --stage system
```

Описание PR, которое формирует `sdd open-pr`, содержит Change ID, этап и Jira-ключ — не удаляй эти строки:
по ним Integration Service определяет переход.
