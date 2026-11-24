"""Best-effort telemetry for agent launches, loaded only during exec."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_KEYS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "TBHB_TELEMETRY_ALLOW_HOSTS")
_MANAGED_SETTINGS = {
    "darwin": Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    "linux": Path("/etc/claude-code/managed-settings.json"),
}
_SETTINGS_READ_LIMIT = 64 * 1024 + 1  # The extra byte detects oversize files.
_REPORTED = Lock()


def _apply_settings(path: Path, source: str, sources: dict[str, str]) -> None:
    try:
        # Avoid waiting on a FIFO in place of a settings file.
        with open(
            path,
            "rb",
            buffering=0,
            opener=lambda name, flags: os.open(name, flags | os.O_NONBLOCK),
        ) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return
            data = stream.read(_SETTINGS_READ_LIMIT)
            if len(data) == _SETTINGS_READ_LIMIT:
                return
            settings: object = json.loads(data.decode())
    except OSError, ValueError, RecursionError:
        return
    if not isinstance(settings, dict) or not isinstance(
        env := settings.get("env"), dict
    ):
        return
    for key in _KEYS:
        if key in sources:
            continue
        value: object = env.get(key)
        if isinstance(value, str) and value:
            # An unrepresentable value, such as one with a NUL, leaves the key unset.
            with suppress(ValueError):
                os.environ[key] = value
                sources[key] = source


def _resolve_environment() -> None:
    disabled = os.environ.get("OTEL_SDK_DISABLED")
    if disabled is not None and disabled.lower() == "true":
        return
    sources = {key: "environment" for key in _KEYS if os.environ.get(key)}
    managed = _MANAGED_SETTINGS.get(sys.platform)
    if len(sources) < len(_KEYS) and managed is not None:
        _apply_settings(managed, "managed", sources)
    if len(sources) < len(_KEYS):
        with suppress(RuntimeError):  # Path.home() can fail for an unknown account.
            config_dir = Path(
                os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
            )
            _apply_settings(config_dir / "settings.json", "user", sources)

    _report(sources)


def _report(sources: dict[str, str]) -> None:
    stderr = sys.stderr
    if stderr is None or stderr.closed:
        return
    # Claim the only report for this process without waiting or retrying the write.
    if _REPORTED.acquire(blocking=False):
        details = "; ".join(f"{key} from {sources.get(key, 'none')}" for key in _KEYS)
        warning = (
            ""
            if len(sources) == len(_KEYS)
            else "; warning: the exporter may drop the launch trace"
        )
        line = f"telemetry: {details}{warning}\n"
        with suppress(OSError, ValueError):
            if sys.__stderr__ is stderr:
                # A failed buffered write can be retried during interpreter shutdown.
                os.write(stderr.fileno(), line.encode())
            else:
                stderr.write(line)
                stderr.flush()


def attributes(**values: str) -> None:
    with suppress(Exception):
        import tbhb_telemetry  # noqa: PLC0415 -- exec-only optional instrumentation

        tbhb_telemetry.set_attributes(**values)


def command(main: Callable[[], int]) -> int:
    result = 0
    error: BaseException | None = None
    started = False

    def launch() -> int:
        nonlocal result, error, started
        started = True
        attributes(**{"tbhb.agent.outcome": "failed"})
        try:
            result = main()
        except BaseException as exc:
            error = exc
            raise
        return result

    with suppress(Exception):
        _resolve_environment()

    # A telemetry failure before main falls back; one after main keeps its result.
    with suppress(Exception):
        import tbhb_telemetry  # noqa: PLC0415 -- other commands must not import it

        def resolved() -> int:
            with suppress(Exception):
                for key in ("alpha", "agent", "exec"):
                    tbhb_telemetry.resolve(key)
                tbhb_telemetry.materialize()
            return launch()

        tbhb_telemetry.command("tbhbdev", version=version("tbhbdev"), main=resolved)
    if error is not None:
        raise error
    return result if started else launch()
