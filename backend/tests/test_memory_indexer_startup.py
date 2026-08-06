from unittest.mock import Mock

from graph.memory_indexer import MemoryIndexer


def _indexer_with_memory(tmp_path) -> MemoryIndexer:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("stable memory", encoding="utf-8")
    return MemoryIndexer(tmp_path)


def test_initialize_index_reuses_matching_persisted_index(
    monkeypatch,
    tmp_path,
) -> None:
    indexer = _indexer_with_memory(tmp_path)
    indexer._storage_dir.mkdir(parents=True)
    indexer._save_hash(indexer._get_file_hash())
    persisted = object()
    load_index = Mock(return_value=persisted)
    rebuild_index = Mock()
    monkeypatch.setattr(indexer, "_load_index", load_index)
    monkeypatch.setattr(indexer, "rebuild_index", rebuild_index)

    indexer.initialize_index()

    load_index.assert_called_once_with()
    rebuild_index.assert_not_called()


def test_initialize_index_rebuilds_stale_index(monkeypatch, tmp_path) -> None:
    indexer = _indexer_with_memory(tmp_path)
    indexer._storage_dir.mkdir(parents=True)
    indexer._save_hash("stale")
    load_index = Mock()
    rebuild_index = Mock()
    monkeypatch.setattr(indexer, "_load_index", load_index)
    monkeypatch.setattr(indexer, "rebuild_index", rebuild_index)

    indexer.initialize_index()

    load_index.assert_not_called()
    rebuild_index.assert_called_once_with()
