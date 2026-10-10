from urllib.parse import quote

from .http import RestClient

API = "/rest/api/latest"


class BitbucketClient(RestClient):
    """Bitbucket Data Center / Server REST client (routes from section 4.1)."""

    system = "bitbucket"

    def _repo(self, project_key: str, repository_slug: str) -> str:
        return f"{API}/projects/{quote(project_key)}/repos/{quote(repository_slug)}"

    def get_pull_request(self, project_key: str, repository_slug: str, pull_request_id: int) -> dict:
        return self.request("GET", f"{self._repo(project_key, repository_slug)}/pull-requests/{pull_request_id}")

    def list_pull_requests(self, project_key: str, repository_slug: str, *, state: str = "OPEN",
                           at: str | None = None, direction: str = "OUTGOING") -> list[dict]:
        params: dict = {"state": state, "limit": 100}
        if at:
            params["at"] = at
            params["direction"] = direction
        data = self.request("GET", f"{self._repo(project_key, repository_slug)}/pull-requests", params=params)
        return data.get("values", [])

    def create_pull_request(self, project_key: str, repository_slug: str, *, title: str, description: str,
                            from_branch: str, to_branch: str, reviewers: list[str] | None = None) -> dict:
        repo = {"slug": repository_slug, "project": {"key": project_key}}
        body = {
            "title": title,
            "description": description,
            "fromRef": {"id": _ref(from_branch), "repository": repo},
            "toRef": {"id": _ref(to_branch), "repository": repo},
            "reviewers": [{"user": {"name": name}} for name in reviewers or []],
        }
        return self.request("POST", f"{self._repo(project_key, repository_slug)}/pull-requests", json=body)

    def add_pull_request_comment(self, project_key: str, repository_slug: str, pull_request_id: int,
                                 text: str) -> dict:
        return self.request(
            "POST",
            f"{self._repo(project_key, repository_slug)}/pull-requests/{pull_request_id}/comments",
            json={"text": text},
        )

    def get_file(self, project_key: str, repository_slug: str, path: str, at: str) -> str | None:
        return self.request(
            "GET",
            f"{self._repo(project_key, repository_slug)}/raw/{quote(path)}",
            params={"at": at},
            expect_json=False,
            allow_404=True,
        )

    def get_build_statuses(self, commit: str) -> list[dict]:
        data = self.request("GET", f"/rest/build-status/1.0/commits/{quote(commit)}")
        return data.get("values", [])

    def list_webhooks(self, project_key: str) -> list[dict]:
        return self.request("GET", f"{API}/projects/{quote(project_key)}/webhooks").get("values", [])

    def create_webhook(self, project_key: str, *, name: str, url: str, events: list[str],
                       secret: str = "") -> dict:
        body: dict = {"name": name, "url": url, "events": events, "active": True}
        if secret:
            body["configuration"] = {"secret": secret}
        return self.request("POST", f"{API}/projects/{quote(project_key)}/webhooks", json=body)

    def browse_url(self, project_key: str, repository_slug: str, path: str, at: str) -> str:
        return (f"{self.base_url}/projects/{quote(project_key)}/repos/{quote(repository_slug)}"
                f"/browse/{quote(path)}?at={quote(at)}")


def _ref(branch: str) -> str:
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


def pull_request_url(pr: dict) -> str | None:
    links = pr.get("links", {}).get("self", [])
    return links[0].get("href") if links else None


def merge_commit_of(pr: dict) -> str | None:
    commit = pr.get("properties", {}).get("mergeCommit") or {}
    return commit.get("id")


def approvals_of(pr: dict) -> list[str]:
    return [
        reviewer.get("user", {}).get("name", "?")
        for reviewer in pr.get("reviewers", [])
        if reviewer.get("approved") or reviewer.get("status") == "APPROVED"
    ]
