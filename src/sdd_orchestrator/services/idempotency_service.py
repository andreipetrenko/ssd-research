import hashlib
from dataclasses import dataclass

from ..repositories.changes import ChangeRepository
from .http import IntegrationError
from .jira_client import JiraClient, linked_keys


def operation_token(operation_key: str) -> str:
    """Stable token written into the Jira description so we can find the issue after a crash.

    Does not use the Labels field: many company screens do not include it.
    """
    return "sddop-" + hashlib.sha1(operation_key.encode("utf-8")).hexdigest()[:16]


def operation_marker(operation_key: str) -> str:
    return f"SDD-OP:{operation_token(operation_key)}"


@dataclass
class EnsuredIssue:
    key: str
    outcome: str  # "known" (already in DB), "reconciled" (found in Jira), "created"


class IdempotencyService:
    """Creates Jira issues exactly once per operation key (section 5.2)."""

    def __init__(self, jira: JiraClient, changes: ChangeRepository):
        self.jira = jira
        self.changes = changes

    def find_existing(self, operation_key: str) -> dict | None:
        token = operation_token(operation_key)
        marker = operation_marker(operation_key)
        for jql in (
            f'description ~ "{token}" ORDER BY created ASC',
            f'text ~ "{token}" ORDER BY created ASC',
            f'labels = "{token}" ORDER BY created ASC',
        ):
            try:
                found = self.jira.search(jql, fields="summary,description,labels", max_results=10)
            except IntegrationError as exc:
                # 4xx: this JQL flavour is unsupported (no Labels, no text search). 5xx: Jira is down.
                if exc.status_code is None or exc.status_code >= 500:
                    raise
                continue
            for issue in found:
                fields = issue.get("fields", {})
                blob = " ".join([
                    str(fields.get("description") or ""),
                    str(fields.get("summary") or ""),
                    " ".join(fields.get("labels") or []),
                ])
                if token in blob or marker in blob:
                    return issue
        return None

    def ensure_issue(self, *, operation_key: str, change_id: str, stage: str, project: str, issue_type: str,
                     summary: str, description: str, parent_issue: str | None = None,
                     spec_ref: str | None = None, extra_fields: dict | None = None) -> EnsuredIssue:
        known = self.changes.issue_by_operation(operation_key)
        if known:
            return EnsuredIssue(known["issue_key"], "known")

        found = self.find_existing(operation_key)
        if found:
            key, outcome = found["key"], "reconciled"
        else:
            fields = {
                "project": {"key": project},
                "issuetype": {"name": issue_type},
                "summary": summary[:250],
                "description": f"{description}\n\nSDD operation: {operation_key}\n{operation_marker(operation_key)}",
                **(extra_fields or {}),
            }
            key, outcome = self.jira.create_issue(fields), "created"

        self.changes.save_issue(
            change_id=change_id, issue_key=key, issue_type=issue_type, parent_issue=parent_issue, stage=stage,
            operation_key=operation_key, spec_ref=spec_ref, summary=summary,
        )
        return EnsuredIssue(key, outcome)

    _RELATE_ALIASES = ["Relates", "Relates to", "Related", "Связывает", "Связан"]
    _BLOCK_ALIASES = ["Blocks", "Block", "Блокирует"]

    def ensure_link(self, link_type: str, *, inward: str, outward: str) -> str:
        issue = self.jira.get_issue(inward, fields="issuelinks")
        if issue is None:
            return "missing"
        if outward in linked_keys(issue):
            return "exists"
        aliases = self._BLOCK_ALIASES if link_type.lower().startswith("block") or "блок" in link_type.lower() \
            else self._RELATE_ALIASES
        resolved = self.jira.resolve_link_type(link_type, aliases)
        self.jira.create_link(resolved, inward=inward, outward=outward)
        return "created"
