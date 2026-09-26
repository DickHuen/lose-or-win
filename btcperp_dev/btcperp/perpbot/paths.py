"""Filesystem layout. Everything is relative to the install root (cross-platform)."""

from dataclasses import dataclass
from pathlib import Path

from perpbot import ROOT


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def default(cls) -> "Paths":
        return cls(root=ROOT)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "btcperp.sqlite3"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "btcperp.lock"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def smoketest_dir(self) -> Path:
        return self.data_dir / "smoketest"

    def ensure(self) -> None:
        for d in (self.data_dir, self.logs_dir, self.exports_dir, self.reports_dir,
                  self.backups_dir, self.smoketest_dir):
            d.mkdir(parents=True, exist_ok=True)
