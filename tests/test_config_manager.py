from config.manager import ConfigManager


def test_get_batch_env_aliases_from_file():
    manager = ConfigManager()
    aliases = manager.get_batch_env_aliases()
    assert "task_index" in aliases
    assert "attempt" in aliases
    assert "array_size" in aliases
    assert aliases["task_index"][0] == "BATCH_TASK_INDEX"


def test_get_batch_env_aliases_respects_overrides():
    manager = ConfigManager()
    manager._worker_config = {
        "batch_env_aliases": {
            "task_index": ["CUSTOM_BATCH_INDEX"],
            "attempt": ["CUSTOM_BATCH_ATTEMPT"],
        }
    }

    aliases = manager.get_batch_env_aliases()
    assert aliases["task_index"] == ["CUSTOM_BATCH_INDEX"]
    assert aliases["attempt"] == ["CUSTOM_BATCH_ATTEMPT"]
    # array_size はフォールバックが適用される
    assert aliases["array_size"][0] == "BATCH_TASK_COUNT"
