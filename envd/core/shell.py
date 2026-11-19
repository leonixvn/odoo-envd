"""Subprocess execution with dry-run, streaming logs and secret redaction.

Every external command the engine runs goes through here so that:

- ``ENVD_DRY_RUN`` (or :func:`envd.config.set_dry_run`) turns the whole engine into
  an echo mode — commands are logged, nothing executes. This is the foundation of
  the test suite and of first-run validation on a new server.
- secrets (PG passwords) never appear in logs: redactions mask them in every line.
- output is streamed line-by-line to a logger for live job logs, and kept (bounded)
  for error tails.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from envd.config import dry_run_active

MASK = "***"
SECRET_ENV_KEYS = frozenset({"PASSWORD", "PGPASSWORD", "ADMIN_PASSWORD"})
MAX_OUTPUT_CHARS = 200_000
TAIL_LINES = 50


class ShellError(RuntimeError):
    """A checked command exited non-zero."""

    def __init__(self, cmd: list[str], rc: int, tail: str) -> None:
        self.cmd = cmd
        self.rc = rc
        self.tail = tail
        shown = " ".join(cmd[:6])
        super().__init__(f"command failed (rc={rc}): {shown}…\n{tail.strip()[-800:]}")


@dataclass
class RunResult:
    rc: int
    duration: float
    output: str = ""
    tail: str = ""
    dry_run: bool = False
    # lines that carry kit → engine state markers (parsed by the job runner)
    markers: list[str] = field(default_factory=list)


class Shell:
    def __init__(
        self,
        log: Callable[[str], None] | None = None,
        redactions: Iterable[str] = (),
    ) -> None:
        self._log = log or (lambda line: None)
        self.redactions = [r for r in redactions if r]

    def add_redactions(self, *secrets: str) -> None:
        self.redactions.extend(s for s in secrets if s)

    def _mask(self, text: str) -> str:
        for secret in self.redactions:
            if secret in text:
                text = text.replace(secret, MASK)
        return text

    def _fmt_cmd(self, cmd: list[str], env: dict[str, str] | None) -> str:
        parts = []
        if env:
            for key in sorted(env):
                value = MASK if key.upper() in SECRET_ENV_KEYS else env[key]
                parts.append(f"{key}={shlex.quote(value)}")
            parts.append("")
        parts.extend(shlex.quote(str(c)) for c in cmd)
        return "$ " + " ".join(parts)

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        check: bool = True,
    ) -> RunResult:
        """Run a command, streaming output to the logger; honors dry-run."""
        line = self._mask(self._fmt_cmd(cmd, env))
        self._log(line)
        if dry_run_active():
            return RunResult(rc=0, duration=0.0, dry_run=True)
        return self._spawn(cmd, cwd=cwd, env=env, timeout=timeout, check=check)

    def run_read(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        check: bool = True,
    ) -> RunResult:
        """Execute a *plan-safe* command even in dry-run mode.

        Reserved for local git queries against the mirror (rev-parse, diff,
        ``git show``) and the mirror fetch itself: they only refresh engine-side
        bookkeeping, cannot mutate any environment, and keep dry-run plans
        realistic. Everything else must use :meth:`run`, which dry-run echoes.
        """
        self._log(self._mask(self._fmt_cmd(cmd, None)) + "   # read-only")
        return self._spawn(cmd, cwd=cwd, env=None, timeout=timeout, check=check)

    def _spawn(
        self,
        cmd: list[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int | None,
        check: bool,
    ) -> RunResult:
        started = time.monotonic()
        # merge with the ambient environment: replacing it entirely would drop
        # PATH/HOME and break git, docker, etc.
        full_env = {**os.environ, **env} if env is not None else None

        try:
            proc = subprocess.Popen(
                [str(c) for c in cmd],
                cwd=cwd,
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            # FileNotFoundError, PermissionError, … — wrap, never leak raw
            raise ShellError(cmd, 126, f"cannot execute: {exc}") from exc

        chunks: list[str] = []
        total = 0
        tail = deque(maxlen=TAIL_LINES)
        markers: list[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            clean = raw.rstrip("\n")
            if clean.startswith("::"):
                markers.append(clean)
            self._log(self._mask(clean))
            tail.append(clean + "\n")
            if total < MAX_OUTPUT_CHARS:
                chunks.append(clean + "\n")
                total += len(chunks[-1])
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise ShellError(cmd, -9, f"timeout after {timeout}s") from None

        result = RunResult(
            rc=rc,
            duration=time.monotonic() - started,
            output="".join(chunks),
            tail="".join(tail),
            markers=markers,
        )
        if check and rc != 0:
            raise ShellError(cmd, rc, result.tail)
        return result

    def log(self, message: str) -> None:
        """Log a free-form engine line into the job log (used by flows)."""
        self._log(message)

    def capture(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> str:
        """Run and return trimmed stdout (check=True; dry-run returns empty string)."""
        result = self.run(cmd, cwd=cwd, env=env, timeout=timeout, check=True)
        return result.output.strip()

    def probe(
        self,
        cmd: list[str],
        *,
        attempts: int,
        interval: float = 1.0,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> bool:
        """Retry a command until it succeeds; used for post-start health waits."""
        for attempt in range(1, attempts + 1):
            result = self.run(cmd, env=env, timeout=timeout, check=False)
            if result.rc == 0:
                return True
            if result.dry_run:
                return True
            if attempt < attempts:
                time.sleep(interval)
        return False
