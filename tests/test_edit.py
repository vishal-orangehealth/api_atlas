"""Unit tests for formatting-preserving edits to Postman collection/environment files."""
import json

from atlas.project.edit import add_folder_to_collection, add_missing_env_vars, detect_indent

STANDARD_COLLECTION = """{
  "info": {
    "name": "Shop"
  },
  "item": [
    {
      "name": "Existing",
      "request": {
        "method": "GET",
        "url": "{{baseUrl}}/x"
      }
    }
  ],
  "variable": [
    {
      "key": "baseUrl",
      "value": "https://x.co"
    }
  ]
}
"""

TAB_INDENTED_COLLECTION = '{\n\t"info": {\n\t\t"name": "Shop"\n\t},\n\t"item": []\n}\n'


def test_detect_indent_two_space():
    assert detect_indent(STANDARD_COLLECTION) == 2


def test_detect_indent_tabs():
    assert detect_indent(TAB_INDENTED_COLLECTION) == "\t"


def test_add_folder_appends_and_preserves_existing_formatting(tmp_path):
    p = tmp_path / "shop.postman_collection.json"
    p.write_text(STANDARD_COLLECTION)
    new_folder = {"name": "New flow", "item": [{"name": "GET /y", "request": {"method": "GET", "url": "{{baseUrl}}/y"}}]}
    result = add_folder_to_collection(p, new_folder, extra_variables=[{"key": "atlas_token", "value": ""}])

    data = json.loads(result["new_text"])
    assert [it["name"] for it in data["item"]] == ["Existing", "New flow"]
    assert {v["key"] for v in data["variable"]} == {"baseUrl", "atlas_token"}
    # the original folder's own formatting (key order, nesting) is byte-identical in the new text
    assert '"name": "Existing"' in result["new_text"]
    assert not result["unrelated_changes"]
    assert result["added_lines"] > 0


def test_add_folder_skips_existing_variable_keys(tmp_path):
    p = tmp_path / "shop.postman_collection.json"
    p.write_text(STANDARD_COLLECTION)
    result = add_folder_to_collection(p, {"name": "f", "item": []}, extra_variables=[{"key": "baseUrl", "value": "ignored"}])
    data = json.loads(result["new_text"])
    base_url_entries = [v for v in data["variable"] if v["key"] == "baseUrl"]
    assert len(base_url_entries) == 1
    assert base_url_entries[0]["value"] == "https://x.co"  # the team's own value, untouched


def test_round_trip_is_clean_for_a_standard_export(tmp_path):
    """A file shaped like our own generator's output round-trips with only the append as a diff."""
    p = tmp_path / "shop.postman_collection.json"
    p.write_text(STANDARD_COLLECTION)
    result = add_folder_to_collection(p, {"name": "f", "item": []})
    assert result["removed_lines"] <= 2
    assert not result["unrelated_changes"]


def test_round_trip_warns_when_formatting_does_not_match(tmp_path):
    """4-space, hand-edited file: re-serializing at the detected indent still reformats lines
    we didn't intend to touch (e.g. inconsistent spacing), and that must surface as a warning."""
    messy = '{\n  "info": {"name": "Shop"},\n    "item": [\n        {"name":"Existing","request":{"method":"GET","url":"{{baseUrl}}/x"}}\n    ]\n}\n'
    p = tmp_path / "shop.postman_collection.json"
    p.write_text(messy)
    result = add_folder_to_collection(p, {"name": "f", "item": []})
    assert result["unrelated_changes"] is True
    assert result["removed_lines"] > 2


def test_preserves_no_trailing_newline(tmp_path):
    p = tmp_path / "shop.postman_collection.json"
    p.write_text(STANDARD_COLLECTION.rstrip("\n"))  # no trailing newline
    result = add_folder_to_collection(p, {"name": "f", "item": []})
    assert not result["new_text"].endswith("\n")


def test_add_missing_env_vars_only_adds_whats_missing(tmp_path):
    env = '{\n  "name": "Shop env",\n  "values": [\n    {\n      "key": "baseUrl",\n      "value": "https://x.co"\n    }\n  ]\n}\n'
    p = tmp_path / "shop.postman_environment.json"
    p.write_text(env)
    result = add_missing_env_vars(p, ["baseUrl", "ATLAS_PASSWORD", "ATLAS_EMAIL"])
    assert result["added_vars"] == ["ATLAS_PASSWORD", "ATLAS_EMAIL"]
    data = json.loads(result["new_text"])
    added = {v["key"]: v["value"] for v in data["values"] if v["key"] != "baseUrl"}
    assert added == {"ATLAS_PASSWORD": "", "ATLAS_EMAIL": ""}
    assert not result["unrelated_changes"]
