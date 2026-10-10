import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Stage(StrEnum):
    IDEA = "idea"
    BUSINESS_SPECIFICATION = "business_specification"
    SYSTEM_ANALYSIS = "system_analysis"
    FUNCTIONAL_SPECIFICATION = "functional_specification"
    DEVELOPMENT = "development"
    DONE = "done"


class ArtifactType(StrEnum):
    BUSINESS_SPECIFICATION = "business_specification"
    FUNCTIONAL_SPECIFICATION = "functional_specification"
    IMPLEMENTATION = "implementation"


class EventType(StrEnum):
    PR_OPENED = "pull_request_opened"
    PR_UPDATED = "pull_request_updated"
    PR_MERGED = "pull_request_merged"
    PR_DECLINED = "pull_request_declined"
    PR_NEEDS_WORK = "pull_request_needs_work"
    PR_APPROVED = "pull_request_approved"


BITBUCKET_EVENT_MAP: dict[str, EventType] = {
    "pr:opened": EventType.PR_OPENED,
    "pr:from_ref_updated": EventType.PR_UPDATED,
    "pr:modified": EventType.PR_UPDATED,
    "pr:merged": EventType.PR_MERGED,
    "pr:declined": EventType.PR_DECLINED,
    "pr:deleted": EventType.PR_DECLINED,
    "pr:reviewer:needs_work": EventType.PR_NEEDS_WORK,
    "pr:reviewer:approved": EventType.PR_APPROVED,
}

STAGE_TITLES = {
    Stage.IDEA: "Идея",
    Stage.BUSINESS_SPECIFICATION: "Бизнес-спецификация",
    Stage.SYSTEM_ANALYSIS: "Системный анализ",
    Stage.FUNCTIONAL_SPECIFICATION: "Функциональная спецификация",
    Stage.DEVELOPMENT: "Разработка",
    Stage.DONE: "Готово",
}

ARTIFACT_TITLES = {
    ArtifactType.BUSINESS_SPECIFICATION: "Бизнес-спецификация",
    ArtifactType.FUNCTIONAL_SPECIFICATION: "Функциональная спецификация",
    ArtifactType.IMPLEMENTATION: "Код и тесты",
}

ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_BRANCH_SUFFIX = {
    "business-spec": ArtifactType.BUSINESS_SPECIFICATION,
    "system-spec": ArtifactType.FUNCTIONAL_SPECIFICATION,
}
_STAGE_ALIASES = {
    "business_specification": ArtifactType.BUSINESS_SPECIFICATION,
    "business-spec": ArtifactType.BUSINESS_SPECIFICATION,
    "functional_specification": ArtifactType.FUNCTIONAL_SPECIFICATION,
    "system-spec": ArtifactType.FUNCTIONAL_SPECIFICATION,
    "system_analysis": ArtifactType.FUNCTIONAL_SPECIFICATION,
    "implementation": ArtifactType.IMPLEMENTATION,
}


def change_id_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(prefix)}-\d+\b")


def branch_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?:refs/heads/)?feature/(?P<change>{re.escape(prefix)}-\d+)-"
        rf"(?:(?P<spec>business-spec|system-spec)|(?P<issue>[A-Z][A-Z0-9]+-\d+)-implementation)"
        rf"(?:[-/].*)?$"
    )


def branch_name(change_id: str, artifact_type: ArtifactType, issue_key: str | None = None) -> str:
    if artifact_type is ArtifactType.BUSINESS_SPECIFICATION:
        return f"feature/{change_id}-business-spec"
    if artifact_type is ArtifactType.FUNCTIONAL_SPECIFICATION:
        return f"feature/{change_id}-system-spec"
    if not issue_key:
        raise ValueError("implementation branch requires a Jira issue key")
    return f"feature/{change_id}-{issue_key}-implementation"


@dataclass
class PrMetadata:
    change_id: str | None
    artifact_type: ArtifactType | None
    jira_key: str | None
    errors: list[str] = field(default_factory=list)


def _description_fields(description: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in description.splitlines():
        match = re.match(r"^\s*[-*]?\s*([A-Za-zА-Яа-яЁё ]+?)\s*:\s*(.+?)\s*$", line)
        if match:
            values[match.group(1).strip().lower()] = match.group(2).strip()
    return values


def extract_pr_metadata(prefix: str, title: str, description: str, source_branch: str) -> PrMetadata:
    """Read Change ID, stage and Jira key from branch name and PR description."""
    errors: list[str] = []
    fields = _description_fields(description or "")
    branch_match = branch_pattern(prefix).match(source_branch or "")
    id_re = change_id_pattern(prefix)

    branch_change = branch_match.group("change") if branch_match else None
    described_change = None
    if "change id" in fields:
        found = id_re.search(fields["change id"])
        described_change = found.group(0) if found else None
    title_change = id_re.search(title or "")
    change_id = branch_change or described_change or (title_change.group(0) if title_change else None)
    if branch_change and described_change and branch_change != described_change:
        errors.append(
            f"Change ID в ветке ({branch_change}) не совпадает с описанием PR ({described_change})"
        )
    if not change_id:
        errors.append("Не найден Change ID: ни в имени ветки, ни в описании PR")

    artifact_type: ArtifactType | None = None
    if branch_match:
        if branch_match.group("spec"):
            artifact_type = _BRANCH_SUFFIX[branch_match.group("spec")]
        else:
            artifact_type = ArtifactType.IMPLEMENTATION
    described_stage = _STAGE_ALIASES.get(fields.get("stage", "").strip().lower())
    if artifact_type and described_stage and artifact_type is not described_stage:
        errors.append(
            f"Этап в ветке ({artifact_type.value}) не совпадает с описанием PR ({described_stage.value})"
        )
    artifact_type = artifact_type or described_stage
    if not artifact_type:
        errors.append("Не определён тип этапа: используйте ветку feature/<CHG>-business-spec|system-spec|"
                      "<JIRA>-implementation или строку 'Stage:' в описании")

    jira_key = branch_match.group("issue") if branch_match and branch_match.group("issue") else None
    if not jira_key and "jira" in fields:
        found = ISSUE_KEY_RE.search(fields["jira"])
        jira_key = found.group(0) if found else None
    if artifact_type is ArtifactType.IMPLEMENTATION and not jira_key:
        errors.append("PR с кодом должен ссылаться на Jira-задачу разработки")

    return PrMetadata(change_id=change_id, artifact_type=artifact_type, jira_key=jira_key, errors=errors)


@dataclass
class NormalizedEvent:
    """Internal event contract (section 4.4); not a copy of the native Bitbucket payload."""

    event_id: str
    event_type: EventType
    project_key: str
    repository_slug: str
    pull_request_id: int
    source_branch: str
    target_branch: str
    title: str = ""
    description: str = ""
    pull_request_url: str | None = None
    latest_commit: str | None = None
    merge_commit: str | None = None
    change_id: str | None = None
    stage: str | None = None
    jira_key: str | None = None
    occurred_at: str | None = None
    metadata_errors: list[str] = field(default_factory=list)

    @property
    def repository(self) -> str:
        return f"{self.project_key}/{self.repository_slug}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["event_type"] = self.event_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "NormalizedEvent":
        values = dict(data)
        values["event_type"] = EventType(values["event_type"])
        return cls(**values)
