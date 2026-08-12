import json
import multiprocessing


def _append_memory_in_process(memory_file: str, memory_root: str, content: str, start) -> None:
    from tools.update_memory_tool import UpdateMemoryTool

    start.wait(timeout=10)
    result = json.loads(
        UpdateMemoryTool(memory_file=memory_file, memory_root=memory_root)._run(
            operation="append",
            section="Concurrent",
            content=content,
        )
    )
    if not result.get("ok"):
        raise RuntimeError(str(result))


def test_update_memory_appends_deduplicates_and_replaces(tmp_path):
    from tools.update_memory_tool import UpdateMemoryTool

    memory = tmp_path / "MEMORY.md"
    memory.write_text("# Project Memory\n", encoding="utf-8")
    tool = UpdateMemoryTool(memory_file=str(memory), memory_root=str(tmp_path))

    first = json.loads(
        tool._run(
            operation="append",
            section="User Preferences",
            content="默认使用中文回答",
        )
    )
    second = json.loads(
        tool._run(
            operation="append",
            section="User Preferences",
            content="默认使用中文回答",
        )
    )
    assert first["changed"] is True
    assert second["changed"] is False
    assert memory.read_text(encoding="utf-8").count("默认使用中文回答") == 1

    replaced = json.loads(
        tool._run(
            operation="replace",
            old_text="默认使用中文回答",
            content="跟随用户最近使用的语言",
        )
    )
    assert replaced["changed"] is True
    assert "跟随用户最近使用的语言" in memory.read_text(encoding="utf-8")


def test_update_memory_requires_exact_replace_target(tmp_path):
    from tools.update_memory_tool import UpdateMemoryTool

    memory = tmp_path / "MEMORY.md"
    memory.write_text("# Memory\n\n- repeated\n- repeated\n", encoding="utf-8")
    tool = UpdateMemoryTool(memory_file=str(memory), memory_root=str(tmp_path))

    result = json.loads(tool._run(operation="remove", old_text="repeated"))

    assert result["ok"] is False
    assert "matched 2 times" in result["error"]
    assert memory.read_text(encoding="utf-8").count("repeated") == 2


def test_update_memory_rejects_unbound_and_symlink_scopes(tmp_path):
    from tools.update_memory_tool import UpdateMemoryTool

    unbound = json.loads(UpdateMemoryTool()._run(operation="append", content="never written"))
    assert unbound == {"ok": False, "error": "memory_scope_unavailable"}

    memory_root = tmp_path / "memory"
    external = tmp_path / "external"
    memory_root.mkdir()
    external.mkdir()
    project = memory_root / "project"
    project.symlink_to(external, target_is_directory=True)
    escaped = UpdateMemoryTool(
        memory_file=str(project / "MEMORY.md"),
        memory_root=str(memory_root),
    )

    result = json.loads(escaped._run(operation="append", content="never written"))

    assert result["ok"] is False
    assert "symbolic links are not allowed" in result["error"]
    assert not (external / "MEMORY.md").exists()


def test_update_memory_serializes_cross_process_appends(tmp_path):
    memory = tmp_path / "MEMORY.md"
    memory.write_text("# Memory\n", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_append_memory_in_process,
            args=(str(memory), str(tmp_path), f"entry-{index}", start),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    content = memory.read_text(encoding="utf-8")
    assert "entry-0" in content
    assert "entry-1" in content
