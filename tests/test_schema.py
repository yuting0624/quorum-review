import pytest

from src import schema


def test_parse_json_object_handles_a_fenced_payload():
    raw = '```json\n{"findings": []}\n```'
    assert schema.parse_json_object(raw) == {"findings": []}


def test_parse_json_object_handles_a_preamble():
    raw = 'Here is the result:\n{"findings": [], "note": "x"}\nThanks!'
    assert schema.parse_json_object(raw)["findings"] == []


def test_parse_json_object_repairs_a_stray_backslash():
    raw = r'{"findings": [], "path": "C:\Users\x"}'
    assert schema.parse_json_object(raw)["path"] == r"C:\Users\x"


def test_parse_json_object_raises_rather_than_returning_empty():
    """An unparseable response is a failure, not 'no findings found'."""
    with pytest.raises(ValueError):
        schema.parse_json_object("the model said no")
    with pytest.raises(ValueError):
        schema.parse_json_object("")


def test_findings_from_payload_drops_off_schema_entries():
    payload = {
        "findings": [
            {
                "file_path": "a.py",
                "line": 3,
                "category": "security",
                "severity": "high",
                "title": "t",
                "body": "b",
                "code_snippet": "s",
            },
            {"file_path": "b.py"},  # missing keys
            {
                "file_path": "c.py",
                "line": 1,
                "category": "vibes",  # not in the enum
                "severity": "high",
                "title": "t",
                "body": "b",
                "code_snippet": "s",
            },
            {
                "file_path": "d.py",
                "line": 1,
                "category": "security",
                "severity": "apocalyptic",  # not in the enum
                "title": "t",
                "body": "b",
                "code_snippet": "s",
            },
            "not a dict",
        ]
    }
    findings = schema.findings_from_payload(payload, "test-model")
    assert [f.file_path for f in findings] == ["a.py"]
    assert findings[0].primary_model == "test-model"


def test_findings_from_payload_tolerates_a_missing_key():
    assert schema.findings_from_payload({}, "m") == []
    assert schema.findings_from_payload({"findings": "nope"}, "m") == []


def test_verdict_falls_back_to_uncertain():
    """A malformed verdict must not read as 'confirmed' and get posted."""
    assert schema.verdict_from_payload({}, "m").verdict == "uncertain"
    assert schema.verdict_from_payload({"verdict": "yes"}, "m").verdict == "uncertain"
    assert schema.verdict_from_payload({"verdict": "REFUTED"}, "m").verdict == "refuted"


def test_for_gemini_strips_additional_properties_recursively():
    reduced = schema.for_gemini(schema.FINDINGS_SCHEMA)
    assert "additionalProperties" not in reduced
    assert "additionalProperties" not in reduced["properties"]["findings"]["items"]
    # The parts Gemini does understand must survive.
    assert reduced["properties"]["findings"]["items"]["required"]
    assert "additionalProperties" in schema.FINDINGS_SCHEMA  # original untouched


def test_language_directive_is_empty_for_english():
    assert schema.language_directive("") == ""
    assert schema.language_directive("English") == ""
    assert "Japanese" in schema.language_directive("Japanese")
