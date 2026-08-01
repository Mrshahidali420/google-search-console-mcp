import logging

from gsc_core import runlog


def test_init_writes_nothing_to_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    runlog._reset_for_tests()
    logger = runlog.init()
    logger.info("hello")
    logger.error("problem")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "problem" in captured.err


def test_init_writes_to_a_log_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    runlog._reset_for_tests()
    logger = runlog.init()
    logger.info("written to disk")

    for handler in logger.handlers:
        handler.flush()

    log_file = tmp_path / "logs" / "gsc.log"
    assert log_file.exists()
    assert "written to disk" in log_file.read_text(encoding="utf-8")


def test_init_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    runlog._reset_for_tests()
    first = runlog.init()
    handler_count = len(first.handlers)
    second = runlog.init()
    assert second is first
    assert len(second.handlers) == handler_count


def test_get_returns_child_of_root_logger(monkeypatch, tmp_path):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    runlog._reset_for_tests()
    child = runlog.get("gsc_core.store")
    assert child.name == "gsc.gsc_core.store"


def test_no_handler_targets_stdout(monkeypatch, tmp_path):
    import sys

    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    runlog._reset_for_tests()
    logger = runlog.init()
    streams = [getattr(h, "stream", None) for h in logger.handlers]
    assert sys.stdout not in streams
