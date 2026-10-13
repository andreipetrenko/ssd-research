"""End-to-end demo against `uv run sdd up` (дашборд :48080, моки :48090).

    uv run python scripts/demo.py               # whole flow: idea → specs → Jira → code PRs
    uv run python scripts/demo.py --fault       # plus a Jira crash after issue creation (Demo 5)
    uv run python scripts/demo.py --tasks 1     # implement one task, leave the rest for the mock UI
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "credit-holidays"


class Demo:
    def __init__(self, api: str, mock: str):
        self.api = httpx.Client(base_url=api, timeout=30)
        self.mock = httpx.Client(base_url=mock, timeout=30)
        self.mock_url = mock

    def step(self, title: str) -> None:
        print(f"\n\033[1m▸ {title}\033[0m")

    def info(self, text: str) -> None:
        print(f"  {text}")

    def wait(self, what: str, check, timeout: float = 60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.5)
        raise SystemExit(f"  не дождались: {what}")

    def change(self, change_id: str) -> dict:
        return self.api.get(f"/api/changes/{change_id}").json()

    def push(self, project: str, repo: str, branch: str, files: dict[str, str]) -> str:
        response = self.mock.post("/mock/bitbucket/push", json={
            "project": project, "repo": repo, "branch": branch, "files": files, "message": f"Update {branch}"})
        response.raise_for_status()
        return response.json()["commit"]

    def open_pr(self, project: str, repo: str, branch: str, title: str, description: str) -> dict:
        response = self.mock.post(f"/rest/api/latest/projects/{project}/repos/{repo}/pull-requests", json={
            "title": title, "description": description, "reviewers": [{"user": {"name": "reviewer"}}],
            "fromRef": {"id": f"refs/heads/{branch}"}, "toRef": {"id": "refs/heads/main"}})
        if response.status_code == 409:
            existing = self.mock.get(f"/rest/api/latest/projects/{project}/repos/{repo}/pull-requests",
                                     params={"at": f"refs/heads/{branch}"}).json()["values"]
            return existing[0]
        response.raise_for_status()
        return response.json()

    def pr(self, project: str, repo: str, pr: dict, action: str, **body) -> dict:
        response = self.mock.post(f"/mock/bitbucket/{project}/{repo}/pull-requests/{pr['id']}/{action}", json=body)
        response.raise_for_status()
        return response.json()


def spec(name: str, **values: str) -> str:
    text = (EXAMPLES / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:48080")
    parser.add_argument("--mock", default="http://127.0.0.1:48090")
    parser.add_argument("--idea", help="ключ Business Idea; по умолчанию создаётся новая")
    parser.add_argument("--fault", action="store_true", help="сбой Jira после создания задачи")
    parser.add_argument("--tasks", type=int, default=None, help="сколько задач разработки довести до merge")
    args = parser.parse_args()
    demo = Demo(args.api, args.mock)

    demo.step("Проверка сервисов")
    try:
        health = demo.api.get("/health").json()
        demo.mock.get("/mock/state").raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  сервисы недоступны: {exc}. Запустите `uv run sdd mocks` и `uv run sdd serve`.")
        return 1
    demo.info(f"Integration Service: {health['status']}, PostgreSQL: {health['database']}")

    demo.step("Демо 1. Бизнес-идея")
    idea = args.idea
    if not idea:
        created = demo.mock.post("/ui/ideas", data={
            "summary": "Онлайн-заявка на кредитные каникулы",
            "description": "Клиенты ждут оператора 14 минут, чтобы оформить кредитные каникулы. "
                           "Нужна заявка в мобильном банке.",
        }, follow_redirects=False)
        idea = created.headers["location"].split("#issue-")[1]
        demo.info(f"В Jira создана Business Idea {idea}")
    response = demo.api.post("/api/changes", json={"source_issue": idea})
    if response.status_code >= 400:
        print(f"  {response.json()}")
        return 1
    change = response.json()
    change_id = change["change_id"]
    demo.info(f"Integration Service выдал Change ID {change_id}; ветка {change['business_branch']}")

    business_path = change["business_spec_path"]
    commit = demo.push("SPEC", "business-specs", change["business_branch"],
                       {business_path: spec("business-spec.md", CHANGE_ID=change_id, IDEA_KEY=idea)})
    demo.info(f"Бизнес-агент подготовил {business_path} (commit {commit[:10]})")

    demo.step("Демо 2. Бизнес-согласование")
    business_pr = demo.open_pr("SPEC", "business-specs", change["business_branch"],
                               f"[{change_id}] Бизнес-спецификация: онлайн-заявка на кредитные каникулы",
                               f"Change ID: {change_id}\nStage: business_specification\nJira: {idea}")
    demo.info(f"Открыт PR SPEC/business-specs#{business_pr['id']}")
    demo.wait("регистрация PR", lambda: demo.change(change_id)["artifacts"])
    if args.fault:
        demo.mock.post("/mock/jira/faults", json={"mode": "fail_after_create", "count": 1})
        demo.info("Включён сбой: Jira создаст задачу, но ответ до сервиса не дойдёт")
    demo.pr("SPEC", "business-specs", business_pr, "approve", user="reviewer")
    demo.pr("SPEC", "business-specs", business_pr, "merge")
    demo.info("Ревьюер одобрил PR, выполнен merge")
    if args.fault:
        demo.info("Ждём обработчик повторов: он найдёт уже созданную задачу по метке и не создаст дубликат…")
    analysis = demo.wait("задача системного анализа", lambda: [
        i for i in demo.change(change_id)["issues"] if i["stage"] == "system_analysis"], timeout=120)[0]
    demo.info(f"Автоматически создана задача системного анализа {analysis['issue_key']}: {analysis['summary']}")

    demo.step("Демо 5. Повторная доставка webhook")
    demo.pr("SPEC", "business-specs", business_pr, "redeliver")
    time.sleep(1.5)
    anl = [i for i in demo.mock.get("/mock/state").json()["issues"].values()
           if i["fields"]["project"]["key"] == "ANL" and change_id in i["fields"].get("description", "")]
    demo.info(f"Webhook pr:merged отправлен повторно; задач системного анализа в Jira: {len(anl)}")

    demo.step("Демо 3. Системный анализ")
    trace = demo.change(change_id)
    business = next(a for a in trace["artifacts"] if a["artifact_type"] == "business_specification")
    functional_path = trace["functional_spec_path"]
    demo.push("SPEC", "business-specs", trace["system_branch"], {functional_path: spec(
        "functional-spec.md", CHANGE_ID=change_id, BUSINESS_COMMIT=business["merge_commit_sha"],
        ANALYSIS_KEY=analysis["issue_key"])})
    system_pr = demo.open_pr("SPEC", "business-specs", trace["system_branch"],
                             f"[{change_id}] Функциональная спецификация: сервис заявок на каникулы",
                             f"Change ID: {change_id}\nStage: functional_specification\nJira: {analysis['issue_key']}")
    demo.info(f"Открыт PR SPEC/business-specs#{system_pr['id']} "
              f"(ссылается на commit {business['merge_commit_sha'][:10]})")
    demo.wait("регистрация PR", lambda: len(demo.change(change_id)["artifacts"]) >= 2)
    demo.pr("SPEC", "business-specs", system_pr, "approve", user="architect")
    demo.pr("SPEC", "business-specs", system_pr, "merge")
    dev = demo.wait("задачи разработки", lambda: [
        i for i in demo.change(change_id)["issues"] if i["stage"] == "development"])
    for issue in dev:
        demo.info(f"Создана задача разработки {issue['issue_key']} ← {issue['spec_ref']}: {issue['summary']}")

    demo.step("Демо 4. AI-разработка")
    count = len(dev) if args.tasks is None else min(args.tasks, len(dev))
    for issue in dev[:count]:
        key = issue["issue_key"]
        branch = f"feature/{change_id}-{key}-implementation"
        slug = issue["spec_ref"].lower()
        demo.push("APP", "application-repo", branch, {
            f"src/holidays/{slug}.py": f'"""{issue["summary"]}"""\n',
            f"tests/test_{slug.replace('-', '_')}.py": "def test_acceptance():\n    assert True\n",
        })
        code_pr = demo.open_pr("APP", "application-repo", branch, f"[{change_id}] {key}: реализация",
                               f"Change ID: {change_id}\nStage: implementation\nJira: {key}")
        demo.wait("связь PR с задачей", lambda key=key: any(
            a["artifact_type"] == "implementation" and a["jira_key"] == key
            for a in demo.change(change_id)["artifacts"]))
        demo.pr("APP", "application-repo", code_pr, "build", state="SUCCESSFUL")
        demo.pr("APP", "application-repo", code_pr, "approve", user="tech-lead")
        demo.pr("APP", "application-repo", code_pr, "merge")
        demo.wait(f"закрытие {key}", lambda key=key: any(
            i["issue_key"] == key and i["state"] == "done" for i in demo.change(change_id)["issues"]))
        demo.info(f"Агент подготовил код и тесты → PR APP/application-repo#{code_pr['id']} "
                  f"→ CI → review → merge → {key} Done")

    final = demo.change(change_id)
    demo.step("Итог")
    demo.info(f"{change_id}: этап «{final['stage_title']}», статус {final['status']}")
    metrics = demo.api.get("/api/metrics").json()
    demo.info(f"Webhook получено: {metrics['deliveries']}, предотвращено дубликатов: {metrics['duplicates_prevented']}")
    demo.info(f"Дашборд: {args.api}/   Моки Bitbucket/Jira: {args.mock}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
