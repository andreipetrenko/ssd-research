"""Parsing and validation of business and functional specifications (templates from section 7)."""

import re
from dataclasses import dataclass, field

from ..domain import ISSUE_KEY_RE, ArtifactType, change_id_pattern

BUSINESS_SECTIONS = [
    "Проблема",
    "Цель и ожидаемый эффект",
    "Текущий процесс",
    "Целевой процесс",
    "Область изменения",
    "Пользовательские сценарии",
    "Бизнес-правила",
    "Критерии приёмки",
    "Ограничения и допущения",
    "Открытые вопросы",
    "Согласование",
]
BUSINESS_OPTIONAL_CONTENT = {"Открытые вопросы", "Согласование"}

FUNCTIONAL_SECTIONS = [
    "Связанные требования",
    "Затрагиваемые компоненты",
    "Функциональное поведение",
    "API и контракты",
    "Данные и миграции",
    "Безопасность и нефункциональные требования",
    "Тестирование",
    "Критерии готовности",
    "Декомпозиция",
]

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<[А-Яа-яЁё][^<>\n]{1,80}>|\bTODO\b|\bTBD\b")
_GWT_RE = re.compile(r"\b(Given|Дано)\b.*?\b(When|Когда)\b.*?\b(Then|Тогда)\b", re.IGNORECASE | re.DOTALL)
_TASK_RE = re.compile(r"^\s*[-*]\s*(?P<ref>[A-Z][A-Z0-9]*-\d+)\s*[:.—–-]\s*(?P<title>.+?)\s*$")
_DEPENDENCY_RE = re.compile(
    r"(?P<task>[A-Z][A-Z0-9]*-\d+)\s+(?:после|зависит от|depends on|after)\s+(?P<blocker>[A-Z][A-Z0-9]*-\d+)",
    re.IGNORECASE,
)


@dataclass
class Section:
    title: str
    level: int
    body: str


@dataclass
class DecompositionTask:
    ref: str
    title: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ParsedSpec:
    title: str
    metadata: dict[str, str]
    sections: dict[str, Section]
    tasks: list[DecompositionTask] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _normalize(title: str) -> str:
    title = re.sub(r"^\d+(\.\d+)*\.?\s*", "", title.strip())
    title = re.sub(r"\s*\(.*?\)\s*$", "", title)
    return title.strip().lower().replace("ё", "е")


def parse_markdown(text: str) -> tuple[str, list[Section]]:
    doc_title = ""
    sections: list[Section] = []
    current: Section | None = None
    lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        heading = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            if current is not None:
                current.body = "\n".join(lines).strip()
                sections.append(current)
            level = len(heading.group(1))
            if level == 1 and not doc_title:
                doc_title = heading.group(2)
                current, lines = None, []
                continue
            current, lines = Section(title=heading.group(2), level=level, body=""), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        current.body = "\n".join(lines).strip()
        sections.append(current)
    return doc_title, sections


def _with_subsections(sections: list[Section]) -> dict[str, Section]:
    """Index level-2 sections by normalized title; their body includes nested subsections."""
    result: dict[str, Section] = {}
    for index, section in enumerate(sections):
        if section.level != 2:
            continue
        parts = [section.body]
        for nested in sections[index + 1:]:
            if nested.level <= 2:
                break
            parts.append(f"{'#' * nested.level} {nested.title}\n{nested.body}")
        result[_normalize(section.title)] = Section(section.title, 2, "\n".join(p for p in parts if p).strip())
    return result


