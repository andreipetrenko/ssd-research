# business-specs

Репозиторий бизнес- и функциональных спецификаций SDD (OpenSpec, схема `sdd`).

- `openspec/config.yaml` — контекст и правила для агентов.
- `openspec/schemas/sdd/` — схема с двумя артефактами: `business-spec` → `functional-spec`.
- `openspec/changes/<Change ID>/` — документы конкретного изменения.
- `openspec/specs/` — действующие (архивированные) спецификации.
- `prompts/` — промпты агентов бизнес-аналитика и системного аналитика.
- `AGENTS.md` — обязательные правила для AI-агентов.

Ветки: `feature/<Change ID>-business-spec`, `feature/<Change ID>-system-spec`. Merge выполняет человек
после ревью; после merge Integration Service создаёт следующую Jira-задачу.
