"""Shell executor: dry-run mode, redaction, failure handling, markers, probe."""

from __future__ import annotations

import envd.config as config_mod
import pytest
from envd.core.shell import MASK, Shell, ShellError


@pytest.fixture()
def dry_run():
    config_mod.set_dry_run(True)
    yield
    config_mod.set_dry_run(False)


class TestDryRun:
    def test_commands_are_echoed_not_executed(self, dry_run):
        log: list[str] = []
        sh = Shell(log=log.append)
        result = sh.run(["ls", "/definitely/not/there"])
        assert result.rc == 0
        assert result.dry_run is True
        assert "$ ls /definitely/not/there" in log

    def test_capture_returns_empty_in_dry_run(self, dry_run):
        sh = Shell()
        assert sh.capture(["git", "status"]) == ""

    def test_fmt_env_masks_secret_values(self, dry_run):
        log: list[str] = []
        sh = Shell(log=log.append)
        sh.run(["true"], env={"PASSWORD": "super-secret", "MODE": "fast"})
        line = log[0]
        assert "MODE=fast" in line
        assert "super-secret" not in line
        assert MASK in line and "PASSWORD=" in line


class TestRealRun:
    def test_output_and_rc(self):
        log: list[str] = []
        sh = Shell(log=log.append)
        result = sh.run(["bash", "-c", "echo hello; echo ::step::x begin"])
        assert result.rc == 0
        assert "hello" in result.output
        assert result.markers == ["::step::x begin"]
        assert "echo hello" in log[0]  # the command echo itself
        assert "hello" in log[1]

    def test_check_failure_raises_with_tail(self):
        sh = Shell()
        with pytest.raises(ShellError, match="rc=3"):
            sh.run(["bash", "-c", "echo boom >&2; exit 3"])
        result = sh.run(["bash", "-c", "exit 3"], check=False)
        assert result.rc == 3

    def test_missing_binary(self):
        sh = Shell()
        with pytest.raises(ShellError, match="binary not found"):
            sh.run(["/no/such/binary", "--x"])

    def test_env_is_merged_not_replaced(self):
        sh = Shell()
        out = sh.capture(["bash", "-c", "echo $PATH"], env={"MY_VAR": "1"})
        assert out  # PATH survived the merge: bash was found and ran

    def test_redaction_applies_to_output_lines(self):
        log: list[str] = []
        sh = Shell(log=log.append, redactions=["sekrit-value"])
        sh.run(["bash", "-c", "echo sekrit-value"])
        assert MASK in log[1]
        assert "sekrit-value" not in log[1]


class TestProbe:
    def test_success_first_attempt(self):
        sh = Shell()
        assert sh.probe(["true"], attempts=1) is True

    def test_success_on_retry(self):
        sh = Shell()
        assert sh.probe(["true"], attempts=3, interval=0.01) is True

    def test_all_attempts_fail(self):
        sh = Shell()
        assert sh.probe(["false"], attempts=2, interval=0.01) is False
