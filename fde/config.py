"""User config (~/.fde.yaml) + per-repo manifest (fde.yaml)."""
from copy import deepcopy
from pathlib import Path

import yaml

DEFAULTS = {
    "agent_backend": "codex",
    "deploy": {"prod_branch": "prod", "port": 8124, "preview_port": 8123},
}
REQUIRED_MANIFEST = ["install_cmd", "test_cmd", "run_cmd", "app_type"]
APP_TYPES = {"js", "py", "node"}


class ConfigError(Exception):
    pass


def user_config_path() -> Path:
    return Path.home() / ".fde.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_user_config(path: Path | None = None) -> dict:
    p = path or user_config_path()
    cfg = deepcopy(DEFAULTS)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = _deep_merge(cfg, data)
    return cfg


def load_repo_manifest(repo_dir: Path) -> dict:
    p = Path(repo_dir) / "fde.yaml"
    if not p.exists():
        raise ConfigError(f"missing fde.yaml in {p.parent}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED_MANIFEST if k not in data or data[k] is None]
    if missing:
        raise ConfigError(f"fde.yaml missing required keys: {', '.join(missing)}")
    if data["app_type"] not in APP_TYPES:
        raise ConfigError(f"app_type must be one of {sorted(APP_TYPES)}")
    return data
