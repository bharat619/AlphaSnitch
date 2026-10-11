"""Credential-free configuration and lazy runtime-directory creation."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, ConfigDict


class PathSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path | None = None
    database_dir: Path | None = None
    snapshots_dir: Path | None = None
    cache_dir: Path | None = None
    reports_dir: Path | None = None
    logs_dir: Path | None = None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["US"] = "US"
    paths: PathSettings = PathSettings()


class ConfigurationError(ValueError):
    """Public configuration error with no raw values from the input."""


class Runtime(BaseModel):
    market: Literal["US"]
    config_file: Path | None
    data_dir: Path
    database_dir: Path
    snapshots_dir: Path
    cache_dir: Path
    reports_dir: Path
    logs_dir: Path

    def initialize(self) -> None:
        for name in ("data", "database", "snapshots", "cache", "reports", "logs"):
            getattr(self, f"{name}_dir").mkdir(parents=True, exist_ok=True)


def default_config_file() -> Path:
    return user_config_path("stock-analyser", appauthor=False) / "config.toml"


def load_runtime(
    config_file: Path | None = None,
    data_dir: Path | None = None,
    market: str | None = None,
) -> Runtime:
    explicit = config_file is not None or bool(os.environ.get("STOCK_ANALYSER_CONFIG"))
    path = config_file or Path(os.environ.get("STOCK_ANALYSER_CONFIG", default_config_file()))
    try:
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        if explicit and not path.is_file():
            raise ConfigurationError("The requested configuration file does not exist.")
        settings = Settings.model_validate(raw)
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError):
        # Pydantic and TOML errors can include secret-bearing raw input values.
        raise ConfigurationError(
            "Cannot load configuration. Use config.example.toml; only market US is supported."
        ) from None

    active_market = market or os.environ.get("STOCK_ANALYSER_MARKET") or settings.market
    if active_market != "US":
        raise ConfigurationError("Unsupported market. Only US is enabled.")

    def resolve(value: Path) -> Path:
        value = value.expanduser()
        return (value if value.is_absolute() else path.absolute().parent / value).resolve()

    root = data_dir or os.environ.get("STOCK_ANALYSER_DATA_DIR") or settings.paths.data_dir
    root = resolve(Path(root)) if root else user_data_path("stock-analyser", appauthor=False)
    values = {}
    for name in ("database", "snapshots", "cache", "reports", "logs"):
        key = f"{name}_dir"
        override = os.environ.get(f"STOCK_ANALYSER_{key.upper()}") or getattr(settings.paths, key)
        values[key] = resolve(Path(override)) if override else root / name
    return Runtime(
        market="US",
        config_file=path.resolve() if path.exists() else None,
        data_dir=root,
        **values,
    )
