import httpx


class IntegrationError(Exception):
    """Failure talking to Jira or Bitbucket; the event is retried by the worker."""

    def __init__(self, system: str, message: str, status_code: int | None = None):
        super().__init__(f"{system}: {message}")
        self.system = system
        self.status_code = status_code


def build_auth_headers(token: str, username: str, password: str) -> tuple[dict[str, str], httpx.Auth | None]:
    if token:
        return {"Authorization": f"Bearer {token}"}, None
    if username:
        return {}, httpx.BasicAuth(username, password)
    return {}, None


class RestClient:
    system = "rest"

    def __init__(self, base_url: str, http: httpx.Client | None = None, *, token: str = "",
                 username: str = "", password: str = "", timeout: float = 15.0):
        headers, auth = build_auth_headers(token, username, password)
        headers["Accept"] = "application/json"
        # Atlassian DC rejects state-changing requests without this header when XSRF checks are on.
        headers["X-Atlassian-Token"] = "no-check"
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = headers
        self._auth = auth

    def request(self, method: str, path: str, *, params: dict | None = None, json: object = None,
                expect_json: bool = True, allow_404: bool = False):
        url = f"{self.base_url}{path}"
        try:
            kwargs = {"params": params, "json": json, "headers": self._headers}
            if self._auth is not None:
                kwargs["auth"] = self._auth
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise IntegrationError(self.system, f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            body = response.text[:500]
            raise IntegrationError(
                self.system, f"{method} {path} → HTTP {response.status_code}: {body}", response.status_code
            )
        if not expect_json:
            return response.text
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()
