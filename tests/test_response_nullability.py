from wavix_mcp.server import _relax_response_nullability


def _make_response_spec(schema: dict, content_type: str = "application/json") -> dict:
    return {
        "paths": {
            "/x": {"get": {"responses": {"200": {"content": {content_type: {"schema": schema}}}}}}
        }
    }


def _schema(spec: dict) -> dict:
    return spec["paths"]["/x"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_scalar_property_becomes_nullable():
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    _relax_response_nullability(_make_response_spec(schema))
    assert schema["properties"]["label"]["type"] == ["string", "null"]


def test_object_root_stays_object():
    # Must not become ["object","null"] or FastMCP re-wraps the output in {"result": ...}.
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    _relax_response_nullability(_make_response_spec(schema))
    assert schema["type"] == "object"
    assert schema["properties"]["n"]["type"] == ["integer", "null"]


def test_nested_and_array_items_widened():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"charge": {"type": "string"}}},
            },
            "pagination": {"type": "object", "properties": {"total": {"type": "integer"}}},
        },
    }
    _relax_response_nullability(_make_response_spec(schema))
    items = schema["properties"]["items"]
    assert items["type"] == ["array", "null"]
    assert items["items"]["type"] == "object"  # nested object stays strict
    assert items["items"]["properties"]["charge"]["type"] == ["string", "null"]
    assert schema["properties"]["pagination"]["properties"]["total"]["type"] == ["integer", "null"]


def test_enum_gains_null_member():
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["active", "pending"]}},
    }
    _relax_response_nullability(_make_response_spec(schema))
    status = schema["properties"]["status"]
    assert status["type"] == ["string", "null"]
    assert None in status["enum"]


def test_already_nullable_is_untouched():
    schema = {"type": "object", "properties": {"callerid": {"type": ["string", "null"]}}}
    _relax_response_nullability(_make_response_spec(schema))
    assert schema["properties"]["callerid"]["type"] == ["string", "null"]


def test_allof_member_widened():
    schema = {"allOf": [{"type": "object", "properties": {"label": {"type": "string"}}}]}
    _relax_response_nullability(_make_response_spec(schema))
    assert schema["allOf"][0]["properties"]["label"]["type"] == ["string", "null"]


def test_skips_non_json_content():
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    _relax_response_nullability(_make_response_spec(schema, content_type="application/x-ndjson"))
    assert schema["properties"]["label"]["type"] == "string"


def test_idempotent():
    schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["a"]}}}
    spec = _make_response_spec(schema)
    _relax_response_nullability(spec)
    snapshot = _schema(spec)
    _relax_response_nullability(spec)
    assert _schema(spec) == snapshot


def test_handles_malformed_specs():
    for spec in [
        {},
        {"paths": None},
        {"paths": {"/x": None}},
        {"paths": {"/x": {"get": None}}},
        {"paths": {"/x": {"get": {"responses": None}}}},
        {"paths": {"/x": {"get": {"responses": {"200": None}}}}},
        {"paths": {"/x": {"get": {"responses": {"200": {"content": None}}}}}},
        {"paths": {"/x": {"get": {"responses": {"200": {"content": {"application/json": None}}}}}}},
        {
            "paths": {
                "/x": {
                    "get": {
                        "responses": {"200": {"content": {"application/json": {"schema": None}}}}
                    }
                }
            }
        },
    ]:
        assert _relax_response_nullability(spec) is spec
