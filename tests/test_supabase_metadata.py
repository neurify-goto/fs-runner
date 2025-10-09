from src.shared.supabase.metadata import merge_metadata


def test_merge_metadata_skips_none_top_level():
    base = {"status": "running", "batch": {"attempt": 1}}
    patch = {"status": None, "batch": None, "new_field": 42}

    merged = merge_metadata(base, patch)

    assert merged["status"] == "running"
    assert merged["batch"] == {"attempt": 1}
    assert merged["new_field"] == 42


def test_merge_metadata_skips_none_nested_values():
    base = {"batch": {"attempt": 3, "current_task_index": 7}}
    patch = {"batch": {"attempt": None, "current_task_index": 9, "preempted": None}}

    merged = merge_metadata(base, patch)

    assert merged["batch"]["attempt"] == 3
    assert merged["batch"]["current_task_index"] == 9
    assert "preempted" not in merged["batch"]


def test_merge_metadata_does_not_mutate_inputs():
    base = {"batch": {"attempt": 1}}
    patch = {"batch": {"attempt": 2}}

    merged = merge_metadata(base, patch)

    assert merged["batch"]["attempt"] == 2
    assert base["batch"]["attempt"] == 1
