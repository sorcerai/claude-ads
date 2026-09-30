"""Fail-closed tests for typed workflow artifacts and immutable orchestration."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from claude_ads_core.contracts import CONTRACT_NAMES, ContractError, validate_contract
from claude_ads_core.lifecycle import make_pending_lifecycle
from claude_ads_core.orchestration import (
    OrchestrationError,
    OrchestrationStore,
    evaluate_artifact_gate,
)


@pytest.fixture()
def workflow_fixtures(repo_root: Path) -> dict[str, dict]:
    path = repo_root / "tests/fixtures/workflows/valid-artifacts.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _contract_name(fixture_name: str) -> str:
    if fixture_name.startswith("experiment-"):
        return "experiment-artifact"
    return fixture_name


INTEGER_SCHEMA_FIELDS = (
    ("data-lifecycle", ("retention", "minimum_seconds"), 0),
    ("generation-manifest", ("outputs", 0, "width"), 1),
    ("generation-manifest", ("outputs", 0, "height"), 1),
)


def _set_path(payload: dict, path: tuple[str | int, ...], value) -> None:
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def test_all_workflow_fixtures_validate(workflow_fixtures):
    for fixture_name, payload in workflow_fixtures.items():
        contract = _contract_name(fixture_name)
        assert contract in CONTRACT_NAMES
        validate_contract(contract, payload)


@pytest.fixture(scope="module")
def portable_workflow_schemas(repo_root):
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    schemas = repo_root / "claude_ads_core" / "schemas"
    registry = referencing.Registry()
    for path in schemas.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(
            schema["$id"], referencing.Resource.from_contents(schema)
        )
    return jsonschema, registry, schemas


def test_historical_workflow_schemas_reject_unknown_and_missing_fields(
    workflow_fixtures, portable_workflow_schemas
):
    jsonschema, registry, schemas = portable_workflow_schemas
    for fixture_name, payload in workflow_fixtures.items():
        contract = _contract_name(fixture_name)
        major = payload["schema_version"].split(".", 1)[0]
        schema = json.loads(
            (schemas / f"v{major}" / f"{contract}.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
        validator.validate(payload)
        unexpected = {**payload, "unreviewed_extension": "fixture"}
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(unexpected)
        missing_version = {
            key: value for key, value in payload.items() if key != "schema_version"
        }
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(missing_version)


@pytest.mark.parametrize(("fixture_name", "path", "minimum"), INTEGER_SCHEMA_FIELDS)
def test_historical_portable_workflow_integer_boundaries(
    workflow_fixtures, portable_workflow_schemas, fixture_name, path, minimum
):
    jsonschema, registry, schemas = portable_workflow_schemas
    contract = _contract_name(fixture_name)
    schema = json.loads(
        (schemas / "v1" / f"{contract}.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    for value in (True, False, minimum + 0.5, minimum - 1):
        payload = copy.deepcopy(workflow_fixtures[fixture_name])
        _set_path(payload, path, value)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(payload)


@pytest.mark.parametrize(("fixture_name", "path", "minimum"), INTEGER_SCHEMA_FIELDS)
@pytest.mark.parametrize(
    "invalid_kind",
    ("bool-true", "bool-false", "fraction", "float", "string", "null", "below-minimum"),
)
def test_every_schema_integer_field_rejects_non_integer_or_below_minimum(
    workflow_fixtures, fixture_name, path, minimum, invalid_kind
):
    payload = copy.deepcopy(workflow_fixtures[fixture_name])
    values = {
        "bool-true": True,
        "bool-false": False,
        "fraction": minimum + 0.5,
        "float": float(minimum),
        "string": str(minimum),
        "null": None,
        "below-minimum": minimum - 1,
    }
    _set_path(payload, path, values[invalid_kind])
    with pytest.raises(ContractError, match="integer|must be >="):
        validate_contract(fixture_name, payload)


@pytest.mark.parametrize(("fixture_name", "path", "minimum"), INTEGER_SCHEMA_FIELDS)
@pytest.mark.parametrize("offset", (0, 1, 10_000))
def test_every_schema_integer_field_accepts_integer_values_at_or_above_minimum(
    workflow_fixtures, fixture_name, path, minimum, offset
):
    payload = copy.deepcopy(workflow_fixtures[fixture_name])
    _set_path(payload, path, minimum + offset)
    validate_contract(fixture_name, payload)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "setup-profile",
        "brand-profile",
        "media-plan",
        "creative-brief",
        "generation-manifest",
        "monitoring-bundle",
        "experiment-setup",
        "experiment-readout",
        "mutation-plan",
        "orchestration-run",
    ],
)
def test_embedded_data_lifecycle_integer_validation_is_identical_across_contracts(
    workflow_fixtures, fixture_name
):
    payload = copy.deepcopy(workflow_fixtures[fixture_name])
    payload["data_lifecycle"]["retention"]["minimum_seconds"] = 0.5
    with pytest.raises(ContractError, match="minimum_seconds must be an integer"):
        validate_contract(_contract_name(fixture_name), payload)


def test_schema_number_fields_still_accept_fractional_values(workflow_fixtures):
    media_plan = copy.deepcopy(workflow_fixtures["media-plan"])
    media_plan["channels"][0]["budget_amount"] = 0.5
    validate_contract("media-plan", media_plan)

    generation = copy.deepcopy(workflow_fixtures["generation-manifest"])
    generation["outputs"][0]["cost"] = {"currency": "USD", "amount": 0.5}
    validate_contract("generation-manifest", generation)

    mutation = copy.deepcopy(workflow_fixtures["mutation-plan"])
    mutation["ceilings"][0]["value"] = 0.5
    validate_contract("mutation-plan", mutation)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "data-lifecycle",
        "setup-profile",
        "brand-profile",
        "media-plan",
        "creative-brief",
        "generation-manifest",
        "monitoring-bundle",
        "experiment-setup",
        "experiment-readout",
        "mutation-plan",
        "orchestration-run",
        "orchestration-task",
        "orchestration-result",
        "orchestration-gate",
    ],
)
def test_contracts_reject_unknown_fields(workflow_fixtures, fixture_name):
    payload = copy.deepcopy(workflow_fixtures[fixture_name])
    payload["unreviewed_extension"] = True
    with pytest.raises(ContractError, match="unknown field"):
        validate_contract(_contract_name(fixture_name), payload)


def test_experiment_setup_and_readout_are_distinct(workflow_fixtures):
    setup = copy.deepcopy(workflow_fixtures["experiment-setup"])
    setup["decision"] = "peeked early"
    with pytest.raises(ContractError, match="must not contain result or decision"):
        validate_contract("experiment-artifact", setup)

    readout = copy.deepcopy(workflow_fixtures["experiment-readout"])
    readout["result"] = None
    with pytest.raises(ContractError, match="must be an object"):
        validate_contract("experiment-artifact", readout)


def test_mutation_plan_never_authorizes_itself(workflow_fixtures):
    plan = copy.deepcopy(workflow_fixtures["mutation-plan"])
    plan["status"] = "applied"
    with pytest.raises(ContractError, match="approval is required"):
        validate_contract("mutation-plan", plan)

    deletion = copy.deepcopy(workflow_fixtures["mutation-plan"])
    deletion["operation"] = "permanent-delete"
    with pytest.raises(ContractError, match="deletion is outside"):
        validate_contract("mutation-plan", deletion)

    traversal = copy.deepcopy(workflow_fixtures["mutation-plan"])
    traversal["audit_destination"] = "../outside.json"
    with pytest.raises(ContractError, match="contained POSIX relative path"):
        validate_contract("mutation-plan", traversal)


def test_creative_copy_requires_current_specification_evidence(workflow_fixtures):
    brief = copy.deepcopy(workflow_fixtures["creative-brief"])
    brief["specification_source_ids"] = []
    with pytest.raises(ContractError, match="required when copy_deck is present"):
        validate_contract("creative-brief", brief)


def test_monitoring_bundle_cannot_hide_missing_inputs(workflow_fixtures):
    bundle = copy.deepcopy(workflow_fixtures["monitoring-bundle"])
    bundle["completeness"] = "complete"
    with pytest.raises(ContractError, match="cannot be complete"):
        validate_contract("monitoring-bundle", bundle)


def test_non_public_lifecycle_requires_encryption_and_deletion_deadline(
    workflow_fixtures,
):
    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["encryption"]["at_rest"] = "not-applicable"
    with pytest.raises(ContractError, match="verified at-rest and in-transit"):
        validate_contract("data-lifecycle", lifecycle)

    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["retention"]["delete_after"] = None
    with pytest.raises(ContractError, match="delete_after is required"):
        validate_contract("data-lifecycle", lifecycle)


def test_v2_lifecycle_discloses_unknown_controls_without_inventing_retention(
    workflow_fixtures,
):
    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["schema_version"] = "2.0.0"
    lifecycle["retention"]["mode"] = "unassigned"
    lifecycle["retention"]["delete_after"] = None
    lifecycle["encryption"] = {
        "at_rest": "unknown",
        "in_transit": "unknown",
        "evidence_refs": [],
    }
    lifecycle["deletion"]["status"] = "pending"
    lifecycle["deletion"]["method"] = "file-removal"
    lifecycle["deletion"]["scheduler_receipt_locator"] = None
    validate_contract("data-lifecycle", lifecycle)


@pytest.mark.parametrize(
    ("status", "locator"),
    (
        ("scheduled", "scheduler_receipt_locator"),
        ("verified", "verification_artifact_locator"),
    ),
)
def test_v2_lifecycle_requires_status_receipts(workflow_fixtures, status, locator):
    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["schema_version"] = "2.0.0"
    lifecycle["encryption"] = {
        "at_rest": "unknown",
        "in_transit": "unknown",
        "evidence_refs": [],
    }
    lifecycle["deletion"]["status"] = status
    lifecycle["deletion"]["scheduler_receipt_locator"] = None
    lifecycle["deletion"][locator] = None
    with pytest.raises(ContractError, match="requires a"):
        validate_contract("data-lifecycle", lifecycle)


def test_v2_lifecycle_requires_verification(workflow_fixtures):
    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["schema_version"] = "2.0.0"
    lifecycle["encryption"] = {
        "at_rest": "unknown",
        "in_transit": "unknown",
        "evidence_refs": [],
    }
    lifecycle["deletion"]["status"] = "pending"
    lifecycle["deletion"]["scheduler_receipt_locator"] = None
    lifecycle["deletion"]["verification_required"] = False
    with pytest.raises(ContractError, match="verification_required must be true"):
        validate_contract("data-lifecycle", lifecycle)


def test_v2_lifecycle_rejects_unassigned_deadline_and_unknown_version(
    workflow_fixtures,
):
    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["schema_version"] = "2.0.0"
    lifecycle["retention"]["mode"] = "unassigned"
    lifecycle["retention"]["delete_after"] = None
    lifecycle["encryption"] = {
        "at_rest": "unknown",
        "in_transit": "unknown",
        "evidence_refs": [],
    }
    lifecycle["deletion"]["status"] = "pending"
    lifecycle["deletion"]["scheduler_receipt_locator"] = None
    invalid_deadline = copy.deepcopy(lifecycle)
    invalid_deadline["retention"]["delete_after"] = "2026-07-12T16:00:00Z"
    with pytest.raises(ContractError, match="unassigned"):
        validate_contract("data-lifecycle", invalid_deadline)
    invalid_mode = copy.deepcopy(lifecycle)
    invalid_mode["retention"]["mode"] = "operator-defined"
    with pytest.raises(ContractError, match="requires a delete_after"):
        validate_contract("data-lifecycle", invalid_mode)
    unsupported = copy.deepcopy(lifecycle)
    unsupported["schema_version"] = "3.0.0"
    with pytest.raises(ContractError, match="schema_version"):
        validate_contract("data-lifecycle", unsupported)


def test_make_pending_lifecycle_is_truthful_when_retention_is_unassigned():
    pending = make_pending_lifecycle(
        lifecycle_id="lifecycle-case",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )
    assert pending["retention"]["mode"] == "unassigned"
    assert pending["retention"]["delete_after"] is None
    assert pending["encryption"] == {
        "at_rest": "unknown",
        "in_transit": "unknown",
        "evidence_refs": [],
    }
    assert pending["deletion"]["status"] == "pending"
    validate_contract("data-lifecycle", pending)


@pytest.mark.parametrize("field", ("at_rest", "in_transit"))
def test_v2_verified_encryption_requires_evidence(field):
    pending = make_pending_lifecycle(
        lifecycle_id="lifecycle-unverified",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )
    pending["encryption"][field] = "verified"
    with pytest.raises(ContractError, match="verified controls require evidence"):
        validate_contract("data-lifecycle", pending)


@pytest.mark.parametrize("roles", ("admin", b"admin"))
def test_make_pending_lifecycle_rejects_scalar_roles(roles):
    with pytest.raises(TypeError):
        make_pending_lifecycle(
            lifecycle_id="lifecycle-roles",
            classification="internal",
            delete_after=None,
            purpose="In-memory review",
            owner="operator",
            authorized_roles=roles,
            reporting_channel="security@operator",
        )


def test_make_pending_lifecycle_accepts_tuple_roles():
    pending = make_pending_lifecycle(
        lifecycle_id="lifecycle-tuple",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=("operator", "reviewer"),
        reporting_channel="security@operator",
    )
    assert pending["access"]["authorized_roles"] == ["operator", "reviewer"]


@pytest.fixture(scope="module")
def v2_lifecycle_validator(repo_root):
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    schemas = repo_root / "claude_ads_core" / "schemas"
    lifecycle_schema = json.loads(
        (schemas / "v2" / "data-lifecycle.schema.json").read_text()
    )
    common_schema = json.loads(
        (schemas / "v1" / "workflow-common.schema.json").read_text()
    )
    registry = referencing.Registry().with_resource(
        common_schema["$id"], referencing.Resource.from_contents(common_schema)
    )
    return (
        jsonschema.Draft202012Validator(lifecycle_schema, registry=registry),
        jsonschema.ValidationError,
    )


def test_v2_json_schema_rejects_unsubstantiated_verified_encryption(
    v2_lifecycle_validator,
):
    validator, validation_error = v2_lifecycle_validator
    pending = make_pending_lifecycle(
        lifecycle_id="lifecycle-unverified",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )
    pending["encryption"]["at_rest"] = "verified"
    with pytest.raises(validation_error):
        validator.validate(pending)


@pytest.mark.parametrize("field", ("at_rest", "in_transit"))
@pytest.mark.parametrize("blank", (" ", "\n"))
def test_v2_portable_and_runtime_contracts_reject_blank_encryption_evidence(
    v2_lifecycle_validator, field, blank
):
    validator, validation_error = v2_lifecycle_validator
    pending = make_pending_lifecycle(
        lifecycle_id="lifecycle-blank-evidence",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )
    pending["encryption"][field] = "verified"
    pending["encryption"]["evidence_refs"] = [blank]
    with pytest.raises(ContractError):
        validate_contract("data-lifecycle", pending)
    with pytest.raises(validation_error):
        validator.validate(pending)


@pytest.mark.parametrize(
    ("section", "locator_field", "status"),
    (
        ("access", "access_log_locator", None),
        ("deletion", "scheduler_receipt_locator", "scheduled"),
        ("deletion", "verification_artifact_locator", "verified"),
        ("incident", "record_locator", "open"),
    ),
)
@pytest.mark.parametrize(
    "locator", ("receipts/./job", "receipts//job", "receipts/job/", "receipts/\x00job")
)
def test_v2_portable_and_runtime_contracts_reject_malformed_locators(
    v2_lifecycle_validator, section, locator_field, status, locator
):
    validator, validation_error = v2_lifecycle_validator
    pending = make_pending_lifecycle(
        lifecycle_id="lifecycle-receipt",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )
    if status is not None:
        pending[section]["status"] = status
    pending[section][locator_field] = locator
    with pytest.raises(ContractError):
        validate_contract("data-lifecycle", pending)
    with pytest.raises(validation_error):
        validator.validate(pending)


@pytest.fixture()
def v2_pending_lifecycle():
    return make_pending_lifecycle(
        lifecycle_id="lifecycle-conditional",
        classification="internal",
        delete_after=None,
        purpose="In-memory review",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )


@pytest.mark.parametrize(
    ("classification", "mode"),
    (("internal", "operator-defined"), ("confidential", "policy-defined")),
)
def test_v2_nonpublic_assigned_retention_requires_deadline(
    v2_lifecycle_validator, v2_pending_lifecycle, classification, mode
):
    validator, validation_error = v2_lifecycle_validator
    v2_pending_lifecycle["classification"] = classification
    v2_pending_lifecycle["retention"]["mode"] = mode
    with pytest.raises(ContractError):
        validate_contract("data-lifecycle", v2_pending_lifecycle)
    with pytest.raises(validation_error):
        validator.validate(v2_pending_lifecycle)


@pytest.mark.parametrize("reason", (None, " ", "\n"))
def test_v2_retention_exception_requires_nonblank_reason(
    v2_lifecycle_validator, v2_pending_lifecycle, reason
):
    validator, validation_error = v2_lifecycle_validator
    v2_pending_lifecycle["retention"]["mode"] = "exception"
    v2_pending_lifecycle["retention"]["exception_reason"] = reason
    with pytest.raises(ContractError):
        validate_contract("data-lifecycle", v2_pending_lifecycle)
    with pytest.raises(validation_error):
        validator.validate(v2_pending_lifecycle)


def test_v2_retention_exception_allows_null_deadline(
    v2_lifecycle_validator, v2_pending_lifecycle
):
    validator, _ = v2_lifecycle_validator
    v2_pending_lifecycle["retention"]["mode"] = "exception"
    v2_pending_lifecycle["retention"]["exception_reason"] = "legal hold"
    validate_contract("data-lifecycle", v2_pending_lifecycle)
    validator.validate(v2_pending_lifecycle)


@pytest.mark.parametrize("status", ("open", "contained", "resolved"))
def test_v2_triggered_incident_requires_record(
    v2_lifecycle_validator, v2_pending_lifecycle, status
):
    validator, validation_error = v2_lifecycle_validator
    v2_pending_lifecycle["incident"]["status"] = status
    with pytest.raises(ContractError):
        validate_contract("data-lifecycle", v2_pending_lifecycle)
    with pytest.raises(validation_error):
        validator.validate(v2_pending_lifecycle)


def test_v1_lifecycle_still_rejects_unknown_encryption(workflow_fixtures):
    lifecycle = copy.deepcopy(workflow_fixtures["data-lifecycle"])
    lifecycle["encryption"]["at_rest"] = "unknown"
    with pytest.raises(ContractError):
        validate_contract("data-lifecycle", lifecycle)


@pytest.fixture()
def v2_setup_profile(workflow_fixtures):
    profile = copy.deepcopy(workflow_fixtures["setup-profile"])
    profile["schema_version"] = "2.0.0"
    profile["data_lifecycle"] = make_pending_lifecycle(
        lifecycle_id="lifecycle-setup",
        classification=profile["privacy_class"],
        delete_after=None,
        purpose="In-memory setup",
        owner="operator",
        authorized_roles=["operator"],
        reporting_channel="security@operator",
    )
    return profile


def test_setup_profile_versions_require_matching_lifecycle(
    workflow_fixtures, v2_setup_profile
):
    v1_setup = workflow_fixtures["setup-profile"]
    validate_contract("setup-profile", v1_setup)
    validate_contract("setup-profile", v2_setup_profile)
    for outer, nested in (
        (v1_setup, v2_setup_profile["data_lifecycle"]),
        (v2_setup_profile, v1_setup["data_lifecycle"]),
    ):
        with pytest.raises(ContractError):
            validate_contract("setup-profile", {**outer, "data_lifecycle": nested})
    with pytest.raises(ContractError):
        validate_contract(
            "setup-profile", {**v2_setup_profile, "schema_version": "3.0.0"}
        )
    brand = copy.deepcopy(workflow_fixtures["brand-profile"])
    brand["data_lifecycle"] = v2_setup_profile["data_lifecycle"]
    with pytest.raises(ContractError):
        validate_contract("brand-profile", brand)


def test_v2_setup_profile_portable_schema_pairs_v2_lifecycle(
    repo_root, workflow_fixtures, v2_setup_profile
):
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    schemas = repo_root / "claude_ads_core" / "schemas"
    setup_schema = json.loads(
        (schemas / "v2" / "setup-profile.schema.json").read_text()
    )
    common_schema = json.loads(
        (schemas / "v1" / "workflow-common.schema.json").read_text()
    )
    lifecycle_schema = json.loads(
        (schemas / "v2" / "data-lifecycle.schema.json").read_text()
    )
    registry = referencing.Registry()
    for schema in (common_schema, lifecycle_schema):
        registry = registry.with_resource(
            schema["$id"], referencing.Resource.from_contents(schema)
        )
    validator = jsonschema.Draft202012Validator(setup_schema, registry=registry)
    validator.validate(v2_setup_profile)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                **v2_setup_profile,
                "data_lifecycle": workflow_fixtures["setup-profile"]["data_lifecycle"],
            }
        )


def test_store_is_append_only_and_result_reruns_require_supersedes(
    tmp_path, workflow_fixtures
):
    store = OrchestrationStore(tmp_path / "orchestration")
    run = workflow_fixtures["orchestration-run"]
    task = workflow_fixtures["orchestration-task"]
    result = workflow_fixtures["orchestration-result"]
    store.write("run", run)
    store.write("task", task)
    first_path = store.write("result", result)
    assert first_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(OrchestrationError, match="already exists"):
        store.write("result", result)

    repeated = copy.deepcopy(result)
    repeated["result_id"] = "unlinked-repeat"
    repeated["created_at"] = "2026-07-11T10:20:00Z"
    with pytest.raises(OrchestrationError, match="must supersede"):
        store.write("result", repeated)


def test_supersedes_uses_real_instants_not_timestamp_text_order(
    tmp_path, workflow_fixtures
):
    store = OrchestrationStore(tmp_path / "orchestration")
    first = copy.deepcopy(workflow_fixtures["orchestration-result"])
    first["created_at"] = "2026-07-11T11:00:00+02:00"
    store.write("result", first)

    later = copy.deepcopy(first)
    later["result_id"] = "build-plan-result-two"
    later["created_at"] = "2026-07-11T09:30:00Z"
    later["supersedes"] = first["result_id"]
    store.write("result", later)

    branch = copy.deepcopy(later)
    branch["result_id"] = "build-plan-result-branch"
    branch["created_at"] = "2026-07-11T09:45:00Z"
    with pytest.raises(OrchestrationError, match="superseded only once"):
        store.write("result", branch)


def test_store_rejects_symlinked_root_and_intermediate_directory(
    tmp_path, workflow_fixtures
):
    real = tmp_path / "real"
    real.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real, target_is_directory=True)
    with pytest.raises(OrchestrationError, match="contains a symlink"):
        OrchestrationStore(linked_root).write(
            "run", workflow_fixtures["orchestration-run"]
        )

    root = tmp_path / "root"
    root.mkdir()
    (root / "tasks").symlink_to(real, target_is_directory=True)
    with pytest.raises(OrchestrationError, match="contains a symlink"):
        OrchestrationStore(root).write("task", workflow_fixtures["orchestration-task"])


def test_artifact_only_gate_passes_and_fails_from_latest_packets(workflow_fixtures):
    run = workflow_fixtures["orchestration-run"]
    task = workflow_fixtures["orchestration-task"]
    result = workflow_fixtures["orchestration-result"]
    passing = evaluate_artifact_gate(
        run,
        [task],
        [result],
        gate_id="planning-gate-generated",
        stage="planning",
        required_task_ids=["build-plan"],
        evaluated_at="2026-07-11T10:12:00Z",
    )
    assert passing["decision"] == "pass"
    assert passing["evaluated_result_ids"] == ["build-plan-result-one"]

    failing = evaluate_artifact_gate(
        run,
        [task],
        [],
        gate_id="planning-gate-missing",
        stage="planning",
        required_task_ids=["build-plan"],
        evaluated_at="2026-07-11T10:12:00Z",
    )
    assert failing["decision"] == "fail"
    assert "required result packet is missing" in failing["blockers"][0]


def test_artifact_gate_rejects_cross_run_or_role_mismatch(workflow_fixtures):
    run = workflow_fixtures["orchestration-run"]
    task = workflow_fixtures["orchestration-task"]
    result = copy.deepcopy(workflow_fixtures["orchestration-result"])
    result["role"] = "different-role"
    with pytest.raises(OrchestrationError, match="declared task and role"):
        evaluate_artifact_gate(
            run,
            [task],
            [result],
            gate_id="bad-gate",
            stage="planning",
            required_task_ids=["build-plan"],
            evaluated_at="2026-07-11T10:12:00Z",
        )
