"""Project paths and runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """All filesystem locations used by the pipeline, rooted at one directory."""

    root: Path

    @classmethod
    def from_env(cls) -> Paths:
        return cls(Path(os.environ.get("SHOPFLOW_HOME", Path.cwd())).resolve())

    @property
    def landing(self) -> Path:
        """Where the (simulated) source systems drop raw files."""
        return self.root / "data" / "landing"

    @property
    def warehouse(self) -> Path:
        return self.root / "data" / "warehouse.duckdb"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    def ensure(self) -> Paths:
        for p in (self.landing, self.warehouse.parent, self.artifacts, self.reports):
            p.mkdir(parents=True, exist_ok=True)
        return self
