"""First-run setup: write .env, check Jira/Bitbucket, hang repo webhooks, start Postgres."""

from __future__ import annotations

import getpass
import json
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from .container import Services, build_services
from .services.http import IntegrationError
from .settings import PROJECT_ROOT, Settings, get_settings

SKIP_CREATE_FIELDS = {
    "project", "issuetype", "summary", "description", "reporter", "assignee",
    "labels", "priority", "attachment", "issuelinks",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.checks)

    def add(self, name: str, ok: bool, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name, ok, detail, hint))


def split_repo(value: str) -> tuple[str, str]:
    project, _, slug = value.strip().partition("/")
    if not project or not slug:
        raise ValueError(f"Репозиторий должен быть в виде ПРОЕКТ/имя, а не {value!r}")
    return project, slug


def configured_repos(settings: Settings) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for raw in settings.spec_repository_list + settings.code_repository_list:
        pair = split_repo(raw)
        if pair not in seen:
            seen.append(pair)
    return seen


def write_env(path: Path, values: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            existing[key.strip()] = value
    existing.update({key: "" if value is None else str(value) for key, value in values.items()})
    lines = [
        "# Этот файл создал `sdd init`. Токены сюда, в git не коммитить.",
        f"SDD_PUBLIC_URL={existing.get('SDD_PUBLIC_URL', 'http://127.0.0.1:48080')}",
        f"SDD_HOST={existing.get('SDD_HOST', '127.0.0.1')}",
        f"SDD_PORT={existing.get('SDD_PORT', '48080')}",
        f"SDD_DATABASE_URL={existing.get('SDD_DATABASE_URL', 'postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd')}",
        "",
        f"SDD_BITBUCKET_BASE_URL={existing.get('SDD_BITBUCKET_BASE_URL', '')}",
        f"SDD_BITBUCKET_TOKEN={existing.get('SDD_BITBUCKET_TOKEN', '')}",
        f"SDD_BITBUCKET_WEBHOOK_SECRET={existing.get('SDD_BITBUCKET_WEBHOOK_SECRET', '')}",
        f"SDD_SPEC_REPOSITORIES={existing.get('SDD_SPEC_REPOSITORIES', '')}",
        f"SDD_CODE_REPOSITORIES={existing.get('SDD_CODE_REPOSITORIES', '')}",
        f"SDD_TARGET_BRANCHES={existing.get('SDD_TARGET_BRANCHES', 'master')}",
        f"SDD_MIN_APPROVALS={existing.get('SDD_MIN_APPROVALS', '1')}",
        f"SDD_REQUIRE_CI={existing.get('SDD_REQUIRE_CI', 'true')}",
        "",
        f"SDD_JIRA_BASE_URL={existing.get('SDD_JIRA_BASE_URL', '')}",
        f"SDD_JIRA_TOKEN={existing.get('SDD_JIRA_TOKEN', '')}",
        f"SDD_JIRA_IDEA_ISSUE_TYPE={existing.get('SDD_JIRA_IDEA_ISSUE_TYPE', '')}",
        f"SDD_JIRA_IDEA_REQUIRED_FIELDS={existing.get('SDD_JIRA_IDEA_REQUIRED_FIELDS', 'summary,description')}",
        f"SDD_JIRA_ANALYSIS_PROJECT={existing.get('SDD_JIRA_ANALYSIS_PROJECT', '')}",
        f"SDD_JIRA_ANALYSIS_ISSUE_TYPE={existing.get('SDD_JIRA_ANALYSIS_ISSUE_TYPE', 'Task')}",
        f"SDD_JIRA_DEV_PROJECT={existing.get('SDD_JIRA_DEV_PROJECT', '')}",
        f"SDD_JIRA_DEV_ISSUE_TYPE={existing.get('SDD_JIRA_DEV_ISSUE_TYPE', 'Task')}",
        f"SDD_JIRA_DONE_TRANSITION={existing.get('SDD_JIRA_DONE_TRANSITION', 'Done')}",
    ]
    extra = existing.get("SDD_JIRA_DEV_EXTRA_FIELDS")
    if extra and extra not in ("", "{}"):
        lines.append(f"SDD_JIRA_DEV_EXTRA_FIELDS={extra}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        typed = getpass.getpass(f"{prompt}{suffix}: ")
    else:
        typed = input(f"{prompt}{suffix}: ")
    return (typed or "").strip() or default


def collect_init_values(args) -> dict[str, str]:
    values = {
        "SDD_PUBLIC_URL": args.public_url,
        "SDD_DATABASE_URL": args.database_url,
        "SDD_JIRA_BASE_URL": args.jira_url,
        "SDD_JIRA_TOKEN": args.jira_token,
        "SDD_BITBUCKET_BASE_URL": args.bitbucket_url,
        "SDD_BITBUCKET_TOKEN": args.bitbucket_token,
        "SDD_SPEC_REPOSITORIES": args.spec_repo,
        "SDD_CODE_REPOSITORIES": args.code_repo,
        "SDD_TARGET_BRANCHES": args.target_branch,
        "SDD_JIRA_IDEA_ISSUE_TYPE": args.idea_type,
        "SDD_JIRA_ANALYSIS_PROJECT": args.analysis_project,
        "SDD_JIRA_ANALYSIS_ISSUE_TYPE": args.analysis_type,
        "SDD_JIRA_DEV_PROJECT": args.dev_project,
        "SDD_JIRA_DEV_ISSUE_TYPE": args.dev_type,
        "SDD_JIRA_DONE_TRANSITION": args.done_transition,
        "SDD_REQUIRE_CI": "false" if args.no_ci else "true",
        "SDD_BITBUCKET_WEBHOOK_SECRET": args.webhook_secret or secrets.token_urlsafe(24),
    }
    if args.non_interactive:
        required = [
            "SDD_PUBLIC_URL", "SDD_DATABASE_URL", "SDD_JIRA_BASE_URL", "SDD_JIRA_TOKEN",
            "SDD_BITBUCKET_BASE_URL", "SDD_BITBUCKET_TOKEN", "SDD_SPEC_REPOSITORIES",
            "SDD_CODE_REPOSITORIES", "SDD_JIRA_IDEA_ISSUE_TYPE", "SDD_JIRA_ANALYSIS_PROJECT",
            "SDD_JIRA_DEV_PROJECT",
        ]
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise SystemExit("Для --non-interactive не хватает: " + ", ".join(missing))
        return values

    print("Ответьте на вопросы. Пустой ввод оставляет значение в скобках.\n")
    values["SDD_JIRA_BASE_URL"] = _ask(
        "Адрес Jira, как в браузере", values["SDD_JIRA_BASE_URL"] or "https://jira.company.ru")
    values["SDD_JIRA_TOKEN"] = _ask(
        "Токен сервисной учётки Jira (Personal Access Token)",
        values["SDD_JIRA_TOKEN"], secret=True)
    values["SDD_BITBUCKET_BASE_URL"] = _ask(
        "Адрес Bitbucket, как в браузере", values["SDD_BITBUCKET_BASE_URL"] or "https://bitbucket.company.ru"
    )
    values["SDD_BITBUCKET_TOKEN"] = _ask("Токен сервисной учётки Bitbucket", values["SDD_BITBUCKET_TOKEN"], secret=True)
    values["SDD_PUBLIC_URL"] = _ask(
        "Адрес, по которому Bitbucket достучится до этого сервиса",
        values["SDD_PUBLIC_URL"] or "https://sdd.company.ru",
    )
    values["SDD_DATABASE_URL"] = _ask(
        "Строка PostgreSQL",
        values["SDD_DATABASE_URL"] or "postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd",
    )
    values["SDD_SPEC_REPOSITORIES"] = _ask(
        "Репозиторий спецификаций (ПРОЕКТ/имя)", values["SDD_SPEC_REPOSITORIES"] or "SPEC/business-specs"
    )
    values["SDD_CODE_REPOSITORIES"] = _ask(
        "Репозиторий кода (ПРОЕКТ/имя)", values["SDD_CODE_REPOSITORIES"] or "APP/application-repo"
    )
    values["SDD_TARGET_BRANCHES"] = _ask("Ветка, в которую сливают PR", values["SDD_TARGET_BRANCHES"] or "master")
    values["SDD_JIRA_IDEA_ISSUE_TYPE"] = _ask("Тип задачи-идеи в Jira", values["SDD_JIRA_IDEA_ISSUE_TYPE"] or "Epic")
    values["SDD_JIRA_ANALYSIS_PROJECT"] = _ask(
        "Проект Jira для задач системного анализа", values["SDD_JIRA_ANALYSIS_PROJECT"])
    values["SDD_JIRA_ANALYSIS_ISSUE_TYPE"] = _ask(
        "Тип задачи системного анализа", values["SDD_JIRA_ANALYSIS_ISSUE_TYPE"] or "Task")
    values["SDD_JIRA_DEV_PROJECT"] = _ask("Проект Jira для задач разработки", values["SDD_JIRA_DEV_PROJECT"])
    values["SDD_JIRA_DEV_ISSUE_TYPE"] = _ask("Тип задачи разработки", values["SDD_JIRA_DEV_ISSUE_TYPE"] or "Task")
    values["SDD_JIRA_DONE_TRANSITION"] = _ask(
        "Кнопка закрытия задачи разработки (пусто — не менять статус)", values["SDD_JIRA_DONE_TRANSITION"] or "Done"
    )
    if not values["SDD_REQUIRE_CI"] or values["SDD_REQUIRE_CI"] == "true":
        ci = _ask("CI пишет статус сборки в Bitbucket? (да/нет)", "да")
        values["SDD_REQUIRE_CI"] = "false" if ci.lower() in {"нет", "no", "n", "false"} else "true"
    return values


def run_doctor(services: Services | None = None, settings: Settings | None = None) -> DoctorReport:
    settings = settings or get_settings()
    report = DoctorReport()
    try:
        services = services or build_services(settings)
    except Exception as exc:  # noqa: BLE001
        report.add("Сервис", False, f"не собрался: {exc}")
        return report

    try:
        with services.engine.connect() as conn:
            from sqlalchemy import text

            conn.execute(text("SELECT 1"))
        report.add("PostgreSQL", True, "подключение есть")
    except Exception as exc:  # noqa: BLE001
        report.add("PostgreSQL", False, str(exc),
                   "Поднимите базу: docker compose up -d db   или поправьте SDD_DATABASE_URL")

    _check_jira(services, settings, report)
    _check_bitbucket(services, settings, report)
    return report


def _check_jira(services: Services, settings: Settings, report: DoctorReport) -> None:
    if not settings.jira_base_url:
        report.add("Jira", False, "не задан SDD_JIRA_BASE_URL")
        return
    pairs = [
        (settings.jira_analysis_project, settings.jira_analysis_issue_type, "анализа"),
        (settings.jira_dev_project, settings.jira_dev_issue_type, "разработки"),
    ]
    for project, issue_type, title in pairs:
        if not project:
            report.add(f"Jira {title}", False, "проект не задан")
            continue
        try:
            types = [item.get("name") for item in services.jira.get_issue_types(project)]
        except IntegrationError as exc:
            report.add(f"Jira {project}", False, str(exc),
                       "Проверьте URL, токен и что учётка видит проект")
            continue
        if issue_type not in types:
            report.add(f"Jira {project}", False, f"типа «{issue_type}» нет. Есть: {', '.join(types)}",
                       "Поставьте SDD_JIRA_"
                       f"{'ANALYSIS' if title == 'анализа' else 'DEV'}_ISSUE_TYPE в одно из имён выше")
            continue
        report.add(f"Jira {project} / {issue_type}", True, "тип задачи доступен")
        try:
            fields = services.jira.get_create_fields(project, issue_type)
        except IntegrationError as exc:
            report.add(f"Поля {project}", True, f"createmeta недоступна ({exc}); но тип есть")
            continue
        required = [
            name for name, meta in fields.items()
            if meta.get("required") and name not in SKIP_CREATE_FIELDS
        ]
        if required:
            sample = {name: "<значение>" for name in required}
            report.add(
                f"Обязательные поля {project}", False,
                "Jira просит ещё: " + ", ".join(required),
                "Добавьте в .env: SDD_JIRA_"
                f"{'ANALYSIS' if title == 'анализа' else 'DEV'}_EXTRA_FIELDS="
                + json.dumps(sample, ensure_ascii=False),
            )
        else:
            report.add(f"Обязательные поля {project}", True, "дополнительных нет")
    try:
        names = [item.get("name") for item in services.jira.list_link_types()]
        relates = services.jira.resolve_link_type(settings.jira_relates_link_type, ["Relates", "Связывает"])
        report.add("Типы связей Jira", relates in names, f"будем использовать «{relates}»",
                   f"Доступны: {', '.join(names)}" if relates not in names else "")
    except IntegrationError as exc:
        report.add("Типы связей Jira", False, str(exc))


def _check_bitbucket(services: Services, settings: Settings, report: DoctorReport) -> None:
    if not settings.bitbucket_base_url:
        report.add("Bitbucket", False, "не задан SDD_BITBUCKET_BASE_URL")
        return
    webhook_url = settings.public_url.rstrip("/") + "/webhooks/bitbucket"
    for project, slug in configured_repos(settings):
        try:
            repo = services.bitbucket.get_repository(project, slug)
        except IntegrationError as exc:
            report.add(f"Репозиторий {project}/{slug}", False, str(exc),
                       "Проверьте ключ проекта, slug и права Read у сервисной учётки")
            continue
        if repo is None:
            report.add(f"Репозиторий {project}/{slug}", False, "не найден")
            continue
        report.add(f"Репозиторий {project}/{slug}", True, "доступен")
        try:
            repo_hooks = services.bitbucket.list_repo_webhooks(project, slug)
        except IntegrationError:
            repo_hooks = []
        try:
            project_hooks = services.bitbucket.list_webhooks(project)
        except IntegrationError:
            project_hooks = []
        targets = services.bitbucket.webhook_targets(repo_hooks + project_hooks, settings.public_url)
        if targets:
            where = "репозитория" if services.bitbucket.webhook_targets(repo_hooks, settings.public_url) else "проекта"
            report.add(f"Webhook {project}/{slug}", True, f"висит на уровне {where}: {webhook_url}")
        else:
            report.add(
                f"Webhook {project}/{slug}", False, "ещё не создан",
                f"Запустите: uv run sdd register-webhook --repo {project}/{slug}",
            )
        try:
            merged = services.bitbucket.list_pull_requests(project, slug, state="MERGED")
        except IntegrationError:
            merged = []
        if merged:
            from .services.bitbucket_client import merge_commit_of

            sample = merged[0]
            if merge_commit_of(sample):
                report.add(f"Merge commit {project}/{slug}", True, f"PR #{sample.get('id')} отдаёт merge commit")
            else:
                report.add(
                    f"Merge commit {project}/{slug}", False,
                    f"у слитого PR #{sample.get('id')} нет properties.mergeCommit",
                    "На этой версии Bitbucket сервис не сможет надёжно обработать merge. Напишите администратору.",
                )


def print_report(report: DoctorReport) -> None:
    print()
    for item in report.checks:
        mark = "OK" if item.ok else "НЕТ"
        print(f"  [{mark}] {item.name}: {item.detail}")
        if item.hint and not item.ok:
            print(f"         → {item.hint}")
    print()
    if report.ok:
        print("Всё готово. Запускайте:  uv run sdd serve")
        print("Дашборд:                 " + get_settings().public_url)
    else:
        print("Исправьте пункты с [НЕТ] и снова запустите:  uv run sdd doctor")


def register_configured_webhooks(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    bitbucket = build_services(settings).bitbucket
    url = settings.public_url.rstrip("/") + "/webhooks/bitbucket"
    lines = []
    for project, slug in configured_repos(settings):
        result = bitbucket.ensure_repo_webhook(
            project, slug, url=url, secret=settings.bitbucket_webhook_secret
        )
        verb = "создан" if result.get("created") else "уже был"
        lines.append(f"{project}/{slug}: webhook {verb} → {url}")
    return lines


def uses_local_mocks(settings: Settings) -> bool:
    return "48090" in settings.jira_base_url or "48090" in settings.bitbucket_base_url


def db_is_up(url: str) -> bool:
    from sqlalchemy import text

    from .db import make_engine

    try:
        engine = make_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:  # noqa: BLE001
        return False
    return True


def ensure_database(url: str) -> str:
    """Connect to Postgres, or start/create it. Raises SystemExit if nothing works."""
    if db_is_up(url):
        return "PostgreSQL: подключение есть"
    compose = PROJECT_ROOT / "docker-compose.yml"
    docker = shutil.which("docker")
    if docker and compose.exists():
        subprocess.run([docker, "compose", "-f", str(compose), "up", "-d", "db"], check=False)
        for _ in range(40):
            if db_is_up(url):
                return "PostgreSQL: поднята через docker compose"
            time.sleep(0.5)
    if _bootstrap_local_postgres(url) and db_is_up(url):
        return "PostgreSQL: созданы пользователь и база"
    raise SystemExit(
        "Не удалось подключиться к PostgreSQL.\n"
        "Поставьте PostgreSQL 16 или Docker и повторите `sdd up`.\n"
        f"Ожидаемая строка: {url}"
    )


def _parse_db_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url.replace("postgresql+psycopg://", "postgresql://", 1))
    user = unquote(parsed.username or "sdd")
    password = unquote(parsed.password or "sdd")
    name = (parsed.path or "/sdd").lstrip("/") or "sdd"
    return user, password, name


def _bootstrap_local_postgres(url: str) -> bool:
    user, password, name = _parse_db_url(url)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", user) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return False
    quoted = password.replace("'", "''")
    role = _psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{user}'")
    if role is None:
        return False
    if "1" not in role:
        if _psql(f"CREATE USER {user} WITH PASSWORD '{quoted}' CREATEDB") is None:
            return False
    existing = _psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'") or ""
    if "1" not in existing and _psql(f"CREATE DATABASE {name} OWNER {user}") is None:
        return False
    test_db = f"{name}_test" if not name.endswith("_test") else None
    if test_db and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", test_db):
        found = _psql(f"SELECT 1 FROM pg_database WHERE datname = '{test_db}'") or ""
        if "1" not in found:
            _psql(f"CREATE DATABASE {test_db} OWNER {user}")
    return True


def _psql(sql: str) -> str | None:
    sudo = shutil.which("sudo")
    if not sudo:
        return None
    result = subprocess.run(
        [sudo, "-n", "-u", "postgres", "psql", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def copy_spec_template(target: Path) -> list[str]:
    source = PROJECT_ROOT / "openspec-templates" / "business-specs"
    written: list[str] = []
    if not source.is_dir():
        return written
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        dest = target / path.relative_to(source)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
        written.append(str(dest.relative_to(target)))
    return written
