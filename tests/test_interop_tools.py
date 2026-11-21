from __future__ import annotations

import sys

import pytest

from tests.interop.tools import Result, Tools


def test_command_and_environment(tmp_path):
    tools = Tools(tmp_path)
    assert tools.command("bacrp", 1, "device") == [str(tmp_path / "bacrp"), "1", "device"]
    assert tools.command("bacscov", 1, line_buffered=True)[:2] == ["stdbuf", "-oL"]
    env = tools.env()
    assert env["BACNET_IFACE"] == "lo"
    assert env["BACNET_IP_PORT"] == "47899"
    assert (env["BACNET_BBMD_ADDRESS"], env["BACNET_BBMD_PORT"]) == ("127.0.0.2", "47820")
    assert int(env["BACNET_BBMD_TIMETOLIVE"]) > 0
    other = tools.env(47898)
    assert other["BACNET_IP_PORT"] == "47898" and other["BACNET_BBMD_PORT"] == "47820"


def test_failure_message_names_command_and_both_outputs():
    result = Result(1, "some out", "some err", ["tool", "a"])
    with pytest.raises(AssertionError) as err:
        result.expect_ok()
    text = str(err.value)
    assert "tool a" in text and "some out" in text and "some err" in text
    assert Result(0, "", "", ["t"]).expect_ok().code == 0


def _script(tmp_path, body):
    path = tmp_path / "tool.py"
    path.write_text(body, encoding="utf-8")
    return path


def _tools_running(tmp_path, body):
    """A Tools whose 'tool' is a python script (no stdbuf on Windows)."""
    script = _script(tmp_path, body)
    tools = Tools(tmp_path, stdbuf=())
    tools.command = lambda tool, *args, line_buffered=False: [sys.executable, "-u", str(script), *map(str, args)]
    return tools


def test_run_returns_output_and_a_timeout(tmp_path):
    tools = _tools_running(tmp_path, "import sys, time\nprint('hi', sys.argv[1])\nif sys.argv[1] == 'slow': time.sleep(30)\n")
    done = tools.run("t", "fast")
    assert (done.code, done.out.strip()) == (0, "hi fast")
    slow = tools.run("t", "slow", timeout=1)
    assert slow.timed_out and slow.code is None


def test_run_until_stops_a_tool_that_never_exits(tmp_path):
    tools = _tools_running(
        tmp_path, "import time\nprint('first', flush=True)\ntime.sleep(0.3)\nprint('second', flush=True)\ntime.sleep(60)\n"
    )
    result = tools.run_until("t", until=lambda text: "second" in text, total_timeout=20)
    assert not result.timed_out
    assert "first" in result.out and "second" in result.out
    unmet = tools.run_until("t", until=lambda text: "never" in text, total_timeout=1.5)
    assert unmet.timed_out and "first" in unmet.out
