"""§5/§4 — The CLI's per-invocation options: the parsed global flags, the §4
config-precedence fold, and the ``LogConfig`` consumer.

Deliberately click-free (and backend-free): ``emuctl.cli`` must import without
click installed (§5's lazy rule), and this module is imported by every command
module.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

from emuctl.cli._render import emit_note
from emuctl.config import ResolvedConfig, resolve_config
from emuctl.config.model import EmuctlConfig

__all__ = ["Options"]

#: §4: "``LogConfig`` ... consumed ONLY by the CLI surface; the library never
#: configures handlers". This module is that surface.
_LOGGER_NAME: Final[str] = "emuctl"


@dataclass
class Options:
    """The parsed global options plus the lazily resolved config (§4 chain).

    ``--config`` / ``--no-config`` are §4's two flag-only escape hatches and
    ``--json`` is §5's output-mode switch; the value-shaped flags layer of §4's
    precedence chain is empty at v0 (§5 pins no such flag beyond those three).
    """

    config_path: str | None = None
    no_config: bool = False
    as_json: bool = False
    resolved: ResolvedConfig | None = field(default=None, init=False, repr=False)

    def config(self) -> EmuctlConfig:
        """Resolve the §4 precedence chain once per invocation.

        Warnings render as they are collected (human text on stderr — §4: "the
        CLI surface renders these for the user"), so they appear exactly once
        however many verbs touch the config. A broken layer is a typed
        ``ConfigError`` the CLI renders like any other failure (exit 3).
        """
        if self.resolved is None:
            folded = resolve_config(
                flags=None,
                config_file=self.config_path,
                no_config=self.no_config,
            )
            self.resolved = folded
            for warning in folded.warnings:
                emit_note(f"warning: {warning}")
        return self.resolved.config

    @contextmanager
    def session(self) -> Generator[EmuctlConfig]:
        """The resolved config plus the optional log sink for one I/O window."""
        config = self.config()
        with log_sink(config):
            yield config


@contextmanager
def log_sink(config: EmuctlConfig) -> Generator[None]:
    """Honor ``log.file`` by attaching a FileHandler to the ``emuctl`` logger
    for the duration of one invocation (removed on exit — never a process-global
    side effect).

    No ``log.file`` ⇒ no handler and no log output at all: §2's "never
    basicConfig, never handlers" discipline and §5's clean-stderr promise win
    over an unrequested stderr log stream.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    handler: logging.Handler | None = None
    previous_level = logger.level
    if config.log.file:
        with contextlib.suppress(OSError):  # an unopenable log path
            handler = logging.FileHandler(config.log.file, encoding="utf-8")
    if handler is not None:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        with contextlib.suppress(ValueError):  # the model validates level names
            logger.setLevel(config.log.level.upper())
    try:
        yield
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
            logger.setLevel(previous_level)
