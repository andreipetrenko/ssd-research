from conftest import example

from sdd_orchestrator.services.spec_parser import (
    parse_decomposition,
    validate_business_spec,
    validate_functional_spec,
)
from sdd_orchestrator.settings import PROJECT_ROOT

TEMPLATES = PROJECT_ROOT / "openspec-templates/business-specs/openspec/schemas/sdd/templates"


def test_example_business_spec_is_valid():
    spec = validate_business_spec(example("business-spec.md", CHANGE_ID="CHG-1042", IDEA_KEY="IDEA-123"),
                                  change_id="CHG-1042")
    assert spec.errors == []
    assert spec.title == "Бизнес-спецификация: Онлайн-заявка на кредитные каникулы"
    assert spec.metadata["исходная jira"] == "IDEA-123"


def test_empty_business_template_lists_every_gap():
    spec = validate_business_spec((TEMPLATES / "business-spec.md").read_text(encoding="utf-8"))
    joined = "\n".join(spec.errors)
    assert "Заголовок содержит незаполненный шаблон" in joined
    assert "В метаданных не указан Change ID" in joined
    assert "Раздел «Проблема» пуст" in joined
    assert "Раздел «Открытые вопросы» пуст" not in joined


def test_change_id_mismatch_is_an_error():
    spec = validate_business_spec(example("business-spec.md", CHANGE_ID="CHG-1", IDEA_KEY="IDEA-1"),
                                  change_id="CHG-2")
    assert any("не совпадает" in e for e in spec.errors)


def test_acceptance_criteria_must_be_given_when_then():
    text = example("business-spec.md", CHANGE_ID="CHG-1", IDEA_KEY="IDEA-1").replace("Дано", "Если")
    spec = validate_business_spec(text, change_id="CHG-1")
    assert any("Given / When / Then" in e for e in spec.errors)


def test_functional_spec_decomposition_and_dependencies():
    text = example("functional-spec.md", CHANGE_ID="CHG-1", BUSINESS_COMMIT="abcdef1234567", ANALYSIS_KEY="ANL-1")
    spec = validate_functional_spec(text, change_id="CHG-1", business_commit="abcdef1234567890")
    assert spec.errors == []
    assert [(t.ref, t.depends_on) for t in spec.tasks] == [("DEV-1", []), ("DEV-2", ["DEV-1"]), ("DEV-3", ["DEV-1"])]


def test_functional_spec_must_reference_the_approved_business_commit():
    text = example("functional-spec.md", CHANGE_ID="CHG-1", BUSINESS_COMMIT="1111111", ANALYSIS_KEY="ANL-1")
    spec = validate_functional_spec(text, change_id="CHG-1", business_commit="2222222222")
    assert any("согласована бизнес-спецификация" in e for e in spec.errors)


def test_template_comments_do_not_produce_tasks():
    spec = validate_functional_spec((TEMPLATES / "functional-spec.md").read_text(encoding="utf-8"))
    assert spec.tasks == []
    assert any("Декомпозиция" in e for e in spec.errors)


def test_unknown_dependency_is_reported():
    tasks = parse_decomposition("- DEV-1: A\n- Зависимости:\n  - DEV-1 после DEV-9")
    assert tasks[0].depends_on == ["DEV-9"]
    text = example("functional-spec.md", CHANGE_ID="CHG-1", BUSINESS_COMMIT="abcdef1", ANALYSIS_KEY="ANL-1")
    text = text.replace("DEV-3 после DEV-1", "DEV-3 после DEV-9")
    spec = validate_functional_spec(text, change_id="CHG-1")
    assert any("неизвестной задачи DEV-9" in e for e in spec.errors)
