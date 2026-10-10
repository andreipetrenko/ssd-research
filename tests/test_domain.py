from sdd_orchestrator.domain import ArtifactType, branch_name, extract_pr_metadata


def test_business_branch_is_recognized():
    meta = extract_pr_metadata("CHG", "Spec", "", "feature/CHG-1042-business-spec")
    assert meta.change_id == "CHG-1042"
    assert meta.artifact_type is ArtifactType.BUSINESS_SPECIFICATION
    assert meta.errors == []


def test_implementation_branch_carries_jira_key():
    meta = extract_pr_metadata("CHG", "Code", "", "feature/CHG-1042-DEV-231-implementation")
    assert meta.artifact_type is ArtifactType.IMPLEMENTATION
    assert meta.jira_key == "DEV-231"
    assert meta.errors == []


def test_description_is_used_when_branch_does_not_follow_convention():
    meta = extract_pr_metadata(
        "CHG", "x", "Change ID: CHG-7\nStage: functional_specification\nJira: ANL-1", "bugfix/something"
    )
    assert meta.change_id == "CHG-7"
    assert meta.artifact_type is ArtifactType.FUNCTIONAL_SPECIFICATION
    assert meta.jira_key == "ANL-1"


def test_conflicting_change_ids_are_reported():
    meta = extract_pr_metadata("CHG", "x", "Change ID: CHG-2", "feature/CHG-1-business-spec")
    assert any("не совпадает" in error for error in meta.errors)


def test_missing_metadata_is_reported():
    meta = extract_pr_metadata("CHG", "Some PR", "", "feature/whatever")
    assert meta.change_id is None
    assert len(meta.errors) == 2


def test_implementation_without_jira_is_rejected():
    meta = extract_pr_metadata("CHG", "x", "Change ID: CHG-1\nStage: implementation", "feature/x")
    assert any("Jira" in error for error in meta.errors)


def test_branch_names_follow_the_convention():
    assert branch_name("CHG-1", ArtifactType.BUSINESS_SPECIFICATION) == "feature/CHG-1-business-spec"
    assert branch_name("CHG-1", ArtifactType.FUNCTIONAL_SPECIFICATION) == "feature/CHG-1-system-spec"
    assert branch_name("CHG-1", ArtifactType.IMPLEMENTATION, "DEV-5") == "feature/CHG-1-DEV-5-implementation"
