"""Shared context passed to every tool handler."""

from __future__ import annotations

from dataclasses import dataclass

from uiforgemax.config import Config
from uiforgemax.state import RunStore


@dataclass
class ToolContext:
    config: Config
    store: RunStore

    @classmethod
    def from_config(cls, config: Config) -> "ToolContext":
        return cls(config=config, store=RunStore(config.runs_root))
