"""Persistent SSH gate to a frontend, built on OpenSSH connection multiplexing.

One ControlMaster per (user, host, port) lives *outside* the MCP server process
(daemonised by `ssh -f`). That gives us:

  * one login shared by every MCP server process / agent on this machine,
  * survival across MCP crashes and restarts (the next process just reuses the socket),
  * all of the user's normal SSH setup (agent, keys, Kerberos/GSSAPI, ProxyJump).

Creation is serialised with a file lock so concurrent agents never race to log in.
An explicit disconnect leaves a marker so other processes don't silently reconnect.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import NotConnected, SSHError

OUT_MARKER = "__MCP_OUT_b7e1__"


@dataclass
class RemoteResult:
    rc: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _failure_kind(stderr: str) -> str:
    s = stderr.lower()
    if "permission denied" in s or "authentication" in s or "host key verification failed" in s:
        return "auth"
    return "network"


def _classify_ssh_failure(stderr: str) -> str:
    s = stderr.lower()
    if "permission denied" in s or "authentication" in s or "no more authentication methods" in s:
        return (
            "authentication failed (BatchMode: no password prompts from the MCP server). "
            "Ask the user to run `metacentrum-mcp login` in a terminal (supports password/OTP), "
            "or set up an SSH key or a forwarded Kerberos ticket (`kinit <user>@META`)."
        )
    if "host key verification failed" in s or "remote host identification has changed" in s:
        return "host key verification failed; the user must check ~/.ssh/known_hosts manually."
    if "could not resolve" in s or "name or service not known" in s:
        return "hostname could not be resolved (DNS/VPN?)."
    if "timed out" in s or "no route" in s or "network is unreachable" in s:
        return "network timeout; check VPN/network connectivity to the frontend."
    if "connection refused" in s:
        return "connection refused by the frontend."
    return "ssh failed: " + (stderr.strip().splitlines() or ["(no stderr)"])[-1]


def _control_dir(cfg: Config) -> Path:
    """A private directory for control sockets whose path fits the ~104-byte Unix socket limit.

    ssh appends a 17-char temporary suffix while creating the socket, so leave room for it.
    """
    candidates = [cfg.state_dir / "cm"]
    if os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(os.environ["XDG_RUNTIME_DIR"]) / "metacentrum-mcp")
    candidates.append(Path(f"/tmp/metacentrum-mcp-{os.getuid()}"))
    for d in candidates:
        if len(str(d)) + 1 + 12 + 17 < 100:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            st = d.stat()
            if st.st_uid != os.getuid():
                raise SSHError(f"control socket directory {d} is owned by someone else; refusing to use it")
            os.chmod(d, 0o700)
            return d
    raise SSHError("no suitable short directory for SSH control sockets")


class SSHTransport:
    def __init__(self, cfg: Config, host: str, user: str | None = None):
        self.cfg = cfg
        self.host = host
        self.user = user or cfg.connection.user or ""
        self.port = cfg.connection.port
        key = hashlib.sha1(f"{self.user}@{self.host}:{self.port}".encode()).hexdigest()[:12]
        self.key = key
        self.control_path = _control_dir(cfg) / key
        self.lock_path = cfg.state_dir / f"master-{key}.lock"
        self.disconnect_flag = cfg.state_dir / f"disconnected-{key}"
        self.master_log = cfg.state_dir / f"master-{key}.log"
        # Last failed automatic login. Auth failures are never retried automatically (a password-only
        # account cannot succeed in BatchMode, and repeated failures can lock the account); network
        # failures back off exponentially.
        self.fail_file = cfg.state_dir / f"connfail-{key}.json"

    # ----------------------------------------------------------------- options
    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _base_opts(self) -> list[str]:
        c = self.cfg.connection
        opts = [
            "-o", f"ControlPath={self.control_path}",
            "-o", f"ConnectTimeout={c.connect_timeout}",
            "-o", f"StrictHostKeyChecking={c.strict_host_key_checking}",
            # -o forms (not -p/-l) so the same list works for ssh and scp, whose -l means bandwidth.
            "-o", f"Port={self.port}",
        ]
        if c.gssapi:
            opts += ["-o", "GSSAPIAuthentication=yes", "-o", "GSSAPIDelegateCredentials=yes"]
        if c.identity_file:
            opts += ["-i", str(Path(c.identity_file).expanduser())]
        if self.user:
            opts += ["-o", f"User={self.user}"]
        for o in c.ssh_options:
            opts += ["-o", o]
        return opts

    def client_opts(self) -> list[str]:
        """Options for commands that must ride on the existing master."""
        return self._base_opts() + ["-o", "ControlMaster=no", "-o", "BatchMode=yes"]

    # ------------------------------------------------------------------ master
    def master_alive(self) -> bool:
        if not self.control_path.exists():
            return False
        try:
            r = subprocess.run(
                ["ssh", *self._base_opts(), "-O", "check", self.host],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0

    def master_pid(self) -> int | None:
        if not self.control_path.exists():
            return None
        r = subprocess.run(
            ["ssh", *self._base_opts(), "-O", "check", self.host],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        # "Master running (pid=12345)"
        for token in (r.stderr + r.stdout).replace(")", " ").split():
            if token.startswith("(pid="):
                return int(token[5:])
        return None

    @contextmanager
    def _lock(self, timeout: float):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as fh:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise SSHError("another process is opening the connection; retry in a few seconds")
                    time.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def open_master(self, interactive: bool = False) -> None:
        """Start a background ControlMaster. `interactive` lets ssh prompt on the terminal (CLI login)."""
        c = self.cfg.connection
        with self._lock(timeout=c.connect_timeout + 30):
            if self.master_alive():
                self.disconnect_flag.unlink(missing_ok=True)
                return
            # A socket file without a live master (crash, reboot, network drop) blocks a new master.
            self.control_path.unlink(missing_ok=True)
            cmd = [
                "ssh", *self._base_opts(),
                "-o", "ControlMaster=yes",
                "-o", f"ControlPersist={c.control_persist}",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4",
                "-o", "ExitOnForwardFailure=yes",
                "-N", "-f",
            ]
            if not interactive:
                cmd += ["-o", "BatchMode=yes"]
            cmd.append(self.host)
            if interactive:
                r = subprocess.run(cmd)  # inherits the terminal for password/OTP prompts
                err = ""
            else:
                with open(self.master_log, "ab") as log:
                    # The daemonised master must not hold our stdio: stdout is the MCP channel.
                    p = subprocess.Popen(
                        cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                    try:
                        _, err_b = p.communicate(timeout=c.connect_timeout + 15)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        raise SSHError(f"timed out connecting to {self.target}")
                    err = err_b.decode(errors="replace")
                    log.write(err_b)
                r = p
            if r.returncode != 0 or not self.master_alive():
                if not interactive:
                    self._record_failure(err)
                raise SSHError(f"cannot connect to {self.target}: {_classify_ssh_failure(err)}")
            self.disconnect_flag.unlink(missing_ok=True)
            self.fail_file.unlink(missing_ok=True)

    def _record_failure(self, stderr: str) -> None:
        prev = self.last_failure() or {}
        kind = _failure_kind(stderr)
        count = prev.get("count", 0) + 1 if prev.get("kind") == kind else 1
        self.fail_file.write_text(json.dumps({"kind": kind, "at": time.time(), "count": count,
                                              "message": _classify_ssh_failure(stderr)}))

    def last_failure(self) -> dict | None:
        try:
            return json.loads(self.fail_file.read_text())
        except (OSError, ValueError):
            return None

    def _check_backoff(self, explicit: bool) -> None:
        """Refuse an automatic login attempt that would just repeat a recent failure."""
        f = self.last_failure()
        if not f:
            return
        age = time.time() - f["at"]
        when = time.strftime("%H:%M", time.localtime(f["at"]))
        if f["kind"] == "auth":
            if explicit and age > 60:
                return
            raise NotConnected(
                f"not connected to {self.target}: the last login attempt ({when}) was rejected "
                f"({f['message']}). Not retrying automatically, to avoid locking the account. "
                "Ask the user to run `metacentrum-mcp login` in a terminal.")
        wait = min(30 * 2 ** (f["count"] - 1), 600)
        if age < (10 if explicit else wait):
            raise NotConnected(
                f"not connected to {self.target}: the frontend was unreachable at {when} ({f['message']}). "
                f"Next automatic retry in {int(wait - age)} s.")

    def close_master(self) -> bool:
        """Stop the master. Returns False if none was running."""
        self.disconnect_flag.touch()
        if not self.control_path.exists():
            return False
        r = subprocess.run(
            ["ssh", *self._base_opts(), "-O", "exit", self.host],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        )
        self.control_path.unlink(missing_ok=True)
        return r.returncode == 0

    def ensure(self) -> None:
        if self.master_alive():
            return
        if self.disconnect_flag.exists():
            raise NotConnected(
                f"not connected to {self.target} (the connection was closed explicitly). Call `connect` first."
            )
        if not self.cfg.connection.auto_connect:
            raise NotConnected(f"not connected to {self.target}. Call `connect` first.")
        self._check_backoff(explicit=False)
        self.open_master()

    # --------------------------------------------------------------- commands
    def wrap_script(self, script: str) -> str:
        # Values go inside double quotes so they may reference remote variables, e.g. PATH="/x:$PATH".
        env = "".join(f'export {k}="{v}"\n' for k, v in self.cfg.remote_env.items())
        # The body sits in a function so bash parses it completely before running it,
        # and runs with stdin from /dev/null so no command can swallow the script.
        return (
            f"{env}__mcp_main() {{\n{script}\n}}\n"
            f"printf '%s\\n' '{OUT_MARKER}'\n"
            f"__mcp_main </dev/null\n"
        )

    def run(self, script: str, timeout: int | None = None, retry: bool = True) -> RemoteResult:
        """Run a bash script (login shell, so `module` works) on the frontend over the master."""
        timeout = timeout or self.cfg.connection.command_timeout
        self.ensure()
        cmd = ["ssh", *self.client_opts(), "-T", self.host, "--",
               f"timeout -k 5 {int(timeout)} bash -l -s"]
        start = time.monotonic()
        try:
            p = subprocess.run(
                cmd, input=self.wrap_script(script).encode(), capture_output=True,
                timeout=timeout + 20,
            )
        except subprocess.TimeoutExpired:
            raise SSHError(f"remote command did not return within {timeout + 20}s")
        out = p.stdout.decode(errors="replace")
        err = p.stderr.decode(errors="replace")
        if OUT_MARKER not in out:
            # The script never started: the transport failed, not the command.
            if retry and p.returncode == 255:
                # The master may have died mid-call (network drop); ensure() reopens it.
                return self.run(script, timeout, retry=False)
            raise SSHError(f"remote execution failed on {self.target}: {_classify_ssh_failure(err)}")
        out = out.split(OUT_MARKER + "\n", 1)[-1] if OUT_MARKER + "\n" in out else out.split(OUT_MARKER, 1)[-1]
        if p.returncode == 124:
            err += f"\n[killed after {timeout}s timeout]"
        return RemoteResult(p.returncode, out, err, time.monotonic() - start)

    def scp(self, src: str, dst: str, timeout: int = 3600) -> None:
        self.ensure()
        cmd = ["scp", "-q", *self.client_opts(), src, dst]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise SSHError(f"transfer did not finish within {timeout}s")
        if p.returncode != 0:
            raise SSHError(f"transfer failed: {p.stderr.strip()[-500:]}")

    def remote_spec(self, path: str) -> str:
        return f"{self.target}:{path}"

    def interactive_ssh(self, remote_cmd: list[str] | None = None, tty: bool = True) -> int:
        """Run ssh attached to the user's terminal over the master (CLI helpers)."""
        self.ensure()
        cmd = ["ssh", *self._base_opts(), "-o", "ControlMaster=no"]
        if tty:
            cmd.append("-t")
        cmd.append(self.host)
        if remote_cmd:
            cmd += ["--", *remote_cmd]
        return subprocess.call(cmd)
