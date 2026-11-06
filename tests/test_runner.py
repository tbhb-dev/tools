"""Tests for tbhbdev.runner, the main of tools built from argparse subparsers."""

from __future__ import annotations

import argparse
from typing import NoReturn, override

import pytest

from tbhbdev import runner


class ExitsWithAMessage(argparse.ArgumentParser):
    """A parser whose exit raises SystemExit with the message as its code."""

    @override
    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise SystemExit(message)


@pytest.mark.usefixtures("plain")
def test_a_usage_error_without_a_status_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = ExitsWithAMessage(prog="tool")
    parser.add_argument("name")
    assert runner.main(parser, [], ValueError) == 2
    assert capsys.readouterr().err == "usage: tool [-h] name\n"
