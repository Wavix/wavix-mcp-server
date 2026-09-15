from wavix_mcp.server import _relax_require_docs_items


def _make_response_spec(schema: dict, content_type: str = "application/json") -> dict:
    return {
        "paths": {
            "/x": {"get": {"responses": {"200": {"content": {content_type: {"schema": schema}}}}}}
        }
    }


def _schema(spec: dict) -> dict:
    return spec["paths"]["/x"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_integer_items_gain_string():
    schema = {
        "type": "object",
        "properties": {"require_docs": {"type": "array", "items": {"type": "integer"}}},
    }
    _relax_require_docs_items(_make_response_spec(schema))
    assert schema["properties"]["require_docs"]["items"]["type"] == ["integer", "string"]


def test_nullable_items_gain_string():
    schema = {
        "type": "object",
        "properties": {"require_docs": {"type": "array", "items": {"type": ["integer", "null"]}}},
    }
    _relax_require_docs_items(_make_response_spec(schema))
    assert schema["properties"]["require_docs"]["items"]["type"] == ["integer", "null", "string"]


def test_string_items_untouched():
    schema = {
        "type": "object",
        "properties": {"require_docs": {"type": "array", "items": {"type": "string"}}},
    }
    _relax_require_docs_items(_make_response_spec(schema))
    assert schema["properties"]["require_docs"]["items"]["type"] == "string"


def test_widened_inside_array_of_objects():
    inner = {
        "type": "object",
        "properties": {"require_docs": {"type": "array", "items": {"type": "integer"}}},
    }
    schema = {"type": "object", "properties": {"dids": {"type": "array", "items": inner}}}
    _relax_require_docs_items(_make_response_spec(schema))
    assert inner["properties"]["require_docs"]["items"]["type"] == ["integer", "string"]


def test_untyped_items_untouched():
    items = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    schema = {"type": "object", "properties": {"require_docs": {"type": "array", "items": items}}}
    _relax_require_docs_items(_make_response_spec(schema))
    assert schema["properties"]["require_docs"]["items"] == items


def test_other_arrays_untouched():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "integer"}}},
    }
    _relax_require_docs_items(_make_response_spec(schema))
    assert schema["properties"]["tags"]["items"]["type"] == "integer"


def test_skips_non_json_content():
    schema = {
        "type": "object",
        "properties": {"require_docs": {"type": "array", "items": {"type": "integer"}}},
    }
    _relax_require_docs_items(_make_response_spec(schema, content_type="application/x-ndjson"))
    assert schema["properties"]["require_docs"]["items"]["type"] == "integer"


def test_idempotent():
    schema = {
        "type": "object",
        "properties": {"require_docs": {"type": "array", "items": {"type": "integer"}}},
    }
    spec = _make_response_spec(schema)
    _relax_require_docs_items(spec)
    snapshot = _schema(spec)
    _relax_require_docs_items(spec)
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
        assert _relax_require_docs_items(spec) is spec
