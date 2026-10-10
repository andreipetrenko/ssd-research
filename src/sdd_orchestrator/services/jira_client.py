from urllib.parse import quote

from .http import RestClient

API = "/rest/api/2"


class JiraClient(RestClient):
    """Jira Data Center / Server REST v2 client (routes from section 4.2)."""

    system = "jira"

    def get_issue(self, key: str, fields: str | None = None) -> dict | None:
        params = {"fields": fields} if fields else None
        return self.request("GET", f"{API}/issue/{quote(key)}", params=params, allow_404=True)

    def create_issue(self, fields: dict) -> str:
        return self.request("POST", f"{API}/issue", json={"fields": fields})["key"]

    def update_issue(self, key: str, *, fields: dict | None = None, update: dict | None = None) -> None:
        body: dict = {}
        if fields:
            body["fields"] = fields
        if update:
            body["update"] = update
        self.request("PUT", f"{API}/issue/{quote(key)}", json=body)

    def search(self, jql: str, fields: str = "summary,labels,status", max_results: int = 50) -> list[dict]:
        data = self.request(
            "GET", f"{API}/search", params={"jql": jql, "fields": fields, "maxResults": max_results}
        )
        return data.get("issues", [])

    def create_link(self, link_type: str, *, inward: str, outward: str) -> None:
        self.request(
            "POST",
            f"{API}/issueLink",
            json={"type": {"name": link_type}, "inwardIssue": {"key": inward}, "outwardIssue": {"key": outward}},
        )

    def add_comment(self, key: str, body: str) -> None:
        self.request("POST", f"{API}/issue/{quote(key)}/comment", json={"body": body})

    def add_remote_link(self, key: str, *, url: str, title: str, global_id: str | None = None) -> None:
        body: dict = {"object": {"url": url, "title": title}}
        if global_id:
            # Jira updates the existing remote link with the same globalId instead of adding a new one.
            body["globalId"] = global_id
        self.request("POST", f"{API}/issue/{quote(key)}/remotelink", json=body)

    def get_transitions(self, key: str) -> list[dict]:
        return self.request("GET", f"{API}/issue/{quote(key)}/transitions").get("transitions", [])

    def transition(self, key: str, transition_id: str) -> None:
        self.request("POST", f"{API}/issue/{quote(key)}/transitions", json={"transition": {"id": transition_id}})

    def get_issue_types(self, project_key: str) -> list[dict]:
        data = self.request("GET", f"{API}/issue/createmeta/{quote(project_key)}/issuetypes")
        return data.get("values", [])

    def browse_url(self, browse_base: str, key: str) -> str:
        return f"{browse_base.rstrip('/')}/browse/{quote(key)}"


def linked_keys(issue: dict) -> set[str]:
    keys: set[str] = set()
    for link in issue.get("fields", {}).get("issuelinks", []) or []:
        for side in ("inwardIssue", "outwardIssue"):
            if side in link:
                keys.add(link[side]["key"])
    return keys
