import hashlib
from dataclasses import dataclass

from ..repositories.changes import ChangeRepository
from .jira_client import JiraClient, linked_keys


def operation_label(operation_key: str) -> str:
    """Jira label that marks the issue created by an operation; used to reconcile after crashes."""
    return "sdd-op-" + hashlib.sha1(operation_key.encode("utf-8")).hexdigest()[:16]


@dataclass
class EnsuredIssue:
    key: str
    outcome: str  # "known" (already in DB), "reconciled" (found in Jira), "created"


class IdempotencyService:
    """Creates Jira issues exactly once per operation key (section 5.2)."""

    def __init__(self, jira: JiraClient, changes: ChangeRepository):
        self.jira = jira
        self.changes = changes

    def ensure_issue(self, *, operation_key: str, change_id: str, stage: str, project: str, issue_type: str,
                     summary: str, description: str, parent_issue: str | None = None,
                     spec_ref: str | None = None, extra_fields: dict | None = None) -> EnsuredIssue:
        known = self.changes.issue_by_operation(operation_key)
        if known:
            return EnsuredIssue(known["issue_key"], "known")

        label = operation_label(operation_key)
        found = self.jira.search(f'labels = "{label}" ORDER BY created ASC', fields="summary,labels", max_results=5)
        if found:
            key, outcome = found[0]["key"], "reconciled"
        else:
            fields = {
                "project": {"key": project},
                "issuetype": {"name": issue_type},
                "summary": summary[:250],
                "description": f"{description}\n\nSDD operation: {operation_key}",
                "labels": ["sdd", change_id, label],
                **(extra_fields or {}),
            }
            key, outcome = self.jira.create_issue(fields), "created"

        self.changes.save_issue(
            change_id=change_id, issue_key=key, issue_type=issue_type, parent_issue=parent_issue, stage=stage,
            operation_key=operation_key, spec_ref=spec_ref, summary=summary,
        )
        return EnsuredIssue(key, outcome)

    def ensure_link(self, link_type: str, *, inward: str, outward: str) -> str:
        issue = self.jira.get_issue(inward, fields="issuelinks")
        if issue is None:
            return "missing"
        if outward in linked_keys(issue):
            return "exists"
        self.jira.create_link(link_type, inward=inward, outward=outward)
        return "created"