def parse_metadata(body: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^\s*[-*]\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
        if match:
            metadata[match.group(1).strip().lower()] = match.group(2).strip()
    return metadata


def _has_content(body: str) -> bool:
    meaningful = [
        line for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("<!--")
    ]
    return bool(meaningful)


def _parse_common(text: str, required: list[str], optional_content: set[str]) -> ParsedSpec:
    title, raw_sections = parse_markdown(_COMMENT_RE.sub("", text))
    sections = _with_subsections(raw_sections)
    metadata_section = sections.get("метаданные")
    metadata = parse_metadata(metadata_section.body) if metadata_section else {}
    spec = ParsedSpec(title=title, metadata=metadata, sections=sections)
    if not title:
        spec.errors.append("Нет заголовка документа (# ...)")
    elif _PLACEHOLDER_RE.search(title):
        spec.errors.append("Заголовок содержит незаполненный шаблон")
    if metadata_section is None:
        spec.errors.append("Нет раздела «Метаданные»")
    for name in required:
        section = sections.get(_normalize(name))
        if section is None:
            spec.errors.append(f"Нет обязательного раздела «{name}»")
        elif name not in optional_content and not _has_content(section.body):
            spec.errors.append(f"Раздел «{name}» пуст")
        elif section and _PLACEHOLDER_RE.search(section.body):
            spec.errors.append(f"Раздел «{name}» содержит незаполненные плейсхолдеры или TODO")
    return spec


def _check_change_id(spec: ParsedSpec, prefix: str, expected: str | None) -> None:
    value = spec.metadata.get("change id", "")
    found = change_id_pattern(prefix).search(value)
    if not found:
        spec.errors.append("В метаданных не указан Change ID")
    elif expected and found.group(0) != expected:
        spec.errors.append(f"Change ID в документе ({found.group(0)}) не совпадает с ожидаемым ({expected})")


def validate_business_spec(text: str, *, prefix: str = "CHG", change_id: str | None = None) -> ParsedSpec:
    spec = _parse_common(text, BUSINESS_SECTIONS, BUSINESS_OPTIONAL_CONTENT)
    _check_change_id(spec, prefix, change_id)
    if not ISSUE_KEY_RE.search(spec.metadata.get("исходная jira", "")):
        spec.errors.append("В метаданных не указана исходная Jira (Business Idea)")
    if not spec.metadata.get("автор бизнес-требования"):
        spec.errors.append("В метаданных не указан автор бизнес-требования")
    scope = spec.sections.get(_normalize("Область изменения"))
    if scope:
        lowered = scope.body.lower()
        if "входит в scope" not in lowered.replace("не входит в scope", ""):
            spec.errors.append("В разделе «Область изменения» нет подраздела «Входит в scope»")
        if "не входит в scope" not in lowered:
            spec.errors.append("В разделе «Область изменения» нет подраздела «Не входит в scope»")
    criteria = spec.sections.get(_normalize("Критерии приёмки"))
    if criteria and _has_content(criteria.body) and not _GWT_RE.search(criteria.body):
        spec.errors.append("Критерии приёмки должны быть в формате Given / When / Then (Дано / Когда / Тогда)")
    return spec


def validate_functional_spec(text: str, *, prefix: str = "CHG", change_id: str | None = None,
                             business_commit: str | None = None) -> ParsedSpec:
    spec = _parse_common(text, FUNCTIONAL_SECTIONS, set())
    _check_change_id(spec, prefix, change_id)
    referenced = spec.metadata.get("business specification commit", "")
    if not re.fullmatch(r"[0-9a-f]{7,64}", referenced):
        spec.errors.append("В метаданных не указан commit согласованной бизнес-спецификации")
    elif business_commit and not (business_commit.startswith(referenced) or referenced.startswith(business_commit)):
        spec.errors.append(
            f"Спецификация ссылается на commit {referenced}, а согласована бизнес-спецификация {business_commit[:12]}"
        )
    if not spec.metadata.get("component"):
        spec.errors.append("В метаданных не указан Component")
    decomposition = spec.sections.get(_normalize("Декомпозиция"))
    if decomposition:
        spec.tasks = parse_decomposition(decomposition.body)
        if not spec.tasks:
            spec.errors.append("В разделе «Декомпозиция» нет задач вида «- DEV-1: Название»")
        refs = [task.ref for task in spec.tasks]
        if len(refs) != len(set(refs)):
            spec.errors.append("В декомпозиции повторяются идентификаторы задач")
        for task in spec.tasks:
            for blocker in task.depends_on:
                if blocker not in refs:
                    spec.errors.append(f"{task.ref} зависит от неизвестной задачи {blocker}")
    return spec


def parse_decomposition(body: str) -> list[DecompositionTask]:
    tasks: list[DecompositionTask] = []
    dependencies: list[tuple[str, str]] = []
    in_dependencies = False
    for line in body.splitlines():
        if re.match(r"^\s*[-*]\s*(Зависимости|Dependencies)", line, re.IGNORECASE):
            in_dependencies = True
            dependencies += [(m.group("task"), m.group("blocker")) for m in _DEPENDENCY_RE.finditer(line)]
            continue
        if in_dependencies and line.startswith((" ", "\t")):
            dependencies += [(m.group("task"), m.group("blocker")) for m in _DEPENDENCY_RE.finditer(line)]
            continue
        in_dependencies = False
        match = _TASK_RE.match(line)
        if match:
            tasks.append(DecompositionTask(ref=match.group("ref"), title=match.group("title")))
        else:
            dependencies += [(m.group("task"), m.group("blocker")) for m in _DEPENDENCY_RE.finditer(line)]
    by_ref = {task.ref: task for task in tasks}
    for task_ref, blocker in dependencies:
        if task_ref in by_ref and blocker not in by_ref[task_ref].depends_on:
            by_ref[task_ref].depends_on.append(blocker)
    return tasks


def validate(kind: ArtifactType, text: str, **kwargs) -> ParsedSpec:
    if kind is ArtifactType.BUSINESS_SPECIFICATION:
        kwargs.pop("business_commit", None)
        return validate_business_spec(text, **kwargs)
    if kind is ArtifactType.FUNCTIONAL_SPECIFICATION:
        return validate_functional_spec(text, **kwargs)
    raise ValueError(f"no document validation for {kind}")
