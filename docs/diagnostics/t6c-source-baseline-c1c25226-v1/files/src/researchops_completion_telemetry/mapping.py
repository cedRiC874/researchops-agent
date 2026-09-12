from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
CompletionMappingResult = tuple[str, str, JsonValue, str]


_MISSING = object()
_RULE_FIELDS = frozenset(
    {
        "rule_id",
        "precedence_stage",
        "condition",
        "normalized_completion_state",
        "truncation_signal_source",
    }
)
_OPTIONAL_RULE_FIELDS = frozenset(
    {
        "evidence_fixture_id",
        "preserve_native_value",
        "preserved_native_value_field",
        "preserved_native_value_utf8_bytes_max",
        "provider_response_completed_only",
        "terminal_event_write_allowed",
    }
)
_RULE_HINT_FIELDS = _RULE_FIELDS - {"condition"}
_CONDITION_FIELDS = {
    "all": frozenset({"op", "conditions"}),
    "not": frozenset({"op", "condition"}),
    "equals": frozenset({"op", "field", "value"}),
    "all_missing": frozenset({"op", "fields"}),
    "is_null": frozenset({"op", "field"}),
    "is_present_non_null": frozenset({"op", "field"}),
    "json_type_is": frozenset({"op", "field", "value"}),
    "default_unknown": frozenset({"op", "field", "known_values"}),
}
_JSON_TYPE_NAMES = frozenset({"array", "boolean", "null", "number", "object", "string"})
_TRUNCATION_EXCLUSION_POLICY_FIELDS = frozenset(
    {
        "allowed_only_if_every_response_truncation_signal_source_equals",
        "any_other_source_forbids_claim",
        "allowed_only_if_every_response_normalized_completion_state_in",
        "ineligible_normalized_completion_states",
        "unmapped_forbids_claim",
        "unmapped_rationale",
        "minimum_response_count",
        "empty_response_set_forbids_claim",
        "runtime_binding",
    }
)


