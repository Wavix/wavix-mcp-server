from copy import deepcopy

from wavix_mcp.server import _normalize_tool_schemas


def _response_spec(schema: dict) -> dict:
    return {
        "paths": {
            "/x": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}
            }
        }
    }


def _request_spec(schema: dict) -> dict:
    return {
        "paths": {
            "/x": {"post": {"requestBody": {"content": {"application/json": {"schema": schema}}}}}
        }
    }


def _param_spec(schema: dict) -> dict:
    return {
        "paths": {"/x": {"get": {"parameters": [{"name": "q", "in": "query", "schema": schema}]}}}
    }


def test_nullable_type_array_becomes_anyof():
    schema = {"type": "object", "properties": {"label": {"type": ["string", "null"]}}}
    _normalize_tool_schemas(_response_spec(schema))
    label = schema["properties"]["label"]
    assert "type" not in label
    assert label["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_multi_type_union_keeps_every_concrete_branch():
    schema = {"type": "object", "properties": {"v": {"type": ["string", "integer", "null"]}}}
    _normalize_tool_schemas(_response_spec(schema))
    assert schema["properties"]["v"]["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
        {"type": "null"},
    ]


def test_request_body_and_parameter_schemas_normalized():
    body = {"type": "object", "properties": {"note": {"type": ["string", "null"]}}}
    _normalize_tool_schemas(_request_spec(body))
    assert body["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]

    param = {"type": ["integer", "null"]}
    _normalize_tool_schemas(_param_spec(param))
    assert param["anyOf"] == [{"type": "integer"}, {"type": "null"}]


def test_single_type_and_object_untouched():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    _normalize_tool_schemas(_response_spec(schema))
    assert schema["type"] == "object"
    assert schema["properties"]["name"] == {"type": "string"}


def test_existing_anyof_not_double_wrapped():
    node = {"type": ["string", "null"], "anyOf": [{"type": "string"}]}
    _normalize_tool_schemas(_response_spec({"type": "object", "properties": {"x": node}}))
    assert node["type"] == ["string", "null"]
    assert node["anyOf"] == [{"type": "string"}]


def test_ref_sibling_allof_flattened_keeping_validation_and_annotations():
    status = {
        "allOf": [
            {
                "type": ["string", "null"],
                "enum": ["active", "paused", None],
                "title": "Status",
                "description": "Detailed enum docs.",
            },
            {"description": "Short override.", "example": "active"},
        ]
    }
    _normalize_tool_schemas(_response_spec({"type": "object", "properties": {"status": status}}))
    assert "allOf" not in status
    assert status["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert status["enum"] == ["active", "paused", None]
    assert status["description"] == "Detailed enum docs."  # validation member wins
    assert status["example"] == "active"  # annotation-only extra survives


def test_empty_allof_member_dropped():
    node = {"allOf": [{"type": "string"}, {}]}
    _normalize_tool_schemas(_response_spec({"type": "object", "properties": {"x": node}}))
    assert "allOf" not in node
    assert node["type"] == "string"


def test_allof_not_flattened_when_key_would_collide():
    node = {
        "type": "object",
        "required": ["a"],
        "allOf": [{"type": "object", "required": ["b"]}],
    }
    _normalize_tool_schemas(_response_spec({"type": "object", "properties": {"x": node}}))
    assert node["allOf"] == [{"type": "object", "required": ["b"]}]
    assert node["required"] == ["a"]


def test_path_level_parameter_normalized():
    schema = {"type": ["string", "null"]}
    spec = {
        "paths": {
            "/x": {
                "parameters": [{"name": "id", "in": "path", "schema": schema}],
                "get": {"responses": {}},
            }
        }
    }
    _normalize_tool_schemas(spec)
    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_genuine_multi_constraint_allof_left_intact():
    node = {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ]
    }
    _normalize_tool_schemas(_response_spec({"type": "object", "properties": {"x": node}}))
    assert len(node["allOf"]) == 2


def test_idempotent():
    schema = {
        "type": "object",
        "properties": {
            "label": {"type": ["string", "null"]},
            "status": {
                "allOf": [{"type": ["string", "null"], "enum": ["a", None]}, {"example": "a"}]
            },
        },
    }
    spec = _response_spec(schema)
    _normalize_tool_schemas(spec)
    snapshot = deepcopy(schema)
    _normalize_tool_schemas(spec)
    assert schema == snapshot


def test_handles_malformed_specs():
    for spec in [
        {},
        {"paths": None},
        {"paths": {"/x": None}},
        {"paths": {"/x": {"get": None}}},
        {"paths": {"/x": {"get": {"parameters": None}}}},
        {"paths": {"/x": {"post": {"requestBody": {"content": None}}}}},
        {"paths": {"/x": {"get": {"responses": {"200": {"content": {"application/json": None}}}}}}},
    ]:
        assert _normalize_tool_schemas(spec) is spec
