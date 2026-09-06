"""Tests for uds_local/datastore.py (the board-keyed interop store).

All isolated to a tmp file -- nothing touches the real odin_data.json.
"""
from uds_local.datastore import DataStore, json_safe


class TestJsonSafe:
    def test_bytes_to_hex_recursive(self):
        out = json_safe({"blob": b"\xff\xff",
                         "n": {"table": b"\xfe\x85", "v": 1},
                         "list": [b"\x00", 2]})
        assert out == {"blob": "ffff", "n": {"table": "fe85", "v": 1},
                       "list": ["00", 2]}

    def test_passthrough(self):
        assert json_safe({"a": 1, "b": "x", "c": [1, 2]}) == {"a": 1, "b": "x", "c": [1, 2]}


def _store(tmp_path):
    return DataStore(tmp_path / "odin_data.json")


class TestDataStore:
    def test_put_get_roundtrip_and_persist(self, tmp_path):
        s = _store(tmp_path)
        s.put("SN123", "outputs", "DRIVE_UNIT_ODOMETER", 4)
        s.update("SN123", "outputs", {"odin_output": "DI rotations: 4"})
        assert s.get("SN123", "outputs") == {
            "DRIVE_UNIT_ODOMETER": 4, "odin_output": "DI rotations: 4"}
        # a fresh DataStore on the same file sees the persisted data
        assert DataStore(tmp_path / "odin_data.json").get("SN123", "outputs") == {
            "DRIVE_UNIT_ODOMETER": 4, "odin_output": "DI rotations: 4"}

    def test_missing_returns_empty(self, tmp_path):
        s = _store(tmp_path)
        assert s.get("nope", "outputs") == {} and s.has("nope", "outputs") is False
        assert s.boards() == []

    def test_namespaces_are_independent(self, tmp_path):
        s = _store(tmp_path)
        s.update("SN1", "immo", {"key": "aa"})
        s.update("SN1", "outputs", {"x": 1})
        assert s.get("SN1", "immo") == {"key": "aa"}
        assert s.get("SN1", "outputs") == {"x": 1}
        assert s.boards() == ["SN1"]

    def test_update_replace(self, tmp_path):
        s = _store(tmp_path)
        s.update("SN1", "outputs", {"a": 1, "b": 2})
        s.update("SN1", "outputs", {"c": 3}, replace=True)
        assert s.get("SN1", "outputs") == {"c": 3}

    def test_get_returns_copy(self, tmp_path):
        s = _store(tmp_path)
        s.update("SN1", "outputs", {"a": 1})
        s.get("SN1", "outputs")["a"] = 999   # mutating the copy must not persist
        assert s.get("SN1", "outputs") == {"a": 1}

    def test_corrupt_file_loads_empty(self, tmp_path):
        p = tmp_path / "odin_data.json"
        p.write_text("{ not json")
        assert DataStore(p).boards() == []