class CompletionMappingError(ValueError):
    """Raised when a completion mapping contract cannot be interpreted safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _contract_error(code: str, message: str) -> CompletionMappingError:
    return CompletionMappingError(code, message)


def _as_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_error("mapping_contract_invalid", f"{label} must be an object")
    return value


def _as_sequence(value: object, *, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _contract_error("mapping_contract_invalid", f"{label} must be an array")
    return value


def _validate_dotted_path(field: object, *, label: str) -> str:
    if (
        not isinstance(field, str)
        or not field
        or any(not part for part in field.split("."))
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            f"{label} must be a dotted path",
        )
    return field


def _lookup_path(projection: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    field = _validate_dotted_path(field, label="condition field")
    current: object = projection
    for part in field.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, _MISSING
        current = current[part]
    return True, current


def _json_values_equal(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _validate_condition(
    condition_value: object,
    declared_operators: frozenset[str],
) -> None:
    condition = _as_mapping(condition_value, label="condition")
    operator = condition.get("op")
    if not isinstance(operator, str) or operator not in declared_operators:
        raise _contract_error(
            "mapping_operator_unsupported",
            f"condition operator is not declared: {operator!r}",
        )
    expected_fields = _CONDITION_FIELDS.get(operator)
    if expected_fields is None:
        raise _contract_error(
            "mapping_operator_unsupported",
            f"condition operator has no interpreter: {operator}",
        )
    if set(condition) != expected_fields:
        raise _contract_error(
            "mapping_condition_shape_invalid",
            f"condition {operator!r} must contain exactly {sorted(expected_fields)}",
        )

    if operator == "all":
        children = _as_sequence(condition["conditions"], label="condition.conditions")
        if not children:
            raise _contract_error("mapping_contract_invalid", "all requires at least one condition")
        for child in children:
            _validate_condition(child, declared_operators)
        return

    if operator == "not":
        _validate_condition(condition["condition"], declared_operators)
        return

    if operator in {
        "equals",
        "is_null",
        "is_present_non_null",
        "json_type_is",
        "default_unknown",
    }:
        _validate_dotted_path(condition["field"], label="condition field")

    if operator == "all_missing":
        fields = _as_sequence(condition["fields"], label="condition.fields")
        if not fields:
            raise _contract_error(
                "mapping_contract_invalid",
                "all_missing requires at least one dotted string field",
            )
        for field in fields:
            _validate_dotted_path(field, label="all_missing field")

    if operator == "default_unknown":
        _as_sequence(condition["known_values"], label="condition.known_values")
    if operator == "json_type_is":
        json_type = condition["value"]
        if not isinstance(json_type, str) or json_type not in _JSON_TYPE_NAMES:
            raise _contract_error(
                "mapping_contract_invalid",
                "json_type_is value is not a supported JSON type",
            )


def _json_type_name(value: object) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "array"
    return None


def _condition_matches(
    condition_value: object,
    projection: Mapping[str, Any],
    declared_operators: frozenset[str],
) -> bool:
    condition = _as_mapping(condition_value, label="condition")
    operator = condition["op"]

    if operator == "all":
        return all(
            _condition_matches(child, projection, declared_operators)
            for child in condition["conditions"]
        )

    if operator == "not":
        return not _condition_matches(
            condition["condition"], projection, declared_operators
        )

    if operator == "equals":
        present, actual = _lookup_path(projection, condition["field"])
        return present and _json_values_equal(actual, condition["value"])

    if operator == "all_missing":
        return all(not _lookup_path(projection, field)[0] for field in condition["fields"])

    if operator == "is_null":
        present, actual = _lookup_path(projection, condition["field"])
        return present and actual is None

    if operator == "is_present_non_null":
        present, actual = _lookup_path(projection, condition["field"])
        return present and actual is not None

    if operator == "json_type_is":
        present, actual = _lookup_path(projection, condition["field"])
        return present and _json_type_name(actual) == condition["value"]

    if operator == "default_unknown":
        present, actual = _lookup_path(projection, condition["field"])
        known_values = condition["known_values"]
        return present and actual is not None and not any(
            _json_values_equal(actual, known) for known in known_values
        )

    raise AssertionError("unreachable condition operator")


def _collect_rules(provider_mapping: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    rules: list[Mapping[str, Any]] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if "condition" in value or any(field in value for field in _RULE_HINT_FIELDS):
                if not _RULE_FIELDS.issubset(value):
                    raise _contract_error(
                        "mapping_rule_shape_invalid",
                        "a rule-like object is missing required rule fields",
                    )
                unknown_fields = set(value) - _RULE_FIELDS - _OPTIONAL_RULE_FIELDS
                if unknown_fields:
                    raise _contract_error(
                        "mapping_rule_shape_invalid",
                        f"rule contains unsupported fields: {sorted(unknown_fields)}",
                    )
            if _RULE_FIELDS.issubset(value):
                rules.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for child in value:
                visit(child)

    visit(provider_mapping)
    return tuple(rules)


def _validate_rule_metadata(rule: Mapping[str, Any]) -> None:
    for field in (
        "provider_response_completed_only",
        "terminal_event_write_allowed",
    ):
        if field in rule and not isinstance(rule[field], bool):
            raise _contract_error(
                "mapping_rule_shape_invalid",
                f"rule field {field} must be boolean",
            )
    if "evidence_fixture_id" in rule and (
        not isinstance(rule["evidence_fixture_id"], str)
        or not rule["evidence_fixture_id"]
    ):
        raise _contract_error(
            "mapping_rule_shape_invalid",
            "evidence_fixture_id must be a non-empty string",
        )

    preserve = rule.get("preserve_native_value", False)
    if not isinstance(preserve, bool):
        raise _contract_error(
            "mapping_rule_shape_invalid",
            "preserve_native_value must be boolean",
        )
    preservation_fields = {
        "preserved_native_value_field",
        "preserved_native_value_utf8_bytes_max",
    }
    if not preserve and any(field in rule for field in preservation_fields):
        raise _contract_error(
            "mapping_rule_shape_invalid",
            "non-preserving rules cannot declare preservation metadata",
        )
    if preserve:
        field = rule.get("preserved_native_value_field")
        byte_limit = rule.get("preserved_native_value_utf8_bytes_max")
        if not isinstance(field, str) or not field:
            raise _contract_error(
                "mapping_rule_shape_invalid",
                "a preserving rule needs preserved_native_value_field",
            )
        _validate_dotted_path(field, label="preserved_native_value_field")
        if (
            isinstance(byte_limit, bool)
            or not isinstance(byte_limit, int)
            or byte_limit < 1
        ):
            raise _contract_error(
                "mapping_rule_shape_invalid",
                "a preserving rule needs a positive UTF-8 byte limit",
            )


def _preserved_native_value(
    rule: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> JsonValue:
    if not rule.get("preserve_native_value", False):
        return None
    field = rule.get("preserved_native_value_field")
    if not isinstance(field, str):
        raise _contract_error(
            "mapping_contract_invalid",
            "a preserving rule must declare preserved_native_value_field",
        )
    present, value = _lookup_path(projection, field)
    if not present:
        raise _contract_error(
            "mapping_contract_invalid",
            "the preserved native value field is missing",
        )
    byte_limit = rule.get("preserved_native_value_utf8_bytes_max")
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit < 1:
        raise _contract_error(
            "mapping_contract_invalid",
            "a preserving rule must declare a positive UTF-8 byte limit",
        )
    if not isinstance(value, str):
        raise _contract_error(
            "mapping_preserved_native_value_type_invalid",
            "a preserved native value must be a string",
        )
    try:
        encoded_value = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _contract_error(
            "mapping_preserved_native_value_encoding_invalid",
            "the preserved native value is not valid UTF-8",
        ) from exc
    if len(encoded_value) > byte_limit:
        raise _contract_error(
            "mapping_preserved_native_value_over_limit",
            "the preserved native value exceeds its UTF-8 byte limit",
        )
    return copy.deepcopy(value)


def map_completion(
    response_projection: Mapping[str, Any],
    provider_id: str,
    mapping: Mapping[str, Any],
) -> CompletionMappingResult:
    """Interpret a JSON completion mapping without Provider-specific code or I/O."""

    projection = _as_mapping(response_projection, label="response_projection")
    mapping_object = _as_mapping(mapping, label="mapping")
    if mapping_object.get("same_stage_multiple_match") != "error":
        raise _contract_error(
            "mapping_contract_invalid",
            "same_stage_multiple_match must fail closed with error",
        )
    if mapping_object.get("no_rule_match") != "error":
        raise _contract_error(
            "mapping_contract_invalid",
            "no_rule_match must fail closed with error",
        )
    providers = _as_mapping(mapping_object.get("providers"), label="mapping.providers")
    if not isinstance(provider_id, str) or not provider_id:
        raise _contract_error(
            "mapping_provider_unknown",
            "provider_id must be a non-empty string",
        )
    provider_mapping = providers.get(provider_id)
    if not isinstance(provider_mapping, Mapping):
        raise _contract_error(
            "mapping_provider_unknown",
            f"provider is not declared in the mapping: {provider_id!r}",
        )

    precedence_values = _as_sequence(
        mapping_object.get("mapping_precedence"),
        label="mapping.mapping_precedence",
    )
    if not precedence_values or not all(
        isinstance(stage, str) and stage for stage in precedence_values
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "mapping_precedence must contain non-empty strings",
        )
    precedence = tuple(precedence_values)
    if len(set(precedence)) != len(precedence):
        raise _contract_error(
            "mapping_contract_invalid",
            "mapping_precedence stages must be unique",
        )

    operator_values = _as_sequence(
        mapping_object.get("condition_operators"),
        label="mapping.condition_operators",
    )
    if not all(isinstance(operator, str) and operator for operator in operator_values):
        raise _contract_error(
            "mapping_contract_invalid",
            "condition_operators must contain non-empty strings",
        )
    declared_operators = frozenset(operator_values)
    if (
        len(declared_operators) != len(operator_values)
        or declared_operators != frozenset(_CONDITION_FIELDS)
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "condition_operators must exactly match the interpreter operator set",
        )

    state_values = _as_sequence(
        mapping_object.get("normalized_completion_states"),
        label="mapping.normalized_completion_states",
    )
    source_values = _as_sequence(
        mapping_object.get("truncation_signal_sources"),
        label="mapping.truncation_signal_sources",
    )
    if not all(isinstance(state, str) and state for state in state_values):
        raise _contract_error(
            "mapping_contract_invalid",
            "normalized_completion_states must contain non-empty strings",
        )
    if not all(isinstance(source, str) and source for source in source_values):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation_signal_sources must contain non-empty strings",
        )
    declared_states = frozenset(state_values)
    declared_sources = frozenset(source_values)
    if len(declared_states) != len(state_values) or len(declared_sources) != len(
        source_values
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "normalized states and truncation sources must be unique",
        )

    rules_by_stage: dict[str, list[Mapping[str, Any]]] = {
        stage: [] for stage in precedence
    }
    rule_ids: set[str] = set()
    global_stage_counts = {stage: 0 for stage in precedence}
    for declared_provider_id, declared_provider_mapping in providers.items():
        if not isinstance(declared_provider_id, str) or not declared_provider_id:
            raise _contract_error(
                "mapping_contract_invalid",
                "every mapping provider ID must be a non-empty string",
            )
        provider_rules = _collect_rules(
            _as_mapping(
                declared_provider_mapping,
                label=f"mapping.providers.{declared_provider_id}",
            )
        )
        if not provider_rules:
            raise _contract_error(
                "mapping_contract_invalid",
                f"provider {declared_provider_id} has no executable rules",
            )
        for rule in provider_rules:
            rule_id = rule.get("rule_id")
            stage = rule.get("precedence_stage")
            state = rule.get("normalized_completion_state")
            source = rule.get("truncation_signal_source")
            if not isinstance(rule_id, str) or not rule_id:
                raise _contract_error(
                    "mapping_contract_invalid",
                    "every rule needs a rule_id",
                )
            if rule_id in rule_ids:
                raise _contract_error(
                    "mapping_rule_id_duplicate",
                    f"duplicate rule_id: {rule_id}",
                )
            rule_ids.add(rule_id)
            if not isinstance(stage, str) or stage not in rules_by_stage:
                raise _contract_error(
                    "mapping_contract_invalid",
                    f"rule {rule_id} has an undeclared precedence stage",
                )
            if (
                not isinstance(state, str)
                or not isinstance(source, str)
                or state not in declared_states
                or source not in declared_sources
            ):
                raise _contract_error(
                    "mapping_contract_invalid",
                    f"rule {rule_id} has an undeclared output",
                )
            _validate_rule_metadata(rule)
            _validate_condition(rule["condition"], declared_operators)
            global_stage_counts[stage] += 1
            if declared_provider_id == provider_id:
                rules_by_stage[stage].append(rule)

    stage_contract = _as_mapping(
        mapping_object.get("precedence_stage_contract"),
        label="mapping.precedence_stage_contract",
    )
    if set(stage_contract) != set(precedence):
        raise _contract_error(
            "mapping_contract_invalid",
            "precedence_stage_contract must cover every precedence stage exactly",
        )
    for stage in precedence:
        stage_policy = _as_mapping(
            stage_contract[stage],
            label=f"mapping.precedence_stage_contract.{stage}",
        )
        executable = stage_policy.get("executable_in_v1")
        materialized_count = stage_policy.get("materialized_rule_count")
        if stage_policy.get("selection") != "all_rules_whose_precedence_stage_matches":
            raise _contract_error(
                "mapping_contract_invalid",
                f"stage {stage} has an invalid selection policy",
            )
        if not isinstance(executable, bool):
            raise _contract_error(
                "mapping_contract_invalid",
                f"stage {stage} must declare executable_in_v1",
            )
        if (
            isinstance(materialized_count, bool)
            or not isinstance(materialized_count, int)
            or materialized_count != global_stage_counts[stage]
        ):
            raise _contract_error(
                "mapping_contract_invalid",
                f"stage {stage} materialized_rule_count does not match its rules",
            )
        if not executable and global_stage_counts[stage]:
            raise _contract_error(
                "mapping_contract_invalid",
                f"disabled stage {stage} contains executable rules",
            )
    total_count = mapping_object.get("materialized_provider_rule_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != len(rule_ids)
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "materialized_provider_rule_count does not match the rule universe",
        )

    matches_by_stage: dict[str, list[Mapping[str, Any]]] = {}
    for stage in precedence:
        matches = [
            rule
            for rule in rules_by_stage[stage]
            if _condition_matches(rule.get("condition"), projection, declared_operators)
        ]
        matches_by_stage[stage] = matches
        if len(matches) > 1:
            matched_ids = sorted(str(rule["rule_id"]) for rule in matches)
            raise _contract_error(
                "mapping_rule_ambiguous",
                f"multiple rules matched stage {stage}: {matched_ids}",
            )
    for stage in precedence:
        matches = matches_by_stage[stage]
        if matches:
            rule = matches[0]
            return (
                str(rule["normalized_completion_state"]),
                str(rule["truncation_signal_source"]),
                _preserved_native_value(rule, projection),
                str(rule["rule_id"]),
            )

    raise _contract_error(
        "mapping_rule_no_match",
        f"no rule matched provider {provider_id!r}",
    )


def may_claim_truncation_excluded(
    per_response_states_and_sources: Iterable[tuple[str, str]],
    mapping: Mapping[str, Any],
) -> bool:
    """Interpret the mapping's run-level truncation-exclusion policy."""

    mapping_object = _as_mapping(mapping, label="mapping")
    providers = _as_mapping(mapping_object.get("providers"), label="mapping.providers")
    if not providers:
        raise _contract_error(
            "mapping_contract_invalid",
            "mapping.providers must not be empty",
        )
    validation_provider_id = next(iter(providers))
    try:
        map_completion({}, validation_provider_id, mapping_object)
    except CompletionMappingError as exc:
        if exc.code != "mapping_rule_no_match":
            raise
    global_rules = _as_mapping(
        mapping_object.get("global_rules"),
        label="mapping.global_rules",
    )
    policy = _as_mapping(
        global_rules.get("additional_truncation_exclusion_claim"),
        label="mapping.global_rules.additional_truncation_exclusion_claim",
    )
    if set(policy) != _TRUNCATION_EXCLUSION_POLICY_FIELDS:
        raise _contract_error(
            "mapping_contract_invalid",
            "additional_truncation_exclusion_claim has an invalid field set",
        )
    state_values = _as_sequence(
        mapping_object.get("normalized_completion_states"),
        label="mapping.normalized_completion_states",
    )
    source_values = _as_sequence(
        mapping_object.get("truncation_signal_sources"),
        label="mapping.truncation_signal_sources",
    )
    if not all(isinstance(value, str) and value for value in state_values):
        raise _contract_error(
            "mapping_contract_invalid",
            "normalized completion states must be non-empty strings",
        )
    if not all(isinstance(value, str) and value for value in source_values):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation signal sources must be non-empty strings",
        )
    declared_states = frozenset(state_values)
    declared_sources = frozenset(source_values)
    allowed_values = _as_sequence(
        policy.get("allowed_only_if_every_response_normalized_completion_state_in"),
        label="additional_truncation_exclusion_claim.allowed states",
    )
    if not allowed_values or not all(
        isinstance(value, str) and value for value in allowed_values
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion allowed states must be non-empty strings",
        )
    allowed_states = frozenset(allowed_values)
    if len(allowed_states) != len(allowed_values) or not allowed_states <= declared_states:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion allowed states must be unique declared states",
        )
    ineligible_values = _as_sequence(
        policy["ineligible_normalized_completion_states"],
        label="additional_truncation_exclusion_claim.ineligible states",
    )
    if not ineligible_values or not all(
        isinstance(value, str) and value for value in ineligible_values
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion ineligible states must be non-empty strings",
        )
    ineligible_states = frozenset(ineligible_values)
    if (
        len(ineligible_states) != len(ineligible_values)
        or not ineligible_states <= declared_states
        or allowed_states & ineligible_states
        or allowed_states | ineligible_states != declared_states
        or "unmapped" not in ineligible_states
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion state partition is inconsistent",
        )
    required_source = policy.get(
        "allowed_only_if_every_response_truncation_signal_source_equals"
    )
    if not isinstance(required_source, str) or not required_source:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion requires one non-empty native source",
        )
    native_source = mapping_object.get("native_completion_signal_source")
    if (
        not isinstance(native_source, str)
        or native_source not in declared_sources
        or required_source != native_source
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion source must equal the declared native source",
        )
    minimum_count = policy.get("minimum_response_count")
    if isinstance(minimum_count, bool) or not isinstance(minimum_count, int) or minimum_count < 1:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion minimum_response_count must be a positive integer",
        )
    if policy.get("unmapped_forbids_claim") is not True:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion must explicitly forbid unmapped",
        )
    rationale = policy.get("unmapped_rationale")
    if not isinstance(rationale, str) or not rationale:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion unmapped rationale must be non-empty text",
        )
    if policy.get("any_other_source_forbids_claim") is not True:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion must explicitly forbid every other source",
        )
    if policy.get("empty_response_set_forbids_claim") is not True:
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion must explicitly forbid an empty response set",
        )
    if policy.get("runtime_binding") != (
        "researchops_completion_telemetry.mapping.may_claim_truncation_excluded"
    ):
        raise _contract_error(
            "mapping_contract_invalid",
            "truncation exclusion runtime binding is invalid",
        )

    if not isinstance(per_response_states_and_sources, Iterable) or isinstance(
        per_response_states_and_sources,
        (str, bytes, bytearray, Mapping),
    ):
        return False

    observed_count = 0
    for item in per_response_states_and_sources:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)):
            return False
        if len(item) != 2:
            return False
        state, source = item
        observed_count += 1
        if (
            not isinstance(state, str)
            or not isinstance(source, str)
            or state not in allowed_states
            or source != required_source
        ):
            return False
    return observed_count >= minimum_count


__all__ = [
    "CompletionMappingError",
    "CompletionMappingResult",
    "map_completion",
    "may_claim_truncation_excluded",
]
