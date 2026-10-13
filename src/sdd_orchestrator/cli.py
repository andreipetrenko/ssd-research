"""`sdd` command line: run the service and support analysts/developers working with their IDE/CLI agents."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .domain import ArtifactType, branch_name
from .settings import PROJECT_ROOT, get_settings

STAGES = {
    "business": ArtifactType.BUSINESS_SPECIFICATION,
    "system": ArtifactType.FUNCTIONAL_SPECIFICATION,
    "implementation": ArtifactType.IMPLEMENTATION,
}
TEMPLATE_DIRS = [Path("openspec/schemas/sdd/templates"),
                 PROJECT_ROOT / "openspec-templates/business-specs/openspec/schemas/sdd/templates"]


def _fail(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _api(args) -> str:
    return (args.api or get_settings().public_url).rstrip("/")


def _git(repo_dir: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *argv], check=check, capture_output=True, text=True)


def _is_git_repo(repo_dir: Path) -> bool:
    return (repo_dir / ".git").exists()


def _checkout(repo_dir: Path, branch: str, base: str) -> str:
    if not _is_git_repo(repo_dir):
        return "не git-репозиторий, ветка не создана"
    exists = _git(repo_dir, "rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0
    if exists:
        _git(repo_dir, "checkout", branch)
        return f"переключено на существующую ветку {branch}"
    _git(repo_dir, "checkout", "-b", branch, base)
    return f"создана ветка {branch} от {base}"


def _template(repo_dir: Path, name: str) -> str:
    for directory in TEMPLATE_DIRS:
        path = directory if directory.is_absolute() else repo_dir / directory
        if (path / name).is_file():
            return (path / name).read_text(encoding="utf-8")
    raise FileNotFoundError(f"шаблон {name} не найден")


def _fill(template: str, values: dict[str, str]) -> str:
    for placeholder, value in values.items():
        if value:
            template = template.replace(placeholder, value)
    return template


def _write_new(path: Path, content: str, force: bool) -> str:
    if path.exists() and not force:
        return f"{path} уже существует — не перезаписан (используйте --force)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"создан {path}"


def _get_change(args, change_id: str) -> dict:
    response = httpx.get(f"{_api(args)}/api/changes/{change_id}", timeout=15)
    if response.status_code == 404:
        raise SystemExit(_fail(f"изменение {change_id} не найдено в Integration Service"))
    response.raise_for_status()
    return response.json()


# --- commands -------------------------------------------------------------------------------------


def cmd_serve(args) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run("sdd_orchestrator.app:create_app", factory=True, host=args.host or settings.host,
                port=args.port or settings.port, reload=args.reload, log_level=settings.log_level.lower())
    return 0


def cmd_mocks(args) -> int:
    import uvicorn

    uvicorn.run("sdd_mocks.app:create_mock_app", factory=True, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_migrate(_args) -> int:
    from .db import make_engine, run_migrations

    settings = get_settings()
    applied = run_migrations(make_engine(settings.database_url), settings.migrations_dir)
    print("применены миграции: " + ", ".join(applied) if applied else "схема актуальна")
    return 0


def cmd_worker(_args) -> int:
    from .container import build_services
    from .workers.retry_worker import RetryWorker

    settings = get_settings()
    services = build_services(settings)
    worker = RetryWorker(services.workflow, services.events, settings)
    print(f"retry worker: опрос каждые {settings.retry_poll_seconds} с; Ctrl+C для остановки")
    try:
        while True:
            for result in worker.run_once():
                print(json.dumps(result, ensure_ascii=False, default=str))
            time.sleep(settings.retry_poll_seconds)
    except KeyboardInterrupt:
        return 0


def cmd_validate(args) -> int:
    from .services import spec_parser

    text = Path(args.file).read_text(encoding="utf-8")
    kind = ArtifactType.BUSINESS_SPECIFICATION if args.type == "business" else ArtifactType.FUNCTIONAL_SPECIFICATION
    spec = spec_parser.validate(kind, text, prefix=get_settings().change_id_prefix, change_id=args.change,
                                business_commit=args.business_commit)
    if args.json:
        print(json.dumps({"valid": spec.valid, "errors": spec.errors,
                          "tasks": [t.__dict__ for t in spec.tasks]}, ensure_ascii=False, indent=2))
    elif spec.valid:
        print(f"OK: {args.file}")
        for task in spec.tasks:
            deps = f" (после {', '.join(task.depends_on)})" if task.depends_on else ""
            print(f"  {task.ref}: {task.title}{deps}")
    else:
        print(f"{args.file}: найдено замечаний {len(spec.errors)}")
        for error in spec.errors:
            print(f"  - {error}")
    return 0 if spec.valid else 1


def cmd_start(args) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    response = httpx.post(f"{_api(args)}/api/changes", json={"source_issue": args.idea}, timeout=30)
    if response.status_code >= 400:
        detail = response.json().get("detail", response.text)
        if isinstance(detail, dict):
            detail = detail.get("message", "") + "".join(f"\n  - {p}" for p in detail.get("problems", []))
        return _fail(f"Integration Service отказал: {detail}")
    change = response.json()
    change_id = change["change_id"]
    branch = branch_name(change_id, ArtifactType.BUSINESS_SPECIFICATION)
    print(f"Change ID: {change_id} ({'новое' if change['created'] else 'уже существовало'})")
    if not args.no_git:
        print(_checkout(repo_dir, branch, args.base))
    content = _fill(_template(repo_dir, "business-spec.md"), {
        "<Название изменения>": change.get("title") or "",
        "<Change ID>": change_id,
        "<Ключ Business Idea>": args.idea,
        "<ФИО автора>": args.author or "",
    })
    print(_write_new(repo_dir / change["business_spec_path"], content, args.force))
    print(f"Дальше: заполните документ с агентом (prompts/business-analyst.md), затем\n"
          f"  sdd validate {change['business_spec_path']} --type business --change {change_id}\n"
          f"  sdd open-pr --change {change_id} --stage business --jira {args.idea}")
    return 0


def cmd_start_system(args) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    change = _get_change(args, args.change)
    business = next((a for a in change["artifacts"] if a["artifact_type"] == "business_specification"
                     and a["pull_request_state"] == "MERGED"), None)
    if business is None:
        return _fail(f"бизнес-спецификация {args.change} ещё не согласована (нет merge)")
    analysis = next((i for i in change["issues"] if i["stage"] == "system_analysis"), None)
    branch = branch_name(args.change, ArtifactType.FUNCTIONAL_SPECIFICATION)
    if not args.no_git:
        print(_checkout(repo_dir, branch, args.base))
    content = _fill(_template(repo_dir, "functional-spec.md"), {
        "<Change ID>": args.change,
        "<commit бизнес-спецификации>": business["merge_commit_sha"],
        "<Ключ задачи системного анализа>": analysis["issue_key"] if analysis else "",
        "<Компонент>": args.component or "",
        "<Название компонента>": args.component or "",
    })
    print(_write_new(repo_dir / change["functional_spec_path"], content, args.force))
    print(f"Бизнес-спецификация: commit {business['merge_commit_sha']}")
    if analysis:
        print(f"Задача системного анализа: {analysis['issue_key']} {analysis['url']}")
    print(f"Дальше: sdd open-pr --change {args.change} --stage system")
    return 0


def cmd_start_dev(args) -> int:
    from .container import build_services

    repo_dir = Path(args.repo_dir).resolve()
    change = _get_change(args, args.change)
    issue = next((i for i in change["issues"] if i["issue_key"] == args.jira and i["stage"] == "development"), None)
    if issue is None:
        return _fail(f"{args.jira} не является задачей разработки изменения {args.change}")
    services = build_services(get_settings())
    details = services.jira.get_issue(args.jira, fields="summary,description") or {"fields": {}}
    functional = next((a for a in change["artifacts"] if a["artifact_type"] == "functional_specification"
                       and a["pull_request_state"] == "MERGED"), None)
    spec_url = ""
    if functional:
        project, slug = functional["repository"].split("/", 1)
        spec_url = services.bitbucket.browse_url(project, slug, change["functional_spec_path"],
                                                 functional["merge_commit_sha"])
    template_path = repo_dir / "prompts/developer.md"
    if not template_path.exists():
        template_path = PROJECT_ROOT / "openspec-templates/application-repo/prompts/developer.md"
    template = template_path.read_text(encoding="utf-8")
    block = template.split("```text", 1)[1].split("```", 1)[0].strip() if "```text" in template else template
    context = block.format(
        JIRA_KEY=args.jira, CHANGE_ID=args.change, JIRA_SUMMARY=details["fields"].get("summary", issue["summary"]),
        JIRA_DESCRIPTION=details["fields"].get("description", ""), SPEC_URL=spec_url or "-",
    )
    branch = branch_name(args.change, ArtifactType.IMPLEMENTATION, args.jira)
    if not args.no_git:
        print(_checkout(repo_dir, branch, args.base))
    print(_write_new(repo_dir / ".sdd/context.md", context + "\n", True))
    print(f"Передайте .sdd/context.md агенту. Затем: sdd open-pr --change {args.change} --stage implementation "
          f"--jira {args.jira} --project APP --repo application-repo")
    return 0


def cmd_open_pr(args) -> int:
    from .container import build_services

    repo_dir = Path(args.repo_dir).resolve()
    stage = STAGES[args.stage]
    if stage is ArtifactType.IMPLEMENTATION and not args.jira:
        return _fail("для PR с кодом нужен --jira <DEV-KEY>")
    branch = branch_name(args.change, stage, args.jira if stage is ArtifactType.IMPLEMENTATION else None)
    settings = get_settings()
    default_repo = settings.code_repository_list[0] if stage is ArtifactType.IMPLEMENTATION \
        else settings.spec_repository_list[0]
    project, slug = default_repo.split("/", 1)
    project, slug = args.project or project, args.repo or slug

    if stage is not ArtifactType.IMPLEMENTATION:
        from .services import spec_parser

        template = settings.business_spec_path if stage is ArtifactType.BUSINESS_SPECIFICATION \
            else settings.functional_spec_path
        doc = repo_dir / template.format(change_id=args.change)
        if doc.exists():
            spec = spec_parser.validate(stage, doc.read_text(encoding="utf-8"), prefix=settings.change_id_prefix,
                                        change_id=args.change)
            if spec.errors and not args.allow_invalid:
                print("Документ не прошёл проверку:", *[f"  - {e}" for e in spec.errors], sep="\n")
                return 1
            title = args.title or f"[{args.change}] {spec.title}"
        else:
            title = args.title or f"[{args.change}] {stage.value}"
    else:
        title = args.title or f"[{args.change}] {args.jira}: реализация"

    if not args.no_push:
        if not _is_git_repo(repo_dir):
            return _fail(f"{repo_dir} не git-репозиторий; используйте --no-push, если ветка уже в Bitbucket")
        _git(repo_dir, "push", "-u", args.remote, branch)
        print(f"ветка {branch} отправлена в {args.remote}")

    bitbucket = build_services(settings).bitbucket
    existing = bitbucket.list_pull_requests(project, slug, state="OPEN", at=f"refs/heads/{branch}")
    if existing:
        pr = existing[0]
        print(f"PR уже открыт: #{pr['id']} {pr['links']['self'][0]['href']}")
        return 0
    lines = [f"Change ID: {args.change}", f"Stage: {stage.value}"]
    if args.jira:
        lines.append(f"Jira: {args.jira}")
    if args.description:
        lines += ["", args.description]
    pr = bitbucket.create_pull_request(project, slug, title=title, description="\n".join(lines),
                                       from_branch=branch, to_branch=args.base, reviewers=args.reviewer)
    print(f"создан PR #{pr['id']}: {pr['links']['self'][0]['href']}")
    return 0


def cmd_init(args) -> int:
    from .settings import Settings, get_settings
    from .setup import collect_init_values, print_report, register_configured_webhooks, run_doctor, write_env

    values = collect_init_values(args)
    path = Path(args.env_file)
    write_env(path, values)
    print(f"Записал настройки в {path}")
    get_settings.cache_clear()
    settings = Settings(_env_file=path)
    if args.register_webhooks:
        try:
            for line in register_configured_webhooks(settings):
                print(line)
        except Exception as exc:  # noqa: BLE001
            print(f"Webhook пока не создался: {exc}")
            print("Потом: uv run sdd register-webhook --all")
    report = run_doctor(settings=settings)
    print_report(report)
    return 0 if report.ok else 1


def cmd_doctor(_args) -> int:
    from .setup import print_report, run_doctor

    report = run_doctor()
    print_report(report)
    return 0 if report.ok else 1


def cmd_check(_args) -> int:
    return cmd_doctor(_args)


def cmd_register_webhook(args) -> int:
    from .container import build_services
    from .setup import register_configured_webhooks, split_repo

    settings = get_settings()
    url = settings.public_url.rstrip("/") + "/webhooks/bitbucket"
    if args.all or (not args.project and not args.repo):
        for line in register_configured_webhooks(settings):
            print(line)
        return 0
    bitbucket = build_services(settings).bitbucket
    if args.repo:
        project, slug = split_repo(args.repo)
        result = bitbucket.ensure_repo_webhook(project, slug, url=url, secret=settings.bitbucket_webhook_secret)
        print(f"{project}/{slug}: webhook {'создан' if result.get('created') else 'уже был'}")
        return 0
    hook = bitbucket.create_webhook(
        args.project, name="SDD Orchestrator", url=url, events=bitbucket.WEBHOOK_EVENTS,
        secret=settings.bitbucket_webhook_secret,
    )
    print(f"webhook #{hook.get('id')} создан для проекта {args.project}")
    return 0


def _wait_http(url: str, timeout: float = 8) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=0.4)
            return True
        except httpx.HTTPError:
            time.sleep(0.15)
    return False


def _start_mocks(host: str, port: int) -> subprocess.Popen[Any] | None:
    url = f"http://{host}:{port}/"
    if _wait_http(url, timeout=0.6):
        print(f"Моки уже запущены: {url}")
        return None
    proc = subprocess.Popen(
        [sys.executable, "-m", "sdd_orchestrator.cli", "mocks", "--host", host, "--port", str(port)]
    )
    if _wait_http(url):
        print(f"Моки Bitbucket/Jira: {url}")
        return proc
    proc.terminate()
    raise SystemExit("не удалось запустить моки на порту " + str(port))


def cmd_up(args) -> int:
    from .setup import ensure_database, uses_local_mocks

    if args.docker:
        compose = PROJECT_ROOT / "docker-compose.yml"
        if not compose.exists():
            return _fail("нет docker-compose.yml")
        cmd = ["docker", "compose", "-f", str(compose), "--profile", "app", "up", "-d"]
        if args.build:
            cmd.append("--build")
        print(" ".join(cmd))
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            return result.returncode
        print("Сервис: http://127.0.0.1:48080/")
        return 0

    settings = get_settings()
    print(ensure_database(settings.database_url))
    mock_proc = None
    if not args.no_mocks and uses_local_mocks(settings):
        mock_proc = _start_mocks(args.mock_host, args.mock_port)
    print(f"Дашборд: {settings.public_url}")
    print("Остановка: Ctrl+C\n")
    try:
        return cmd_serve(args)
    finally:
        if mock_proc and mock_proc.poll() is None:
            mock_proc.terminate()
            try:
                mock_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_proc.kill()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdd", description="SDD MVP: Integration Service и инструменты аналитика")
    parser.add_argument("--api", help="URL Integration Service (по умолчанию SDD_PUBLIC_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="запустить Integration Service")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mocks", help="запустить моки Bitbucket DC и Jira DC")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=48090)
    p.set_defaults(func=cmd_mocks)

    sub.add_parser("migrate", help="применить миграции PostgreSQL").set_defaults(func=cmd_migrate)
    sub.add_parser("worker", help="отдельный процесс обработчика повторов").set_defaults(func=cmd_worker)
    sub.add_parser("doctor", help="проверить, что Jira, Bitbucket, база и webhook готовы").set_defaults(func=cmd_doctor)
    sub.add_parser("check", help="то же, что doctor").set_defaults(func=cmd_check)

    p = sub.add_parser("init", help="задать настройки и записать .env")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--register-webhooks", action="store_true")
    p.add_argument("--public-url", default="")
    p.add_argument("--database-url", default="")
    p.add_argument("--jira-url", default="")
    p.add_argument("--jira-token", default="")
    p.add_argument("--bitbucket-url", default="")
    p.add_argument("--bitbucket-token", default="")
    p.add_argument("--spec-repo", default="")
    p.add_argument("--code-repo", default="")
    p.add_argument("--target-branch", default="master")
    p.add_argument("--idea-type", default="")
    p.add_argument("--analysis-project", default="")
    p.add_argument("--analysis-type", default="Task")
    p.add_argument("--dev-project", default="")
    p.add_argument("--dev-type", default="Task")
    p.add_argument("--done-transition", default="Done")
    p.add_argument("--webhook-secret", default="")
    p.add_argument("--no-ci", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("register-webhook", help="повесить webhook на репозиторий (или на все из .env)")
    p.add_argument("--repo", help="ПРОЕКТ/репозиторий, например SPEC/business-specs")
    p.add_argument("--project", help="весь проект Bitbucket — только если так решили специально")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_register_webhook)

    p = sub.add_parser("up", help="одной командой: база, моки Jira/Bitbucket и сервис")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--no-mocks", action="store_true",
                   help="не поднимать моки (когда .env указывает на живые Jira/Bitbucket)")
    p.add_argument("--mock-host", default="127.0.0.1")
    p.add_argument("--mock-port", type=int, default=48090)
    p.add_argument("--docker", action="store_true", help="поднять всё через docker compose --profile app")
    p.add_argument("--build", action="store_true")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("validate", help="проверить спецификацию по шаблону")
    p.add_argument("file")
    p.add_argument("--type", choices=["business", "functional"], required=True)
    p.add_argument("--change")
    p.add_argument("--business-commit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    def repo_options(cmd):
        cmd.add_argument("--repo-dir", default=".")
        cmd.add_argument("--base", default="main")
        cmd.add_argument("--no-git", action="store_true", help="не создавать ветку")
        cmd.add_argument("--force", action="store_true", help="перезаписать существующий документ")

    p = sub.add_parser("start", help="взять Business Idea в работу: Change ID, ветка, каркас бизнес-спецификации")
    p.add_argument("idea")
    p.add_argument("--author")
    repo_options(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("start-system", help="каркас функциональной спецификации после merge бизнес-спецификации")
    p.add_argument("change")
    p.add_argument("--component")
    repo_options(p)
    p.set_defaults(func=cmd_start_system)

    p = sub.add_parser("start-dev", help="ветка реализации и контекст агента для задачи разработки")
    p.add_argument("change")
    p.add_argument("jira")
    repo_options(p)
    p.set_defaults(func=cmd_start_dev)

    p = sub.add_parser("open-pr", help="отправить ветку и открыть PR с метаданными SDD")
    p.add_argument("--change", required=True)
    p.add_argument("--stage", choices=list(STAGES), required=True)
    p.add_argument("--jira")
    p.add_argument("--project")
    p.add_argument("--repo")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--reviewer", action="append")
    p.add_argument("--repo-dir", default=".")
    p.add_argument("--base", default="main")
    p.add_argument("--remote", default="origin")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--allow-invalid", action="store_true")
    p.set_defaults(func=cmd_open_pr)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
