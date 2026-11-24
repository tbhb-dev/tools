"""Best-effort telemetry for agent launches, loaded only during exec."""

from __future__ import annotations

import os
from contextlib import suppress
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def attributes(**values: str) -> None:
    with suppress(Exception):
        import tbhb_telemetry  # noqa: PLC0415 -- exec-only optional instrumentation

        tbhb_telemetry.set_attributes(**values)


def command(main: Callable[[], int]) -> int:
    result = 0  # pragma: no mutate -- read only after main assigns it
    error: BaseException | None = None
    started = False  # pragma: no mutate -- None takes the same fallback path

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

    # A telemetry failure before main falls back; one after main keeps its result.
    with suppress(Exception):
        endpoint = os.environ.get("TBHB_TELEMETRY_ENDPOINT", "")
        if (
            not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
            and endpoint.strip()
        ):
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint

        import tbhb_telemetry  # noqa: PLC0415 -- other commands must not import it

        def resolved() -> int:
            with suppress(Exception):
                for key in ("alpha", "agent", "exec"):
                    tbhb_telemetry.resolve(key)
                tbhb_telemetry.materialize()
            return launch()

        name = "tbhbdev"
        tbhb_telemetry.command(name, version=version(name), main=resolved)
    if error is not None:
        raise error
    return result if started else launch()
