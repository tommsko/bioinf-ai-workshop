"""Configuration: TOML file + environment overrides, with safe defaults.

Lookup order for the config file:
  1. $METACENTRUM_MCP_CONFIG
  2. $XDG_CONFIG_HOME/metacentrum-mcp/config.toml (~/.config/...)
A missing file is fine; every option has a default.

Environment overrides (handy in an opencode.json "environment" block):
  METACENTRUM_HOST, METACENTRUM_USER, METACENTRUM_MCP_STATE_DIR,
  METACENTRUM_MCP_MAX_WAIT (seconds a single tool call may block).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError

METACENTRUM_FRONTENDS = [
    "skirit.metacentrum.cz",
    "skirit.ics.muni.cz",
    "skirit.grid.cesnet.cz",
    "skirit-lite.metacentrum.cz",
    "skirit-lite.ics.muni.cz",
    "perian.metacentrum.cz",
    "onyx.metacentrum.cz",
    "tarkil.metacentrum.cz",
    "nympha.metacentrum.cz",
    "charon.metacentrum.cz",
    "elmo.metacentrum.cz",
    "zenith.metacentrum.cz",
    "tilia.metacentrum.cz",
]


@dataclass
class ConnectionConfig:
    host: str = "skirit.metacentrum.cz"
    user: str = ""  # empty -> ssh decides (ssh_config / local username)
    port: int = 22
    allowed_hosts: list[str] = field(default_factory=lambda: list(METACENTRUM_FRONTENDS))
    identity_file: str = ""
    # Forward a local Kerberos ticket (kinit user@META) so the frontend session
    # can reach /storage/* of other sites and talk to PBS.
    gssapi: bool = True
    strict_host_key_checking: str = "accept-new"
    control_persist: str = "4h"  # master stays this long after the last client
    connect_timeout: int = 20
    command_timeout: int = 120
    auto_connect: bool = True  # tools (re)open the master on demand unless explicitly disconnected
    ssh_options: list[str] = field(default_factory=list)  # extra "-o Key=Value" values


@dataclass
class WorkspaceConfig:
    # Research storage. Users have a small quota in $HOME; the project storage has tens of TB.
    project_root: str = "/storage/praha5-elixir/projects/bioinf-fi"
    user_dir: str = "{user}"
    create_user_dir: bool = True
    # When true, writes and job working directories must live under write_roots
    # (the workspace by default); home is readable but not writable.
    enforce: bool = True
    read_roots: list[str] = field(default_factory=lambda: ["{workspace}", "{project_root}", "~"])
    write_roots: list[str] = field(default_factory=lambda: ["{workspace}"])
    # Local directories that upload/download may touch. "." = the server's cwd (the harness project).
    local_roots: list[str] = field(default_factory=lambda: ["."])


@dataclass
class LimitsConfig:
    max_walltime: str = "24:00:00"
    max_ncpus: int = 16
    max_mem_gb: float = 64
    max_scratch_gb: float = 200
    max_ngpus: int = 0
    allowed_queues: list[str] = field(default_factory=list)  # empty -> only default routing
    max_active_jobs: int = 5
    max_submissions_per_hour: int = 30
    max_interactive_sessions: int = 2
    max_interactive_walltime: str = "04:00:00"
    max_read_bytes: int = 64 * 1024
    max_write_bytes: int = 1024 * 1024
    max_transfer_bytes: int = 2 * 1024**3
    max_output_lines: int = 500
    max_list_entries: int = 500
    max_wait_seconds: int = 25  # keep below the harness' MCP tool timeout


@dataclass
class DefaultsConfig:
    """What a job or session gets when the agent does not say otherwise (reported by `cluster_info`)."""
    # ELIXIR CZ routing queue on pbs-m1 (routes to elixir_1d / elixir_2w); needs the `elixir` group.
    # Empty -> MetaCentrum's general `default` routing queue.
    queue: str = "elixircz@pbs-m1.metacentrum.cz"
    ncpus: int = 1
    mem: str = "4gb"
    walltime: str = "01:00:00"
    interactive_walltime: str = "01:00:00"
    scratch: str = "10gb"
    scratch_type: str = "local"
    # Shared, pre-built software environment (conda) sourced at the start of every batch job and
    # interactive shell when it exists. Empty disables it.
    software_env: str = "/storage/praha5-elixir/projects/bioinf-fi/tomsko/workshop/activate.sh"
    # Free-form site facts shown to the model by `cluster_info`.
    notes: list[str] = field(default_factory=list)


@dataclass
class FrontendExecConfig:
    enabled: bool = True
    timeout: int = 30
    allowed_commands: list[str] = field(
        default_factory=lambda: [
            "module", "which", "ls", "stat", "file", "df", "quota", "klist",
            "hostname", "id", "qstat", "pbsnodes", "realpath",
        ]
    )


@dataclass
class Config:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    frontend_exec: FrontendExecConfig = field(default_factory=FrontendExecConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    state_dir: Path = field(default_factory=lambda: _default_state_dir())
    # Prepended to every remote script, e.g. to point at site-specific PBS binaries.
    remote_env: dict[str, str] = field(default_factory=dict)
    source: str = "defaults"


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "metacentrum-mcp"


def _default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "metacentrum-mcp" / "config.toml"


def _apply(section: Any, values: dict[str, Any], name: str) -> None:
    known = {f.name: f for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"unknown option [{name}].{key}")
        current = getattr(section, key)
        if isinstance(current, bool) and not isinstance(value, bool):
            raise ConfigError(f"[{name}].{key} must be true/false")
        if isinstance(current, list) and not isinstance(value, list):
            raise ConfigError(f"[{name}].{key} must be a list")
        setattr(section, key, value)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    path = Path(path or os.environ.get("METACENTRUM_MCP_CONFIG") or _default_config_path()).expanduser()
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
        for name in ("connection", "workspace", "limits", "frontend_exec", "defaults"):
            if name in data:
                _apply(getattr(cfg, name), data.pop(name), name)
        if "remote_env" in data:
            cfg.remote_env = {str(k): str(v) for k, v in data.pop("remote_env").items()}
        if "state_dir" in data:
            cfg.state_dir = Path(data.pop("state_dir")).expanduser()
        if data:
            raise ConfigError(f"unknown top-level options in {path}: {sorted(data)}")
        cfg.source = str(path)

    env = os.environ
    if env.get("METACENTRUM_HOST"):
        cfg.connection.host = env["METACENTRUM_HOST"]
    if env.get("METACENTRUM_USER"):
        cfg.connection.user = env["METACENTRUM_USER"]
    if env.get("METACENTRUM_MCP_STATE_DIR"):
        cfg.state_dir = Path(env["METACENTRUM_MCP_STATE_DIR"]).expanduser()
    if env.get("METACENTRUM_MCP_MAX_WAIT"):
        cfg.limits.max_wait_seconds = int(env["METACENTRUM_MCP_MAX_WAIT"])

    if cfg.connection.host not in cfg.connection.allowed_hosts:
        cfg.connection.allowed_hosts.append(cfg.connection.host)
    cfg.workspace.local_roots = [str(Path(p).expanduser().resolve()) for p in cfg.workspace.local_roots]
    return cfg
