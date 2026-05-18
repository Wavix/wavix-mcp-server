from wavix_mcp.server import (
    _ensure_request_body_type_object,
    _strip_non_json_response_content,
)


def _make_spec(schema: dict, content_type: str = "application/json") -> dict:
    return {
        "paths": {"/x": {"post": {"requestBody": {"content": {content_type: {"schema": schema}}}}}}
    }


def test_sets_type_object_when_allof_present():
    schema = {"allOf": [{"required": ["voice_campaign"], "properties": {"voice_campaign": {}}}]}
    spec = _make_spec(schema)
    _ensure_request_body_type_object(spec)
    assert schema["type"] == "object"
    assert "allOf" in schema  # FastMCP will merge later


def test_no_op_when_type_already_set():
    schema = {"type": "string", "allOf": [{"properties": {"x": {}}}]}
    _ensure_request_body_type_object(_make_spec(schema))
    assert schema["type"] == "string"


def test_no_op_when_no_allof():
    schema = {"properties": {"x": {"type": "string"}}}
    _ensure_request_body_type_object(_make_spec(schema))
    assert "type" not in schema


def test_skips_non_json_content_types():
    schema = {"allOf": [{"properties": {"file": {}}}]}
    _ensure_request_body_type_object(_make_spec(schema, content_type="multipart/form-data"))
    assert "type" not in schema


def test_idempotent():
    schema = {"allOf": [{"properties": {"x": {}}}]}
    spec = _make_spec(schema)
    _ensure_request_body_type_object(spec)
    snapshot = dict(schema)
    _ensure_request_body_type_object(spec)
    assert schema == snapshot


def test_handles_malformed_specs():
    for spec in [
        {},
        {"paths": None},
        {"paths": {"/x": None}},
        {"paths": {"/x": {"post": None}}},
        {"paths": {"/x": {"post": {"requestBody": None}}}},
        {"paths": {"/x": {"post": {"requestBody": {"content": None}}}}},
        {"paths": {"/x": {"post": {"requestBody": {"content": {"application/json": None}}}}}},
        {
            "paths": {
                "/x": {"post": {"requestBody": {"content": {"application/json": {"schema": None}}}}}
            }
        },
    ]:
        assert _ensure_request_body_type_object(spec) is spec


def _make_response_spec(content: dict) -> dict:
    return {"paths": {"/x": {"get": {"responses": {"200": {"content": content}}}}}}


def _get_response(spec: dict) -> dict:
    return spec["paths"]["/x"]["get"]["responses"]["200"]


def test_strips_ndjson_response_content():
    spec = _make_response_spec({"application/x-ndjson": {"schema": {"type": "string"}}})
    _strip_non_json_response_content(spec)
    assert "content" not in _get_response(spec)


def test_strips_binary_response_content():
    spec = _make_response_spec({"application/octet-stream": {"schema": {"type": "string"}}})
    _strip_non_json_response_content(spec)
    assert "content" not in _get_response(spec)


def test_keeps_json_response_content():
    spec = _make_response_spec({"application/json": {"schema": {"type": "object"}}})
    _strip_non_json_response_content(spec)
    assert "application/json" in _get_response(spec)["content"]


def test_keeps_only_json_when_mixed():
    spec = _make_response_spec(
        {
            "application/json": {"schema": {"type": "object"}},
            "application/x-ndjson": {"schema": {"type": "string"}},
        }
    )
    _strip_non_json_response_content(spec)
    content = _get_response(spec)["content"]
    assert "application/json" in content
    assert "application/x-ndjson" not in content


def test_response_strip_handles_malformed_specs():
    for spec in [
        {},
        {"paths": None},
        {"paths": {"/x": None}},
        {"paths": {"/x": {"get": None}}},
        {"paths": {"/x": {"get": {"responses": None}}}},
        {"paths": {"/x": {"get": {"responses": {"200": None}}}}},
        {"paths": {"/x": {"get": {"responses": {"200": {"content": None}}}}}},
    ]:
        assert _strip_non_json_response_content(spec) is spec
