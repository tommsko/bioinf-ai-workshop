"""The gate: current connection, facts about the remote side, and the path policy."""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any

from .config import Config
from .errors import NotConnected, PolicyError, RemoteCommandError, SSHError
from .ssh import RemoteResult, SSHTransport
from .state import State

FACTS_TTL = 600  # seconds; facts are re-probed on connect or when stale


def q(s: str) -> str:
    return shlex.quote(s)


class Gate:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self._target_file = cfg.state_dir / "current_target.json"
        host, user = cfg.connection.host, cfg.connection.user
        if self._target_file.exists():
            try:
                saved = json.loads(self._target_file.read_text())
                if saved.get("host") in cfg.connection.allowed_hosts:
                    host, user = saved["host"], saved.get("user") or user
            except (OSError, ValueError):
                pass
        self.transport = SSHTransport(cfg, host, user)
        self._facts: dict[str, Any] | None = None

    # ------------------------------------------------------------ connection
    @property
    def target(self) -> str:
        return self.transport.target

    def _facts_path(self) -> Path:
        return self.cfg.state_dir / f"facts-{self.transport.key}.json"

    def connect(self, host: str | None = None, user: str | None = None) -> dict[str, Any]:
        if host and host not in self.cfg.connection.allowed_hosts:
            raise PolicyError(
                f"host {host!r} is not allowed; allowed frontends: {', '.join(self.cfg.connection.allowed_hosts)}"
            )
        if host or user:
            self.transport = SSHTransport(self.cfg, host or self.transport.host, user or self.transport.user)
            self._facts = None
            self._target_file.write_text(json.dumps({"host": self.transport.host, "user": self.transport.user}))
        reused = self.transport.master_alive()
        if not reused:
            self.transport._check_backoff(explicit=True)
            self.transport.open_master()
        else:
            self.transport.disconnect_flag.unlink(missing_ok=True)
        facts = self.probe()
        return {"connected": True, "reused_existing_connection": reused, **self.summary(facts)}

    def disconnect(self) -> bool:
        self._facts = None
        self._facts_path().unlink(missing_ok=True)
        return self.transport.close_master()

    def run(self, script: str, timeout: int | None = None, retry: bool = True) -> RemoteResult:
        return self.transport.run(script, timeout=timeout, retry=retry)

    def run_ok(self, script: str, what: str, timeout: int | None = None, retry: bool = True) -> str:
        r = self.run(script, timeout=timeout, retry=retry)
        if not r.ok:
            msg = (r.stderr.strip() or r.stdout.strip())[-1500:]
            raise RemoteCommandError(f"{what} failed (exit {r.rc}): {msg}")
        return r.stdout

    # ----------------------------------------------------------------- facts
    def probe(self) -> dict[str, Any]:
        ws = self.cfg.workspace
        senv = self.cfg.defaults.software_env
        script = f"""
echo "user=$(id -un)"
echo "home=$HOME"
echo "groups=$(id -Gn)"
echo "hostname=$(hostname -f 2>/dev/null || uname -n)"
if klist -s 2>/dev/null; then echo "krb_valid=1"; else echo "krb_valid=0"; fi
# MIT prints "Default principal:", Heimdal "Principal:"; the krbtgt line is "<issued> <expires> krbtgt/...".
echo "krb_principal=$(klist 2>/dev/null | sed -n 's/^ *\\(Default \\)\\{{0,1\\}}[Pp]rincipal: //p' | head -1)"
tgt=$(klist 2>/dev/null | grep -m1 ' krbtgt/' | awk '{{n=NF-1; s=""; for(i=n/2+1;i<=n;i++) s=s" "$i; print s}}')
echo "krb_expires=$tgt"
[ -n "$tgt" ] && echo "krb_seconds_left=$(( $(date -d "$tgt" +%s 2>/dev/null || date +%s) - $(date +%s) ))"
if [ -n {q(senv)} ] && [ -f {q(senv)} ]; then echo "software_env_ok=1"; else echo "software_env_ok=0"; fi
command -v tmux >/dev/null && echo "tmux=1" || echo "tmux=0"
command -v qsub >/dev/null && echo "qsub=1" || echo "qsub=0"
proot={q(ws.project_root)}
wsdir="$proot/{ws.user_dir.replace('{user}', '$(id -un)')}"
echo "workspace=$wsdir"
if [ -d "$proot" ]; then echo "project_root_exists=1"; else echo "project_root_exists=0"; fi
if [ ! -d "$wsdir" ] && [ {1 if ws.create_user_dir else 0} = 1 ]; then
  if err=$(mkdir -p "$wsdir" 2>&1); then echo "workspace_created=1"; else
    echo "workspace_error=$(printf %s "$err" | tr '\\n' ' ')"; fi
fi
if [ -d "$wsdir" ] && [ -w "$wsdir" ]; then echo "workspace_ok=1"; else echo "workspace_ok=0"; fi
echo "workspace_df=$(df -hP "$wsdir" 2>/dev/null | awk 'NR==2{{print $4" free of "$2}}')"
echo "home_quota=$(df -hP "$HOME" 2>/dev/null | awk 'NR==2{{print $4" free of "$2}}')"
"""
        out = self.run_ok(script, "probing the frontend")
        facts: dict[str, Any] = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                facts[k.strip()] = v.strip()
        for k in ("krb_valid", "tmux", "qsub", "project_root_exists", "workspace_ok", "workspace_created",
                  "software_env_ok"):
            facts[k] = facts.get(k) == "1"
        facts["krb_seconds_left"] = int(facts["krb_seconds_left"]) if facts.get("krb_seconds_left", "").lstrip("-").isdigit() else None
        facts["groups"] = facts.get("groups", "").split()
        facts["probed_at"] = time.time()
        # Keep the canonical /storage/... spelling (what docs and colleagues use); /storage/x is a
        # symlink to /auto/x, which the policy checks see through separately.
        if facts["workspace_ok"]:
            facts["workspace"] = self._realpath_raw([facts["workspace"]], logical=True)[0]
        facts["home"] = self._realpath_raw([facts["home"]], logical=True)[0] if facts.get("home") else ""
        self._facts = facts
        self._facts_path().write_text(json.dumps(facts))
        return facts

    def facts(self) -> dict[str, Any]:
        if self._facts and time.time() - self._facts["probed_at"] < FACTS_TTL:
            return self._facts
        p = self._facts_path()
        if p.exists():
            try:
                f = json.loads(p.read_text())
                if time.time() - f.get("probed_at", 0) < FACTS_TTL and self.transport.master_alive():
                    self._facts = f
                    return f
            except ValueError:
                pass
        return self.probe()

    def summary(self, facts: dict[str, Any]) -> dict[str, Any]:
        warnings = []
        left = facts.get("krb_seconds_left")
        if not facts["krb_valid"]:
            warnings.append(
                "No valid Kerberos ticket on the frontend: PBS commands and cross-site /storage access "
                "(including the project workspace) may fail. Ask the user to run `metacentrum-mcp kinit`."
            )
        elif left is not None and left < 3600:
            warnings.append(f"Kerberos ticket expires in {max(left, 0) // 60} min; ask the user to run "
                            "`metacentrum-mcp kinit` before long work.")
        if not facts["workspace_ok"]:
            warnings.append(
                f"Project workspace {facts.get('workspace')} is not usable "
                f"({facts.get('workspace_error') or 'not writable'}). "
                + ("Writes and jobs are blocked until it is fixed (usually a Kerberos ticket)."
                   if self.cfg.workspace.enforce else "Falling back to home (small quota!).")
            )
        if not facts["tmux"]:
            warnings.append("tmux is not available on this frontend: interactive sessions are disabled.")
        dq = self.cfg.defaults.queue
        if dq.startswith("elixir") and "elixir" not in facts.get("groups", []):
            warnings.append(f"Default queue {dq} requires membership in the 'elixir' group, which "
                            f"{facts.get('user')} lacks; jobs will be rejected. Set [defaults] queue = \"\".")
        return {
            "target": self.target,
            "frontend": facts.get("hostname"),
            "remote_user": facts.get("user"),
            "workspace": facts.get("workspace"),
            "workspace_ok": facts["workspace_ok"],
            "workspace_free": facts.get("workspace_df"),
            "home": facts.get("home"),
            "home_free": facts.get("home_quota"),
            "kerberos": {
                "valid": facts["krb_valid"],
                "principal": facts.get("krb_principal"),
                "expires": facts.get("krb_expires"),
                "hours_left": round(left / 3600, 1) if left is not None else None,
            },
            "warnings": warnings,
        }

    # ----------------------------------------------------------- path policy
    def _realpath_raw(self, paths: list[str], logical: bool = False) -> list[str]:
        """Normalise remote paths. physical: resolve symlinks; logical (-s): only clean up . / .. / //."""
        flag = "-m -s" if logical else "-m"
        out = self.run_ok(f"realpath {flag} -- " + " ".join(q(p) for p in paths), "resolving paths")
        res = out.splitlines()
        if len(res) != len(paths):
            raise RemoteCommandError(f"could not resolve paths: {paths}")
        return res

    def workspace(self, required: bool = True) -> str:
        f = self.facts()
        if f["workspace_ok"]:
            return f["workspace"]
        if required and self.cfg.workspace.enforce:
            raise PolicyError(
                f"the project workspace {f.get('workspace')} is not available "
                f"({f.get('workspace_error') or 'missing or not writable'}). All data and jobs must live there "
                "(home has a small quota). Most likely the Kerberos ticket on the frontend is missing or "
                "expired: ask the user to run `metacentrum-mcp kinit`, then call `connect` again."
            )
        return f["home"]

    def _roots(self, write: bool) -> list[str]:
        """Allowed roots, logical spelling."""
        f = self.facts()
        ws_cfg = self.cfg.workspace
        roots = ws_cfg.write_roots if write else ws_cfg.read_roots
        if write and not ws_cfg.enforce:
            roots = [*roots, "~"]
        out = []
        for r in roots:
            if "{workspace}" in r and not f["workspace_ok"]:
                continue
            r = (r.replace("{workspace}", f.get("workspace", ""))
                  .replace("{project_root}", ws_cfg.project_root)
                  .replace("{user}", f.get("user", "")))
            if r == "~" or r.startswith("~/"):
                r = f["home"] + r[1:]
            out.append(r.rstrip("/") or "/")
        return out

    def resolve(self, path: str, write: bool = False, base: str | None = None) -> str:
        """Expand ~ and relative paths (relative to the workspace), enforce roots, return the logical path."""
        return self.resolve_many([path], write=write, base=base)[0]

    def resolve_many(self, paths: list[str], write: bool = False, base: str | None = None) -> list[str]:
        f = self.facts()
        base = base or self.workspace(required=write)
        expanded = []
        for p in paths:
            p = p.strip()
            if not p:
                raise PolicyError("empty path")
            if p == "~" or p.startswith("~/"):
                p = f["home"] + p[1:]
            elif not p.startswith("/"):
                p = f"{base.rstrip('/')}/{p}"
            expanded.append(p)
        roots = self._roots(write)
        # The check uses physical paths (so a symlink cannot smuggle a write out of the workspace);
        # the caller gets the logical path, which also works on compute nodes.
        n = len(expanded)
        physical = self._realpath_raw(expanded + roots)
        phys_paths, phys_roots = physical[:n], physical[n:]
        logical = self._realpath_raw(expanded, logical=True)
        for orig, ph, lo in zip(paths, phys_paths, logical):
            if not any(ph == root or ph.startswith(root.rstrip("/") + "/") for root in phys_roots):
                kind = "writable" if write else "readable"
                hint = ""
                if write and (lo.startswith(f["home"]) or orig.startswith("~")):
                    hint = (" Home has a small quota: put data, scripts and job directories in the project "
                            f"workspace {f.get('workspace')} instead.")
                raise PolicyError(f"path {orig!r} -> {lo} is outside the {kind} roots {roots}.{hint}")
        return logical

    def require_connected(self) -> None:
        try:
            self.transport.ensure()
        except SSHError as e:
            raise NotConnected(str(e)) from e
