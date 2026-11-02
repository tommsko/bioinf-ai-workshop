"""Interactive PBS jobs bridged through tmux on the frontend.

`qsub -I` runs inside a detached tmux session (dedicated socket `-L mcp`) on the
frontend, so the interactive job survives MCP restarts, SSH drops and laptop sleep.
All pane output is appended to a log file in the project workspace (pipe-pane).

Commands are typed into the pane wrapped in start/end markers:

    printf '\\n__MCP_S_%s__\\n' RID; { <command>
    } </dev/null; printf '\\n__MCP_E_%s_%s__\\n' RID "$?"

The terminal echoes the *format strings* (with %s), so only the executed printf
produces a line matching the marker regex. The exit code rides on the end marker.
Because tmux runs `qsub -I` directly (no shell around it), when the job ends the
pane dies instead of dropping the agent into a shell on the frontend.

A human can watch or take over: `metacentrum-mcp attach <session_id>`.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
import uuid
from typing import Any

from .errors import NotFound, PolicyError, RemoteCommandError
from .gate import Gate, q
from .pbs import Resources
from .textutil import clean_terminal, truncate_lines

TMUX = "tmux -L mcp"
SPECIAL_KEYS = {"C-c", "C-d", "C-z", "C-\\", "Escape", "Enter", "Tab", "Up", "Down", "Left", "Right",
                "PageUp", "PageDown", "Home", "End", "BSpace", "q", "y", "n"}
MAX_INLINE_LINE = 1000
MAX_INLINE_TOTAL = 3000


class Interactive:
    def __init__(self, gate: Gate):
        self.gate = gate
        self.cfg = gate.cfg
        self.state = gate.state

    # ----------------------------------------------------------------- helpers
    def _session(self, session_id: str) -> dict[str, Any]:
        s = self.state.get_session(session_id)
        if not s or s["target"] != self.gate.target:
            raise NotFound(f"no interactive session {session_id!r} on {self.gate.target}; see interactive_list")
        return s

    def _reconcile(self) -> list[dict]:
        """Mark sessions whose tmux session vanished (frontend reboot, manual kill) as ended."""
        live = self.state.sessions(self.gate.target)
        if not live:
            return []
        r = self.gate.run(f"{TMUX} ls -F '#{{session_name}} #{{pane_dead}}' 2>/dev/null || true")
        present = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                present[parts[0]] = parts[1] == "1"
        out, dead = [], []
        for s in live:
            if s["tmux_name"] not in present or present[s["tmux_name"]]:
                self.state.update_session(s["session_id"], state="ended", pending_run=None, pending_off=None)
                if present.get(s["tmux_name"]):
                    dead.append(s["tmux_name"])
            else:
                out.append(s)
        if dead:
            self.gate.run("; ".join(f"{TMUX} kill-session -t {q(n)} 2>/dev/null" for n in dead) + "; true")
        return out

    def _probe(self, s: dict) -> dict[str, Any]:
        log = q(s["log_path"])
        r = self.gate.run(f"""
if {TMUX} has-session -t {q(s['tmux_name'])} 2>/dev/null; then echo alive=1; else echo alive=0; fi
echo dead=$({TMUX} display -p -t {q(s['tmux_name'])} '#{{pane_dead}}' 2>/dev/null)
echo size=$(stat -c %s {log} 2>/dev/null || echo 0)
echo waiting=$(grep -a -o -m1 -E 'qsub: waiting for job [^ ]+' {log} 2>/dev/null | awk '{{print $5}}')
echo ready=$(grep -a -o -m1 -E 'qsub: job [^ ]+ ready' {log} 2>/dev/null | awk '{{print $3}}')
grep -a -E '^qsub: ' {log} 2>/dev/null | grep -v -E 'waiting for job|ready$' | tail -3 | sed 's/^/qsub_msg=/'
""")
        p: dict[str, Any] = {"qsub_msgs": []}
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            if k == "qsub_msg":
                p["qsub_msgs"].append(clean_terminal(v).strip())
            else:
                p[k] = v.strip()
        p["alive"] = p.get("alive") == "1" and p.get("dead") != "1"
        p["size"] = int(p.get("size") or 0)
        job_id = p.get("ready") or p.get("waiting") or s["job_id"]
        if p["alive"]:
            st = "ready" if p.get("ready") else "queued" if p.get("waiting") else "starting"
        else:
            st = "ended"
        if job_id != s["job_id"] or st != s["state"]:
            self.state.update_session(s["session_id"], job_id=job_id, state=st)
        p["job_id"], p["state"] = job_id, st
        return p

    def _send(self, s: dict, text: str | None = None, keys: list[str] | None = None) -> int:
        """Type text (literally) and/or keys into the pane; return the log size before sending."""
        parts = [f"off=$(stat -c %s {q(s['log_path'])} 2>/dev/null || echo 0)"]
        if text is not None:
            # $(...) below strips trailing newlines, so a final newline becomes an explicit Enter.
            if text.endswith("\n"):
                text = text.rstrip("\n")
                keys = ["Enter", *(keys or [])]
            b64 = base64.b64encode(text.encode()).decode()
            parts.append(f"{TMUX} send-keys -t {q(s['tmux_name'])} -l -- \"$(printf %s {q(b64)} | base64 -d)\"")
        for k in keys or []:
            parts.append(f"{TMUX} send-keys -t {q(s['tmux_name'])} {q(k)}")
        parts.append('echo "$off"')
        out = self.gate.run_ok("\n".join(parts), "sending keys to the interactive session")
        return int(out.strip().splitlines()[-1])

    def _wait_for(self, s: dict, offset: int, pattern: str, timeout: float) -> str:
        """Block on the frontend until `pattern` appears after `offset`. Returns done|dead|timeout."""
        out = self.gate.run_ok(f"""
deadline=$((SECONDS+{int(timeout)}))
while :; do
  if tail -c +{offset + 1} {q(s['log_path'])} 2>/dev/null | grep -a -q -E {q(pattern)}; then echo done; break; fi
  d=$({TMUX} display -p -t {q(s['tmux_name'])} '#{{pane_dead}}' 2>/dev/null || echo 1)
  if [ "$d" = 1 ]; then echo dead; break; fi
  if [ $SECONDS -ge $deadline ]; then echo timeout; break; fi
  sleep 0.25
done
""", "waiting for interactive output", timeout=int(timeout) + 30)
        return out.strip().splitlines()[-1]

    def _read_bytes(self, s: dict, start: int, length: int) -> bytes:
        out = self.gate.run_ok(
            f"tail -c +{start + 1} {q(s['log_path'])} | head -c {length} | base64 -w0",
            "reading interactive output",
        )
        return base64.b64decode(out.strip() or b"")

    # ------------------------------------------------------------------- start
    def start(self, resources: Resources, name: str, wait_seconds: int,
              use_software_env: bool = True) -> dict[str, Any]:
        lim = self.cfg.limits
        facts = self.gate.facts()
        if not facts["tmux"]:
            raise PolicyError("tmux is not available on this frontend, so interactive sessions are disabled")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_.\-]{0,62}$", name):
            raise PolicyError("invalid session name (letters, digits, _ - .; must start with a letter)")
        res = resources.validate(lim, interactive=True, default_queue=self.cfg.defaults.queue)
        ws = self.gate.workspace()
        with self.state.lock(f"interactive-{self.gate.transport.key}", timeout=30):
            live = self._reconcile()
            if len(live) >= lim.max_interactive_sessions:
                raise PolicyError(
                    f"{len(live)} interactive sessions already open (cap {lim.max_interactive_sessions}): "
                    f"{[x['session_id'] for x in live]}. Reuse one or stop it with interactive_stop.")
            sid = uuid.uuid4().hex[:8]
            sdir = f"{ws}/.mcp/interactive"
            s = {
                "session_id": sid, "target": self.gate.target, "job_id": None, "tmux_name": f"mcp-{sid}",
                "log_path": f"{sdir}/{sid}.log", "cmd_dir": f"{sdir}/{sid}.cmd", "workdir": ws,
                "resources": {**res, "use_software_env": use_software_env},
                "created_at": time.time(), "state": "starting",
            }
            qsub = f"qsub -I -N {name} -l {res['select']} -l walltime={res['walltime']}"
            if "queue" in res:
                qsub += f" -q {res['queue']}"
            gate_file = f"{sdir}/{sid}.go"
            # The pane command waits for pipe-pane to be attached so no output is lost. It is exec'd
            # directly by tmux (argv form), not through the user's login shell, which may be zsh/fish/tcsh.
            inner = f"while [ ! -e {q(gate_file)} ]; do sleep 0.1; done; rm -f {q(gate_file)}; exec {qsub}"
            self.gate.run_ok(f"""
set -e
mkdir -p {q(sdir)} {q(s['cmd_dir'])}
: > {q(s['log_path'])}
{TMUX} new-session -d -s {q(s['tmux_name'])} -x 200 -y 50 -- bash -c {q(inner)}
{TMUX} set-option -t {q(s['tmux_name'])} history-limit 50000
{TMUX} set-option -w -t {q(s['tmux_name'])} remain-on-exit on
{TMUX} pipe-pane -t {q(s['tmux_name'])} -o {q('cat >> ' + q(s['log_path']))}
touch {q(gate_file)}
""", "starting the interactive session")
            self.state.add_session(**s)
        return self.status(sid, wait_seconds=wait_seconds)

    # ------------------------------------------------------------------ status
    def status(self, session_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        s = self._session(session_id)
        wait_seconds = max(0, min(wait_seconds, self.cfg.limits.max_wait_seconds))
        p = self._probe(s)
        if p["state"] in ("starting", "queued") and wait_seconds:
            self._wait_for(s, 0, "qsub: job [^ ]+ ready", wait_seconds)
            p = self._probe(s)
        s = self._session(session_id)
        if p["state"] == "ready" and not s["shell_ready"]:
            self._init_shell(s)
        result = {
            "session_id": session_id, "job_id": p["job_id"], "state": p["state"],
            "resources": json.loads(s["resources"] or "{}"), "workdir": s["workdir"], "log_path": s["log_path"],
            "attach_hint": f"human can watch/take over: metacentrum-mcp attach {session_id}",
        }
        if p["qsub_msgs"]:
            result["qsub_messages"] = p["qsub_msgs"]
        if p["state"] in ("starting", "queued"):
            result["note"] = "job is waiting in the PBS queue; call interactive_status again (wait_seconds) later"
        elif p["state"] == "ended":
            result["note"] = "the interactive job has ended (walltime, exit, or qdel); start a new session if needed"
        elif s["pending_run"]:
            result["busy_with_run"] = s["pending_run"]
        return result

    def _init_shell(self, s: dict) -> None:
        sid = s["session_id"]
        senv = self.cfg.defaults.software_env
        use_env = json.loads(s["resources"] or "{}").get("use_software_env", True)
        senv_cmd = f"[ -f {q(senv)} ] && source {q(senv)}; " if senv and use_env else ""
        init = (
            '[ -n "$BASH_VERSION" ] || exec bash -l\n'
            f"set +H; PS1='[mcp \\W]\\$ '; PS2=''; PROMPT_COMMAND=''; unset HISTFILE; "
            f"export PAGER=cat GIT_PAGER=cat MANPAGER=cat SYSTEMD_PAGER= NO_COLOR=1; "
            f"cd {q(s['workdir'])}; {senv_cmd}printf '__MCP_INIT_%s__\\n' {sid}\n"
        )
        off = self._send(s, text=init)
        if self._wait_for(s, off, f"__MCP_INIT_{sid}__", 30) != "done":
            raise RemoteCommandError("the interactive shell did not respond to initialisation; "
                                     "check it with interactive_read or attach as a human")
        self.state.update_session(sid, shell_ready=1)

    def _ready_session(self, session_id: str) -> dict:
        st = self.status(session_id)
        if st["state"] != "ready":
            raise PolicyError(f"session {session_id} is {st['state']}, not ready: commands run only inside a "
                              "started interactive job, never on the frontend. " + st.get("note", ""))
        return self._session(session_id)

    # --------------------------------------------------------------------- run
    def run(self, session_id: str, command: str, timeout: int) -> dict[str, Any]:
        timeout = max(1, min(timeout, self.cfg.limits.max_wait_seconds))
        command = command.replace("\r\n", "\n").strip("\n")
        if not command.strip():
            raise PolicyError("empty command")
        syn = subprocess.run(["bash", "-n"], input=command, capture_output=True, text=True)
        if syn.returncode != 0:
            raise PolicyError(f"bash syntax error, not sent: {syn.stderr.strip().replace('bash: ', '')}")
        with self.state.lock(f"session-{session_id}", timeout=10):
            s = self._ready_session(session_id)
            if s["pending_run"]:
                if self._finished_pending(s):
                    s = self._session(session_id)
                else:
                    raise PolicyError(
                        f"the previous command ({s['pending_run']}) is still running in this session. "
                        "Use interactive_wait to collect it, or interactive_send key='C-c' to interrupt it.")
            seq = self.state.next_run_seq(session_id)
            rid = f"{session_id}r{seq}"
            lines = command.split("\n")
            if len(command) > MAX_INLINE_TOTAL or any(len(x) > MAX_INLINE_LINE for x in lines):
                path = f"{s['cmd_dir']}/{rid}.sh"
                b64 = base64.b64encode(command.encode()).decode()
                self.gate.run_ok(f"printf %s {q(b64)} | base64 -d > {q(path)}", "staging a long command")
                body = f"source {q(path)}"
            else:
                body = command
            typed = (f"printf '\\n__MCP_S_%s__\\n' {rid}; {{ {body}\n"
                     f"}} </dev/null; printf '\\n__MCP_E_%s_%s__\\n' {rid} \"$?\"\n")
            off = self._send(s, text=typed)
            self.state.update_session(session_id, pending_run=rid, pending_off=off)
        return self._collect(session_id, rid, off, timeout)

    def _finished_pending(self, s: dict) -> bool:
        out = self.gate.run_ok(
            f"tail -c +{s['pending_off'] + 1} {q(s['log_path'])} | grep -a -c -E '__MCP_E_{s['pending_run']}_[0-9]+__' || true",
            "checking the previous command")
        if out.strip().splitlines() and out.strip().splitlines()[-1] != "0":
            self.state.update_session(s["session_id"], pending_run=None, pending_off=None)
            return True
        return False

    def wait(self, session_id: str, timeout: int) -> dict[str, Any]:
        s = self._session(session_id)
        if not s["pending_run"]:
            return {"session_id": session_id, "status": "idle", "note": "no command is running in this session"}
        timeout = max(1, min(timeout, self.cfg.limits.max_wait_seconds))
        return self._collect(session_id, s["pending_run"], s["pending_off"], timeout)

    def _collect(self, session_id: str, rid: str, off: int, timeout: int) -> dict[str, Any]:
        s = self._session(session_id)
        end_re = f"__MCP_E_{rid}_([0-9]+)__"
        outcome = self._wait_for(s, off, end_re.replace("([0-9]+)", "[0-9]+"), timeout)
        pos = self.gate.run_ok(
            f"tail -c +{off + 1} {q(s['log_path'])} | grep -a -b -o -E '__MCP_(S_{rid}|E_{rid}_[0-9]+)__' || true",
            "locating command output")
        start = end = None
        exit_code = None
        for line in pos.splitlines():
            o, _, m = line.partition(":")
            if m == f"__MCP_S_{rid}__":
                start = off + int(o) + len(m)
            elif m.startswith(f"__MCP_E_{rid}_"):
                end = off + int(o)
                exit_code = int(re.match(end_re, m).group(1))
        size_now = None
        if start is None:
            start = off
        if end is None:
            size_now = int(self.gate.run_ok(f"stat -c %s {q(s['log_path'])}", "stat").strip() or 0)
        stop = end if end is not None else size_now
        text = self._region(s, start, stop)
        result: dict[str, Any] = {"session_id": session_id, "run_id": rid}
        if end is not None:
            self.state.update_session(session_id, pending_run=None, pending_off=None)
            result.update(status="completed", exit_code=exit_code)
        elif outcome == "dead":
            self.state.update_session(session_id, pending_run=None, pending_off=None, state="ended")
            result.update(status="session_ended",
                          note="the interactive job ended while the command ran (exit, walltime or qdel)")
        else:
            result.update(status="running",
                          note=f"still running after {timeout}s. Call interactive_wait to keep waiting, "
                               "interactive_read to peek, or interactive_send key='C-c' to interrupt. "
                               "Long computations belong in a batch job (submit_job).")
        result.update(text)
        return result

    def _region(self, s: dict, start: int, stop: int) -> dict[str, Any]:
        length = max(0, stop - start)
        cap = self.cfg.limits.max_read_bytes
        head_n, tail_n = 40, self.cfg.limits.max_output_lines // 4 or 60
        if length <= cap:
            raw = self._read_bytes(s, start, length)
            omitted = 0
        else:
            raw = self._read_bytes(s, start, cap // 4) + b"\n...\n" + self._read_bytes(s, stop - cap // 2, cap // 2)
            omitted = length - cap // 4 - cap // 2
        txt = raw.decode(errors="replace")
        if txt.startswith("\r\n"):
            txt = txt[2:]
        if txt.endswith("\r\n"):
            txt = txt[:-2]
        txt = clean_terminal(txt).rstrip("\n")
        txt, clipped = truncate_lines(txt, head=head_n, tail=tail_n)
        out = {"output": txt, "output_bytes": length, "log_range": [start, stop]}
        if omitted or clipped:
            out["truncated"] = True
            out["hint"] = ("output was truncated; read a specific part with interactive_read(log_offset=...) or "
                           "redirect large output to a file and inspect it with summaries (wc, head, grep -c)")
        return out

    # ------------------------------------------------------------ raw terminal
    def send(self, session_id: str, text: str | None, key: str | None, enter: bool) -> dict[str, Any]:
        with self.state.lock(f"session-{session_id}", timeout=10):
            s = self._ready_session(session_id)
            keys = []
            if key:
                if key not in SPECIAL_KEYS:
                    raise PolicyError(f"unsupported key {key!r}; allowed: {sorted(SPECIAL_KEYS)}")
                keys.append(key)
            if text is not None and enter:
                keys.append("Enter")
            off = self._send(s, text=text, keys=keys)
            if key in ("C-c", "C-\\") and s["pending_run"]:
                # An interrupt aborts the whole wrapper, so the end marker will never be printed.
                self.state.update_session(session_id, pending_run=None, pending_off=None)
        time.sleep(1.0)
        return {**self.read(session_id, tail_lines=30, log_offset=off), "sent": {"text": text, "key": key}}

    def read(self, session_id: str, tail_lines: int = 50, log_offset: int | None = None) -> dict[str, Any]:
        s = self._session(session_id)
        tail_lines = max(1, min(tail_lines, self.cfg.limits.max_output_lines))
        size = int(self.gate.run_ok(f"stat -c %s {q(s['log_path'])} 2>/dev/null || echo 0", "stat").strip() or 0)
        cap = self.cfg.limits.max_read_bytes
        start = max(0, size - cap) if log_offset is None else max(0, min(log_offset, size))
        stop = min(size, start + cap)
        raw = self._read_bytes(s, start, stop - start).decode(errors="replace")
        txt = clean_terminal(raw)
        # Drop the marker plumbing so the agent sees what a human would.
        txt = re.sub(r"printf '\\n__MCP_S_%s__\\n' \S+; \{ ", "", txt)
        txt = re.sub(r"^.*(__MCP_[SE]_[0-9a-f]+r\d+|__MCP_INIT_|\} </dev/null; printf |"
                     r"\[ -n \"\$BASH_VERSION\" \] \|\| exec bash -l).*\n?", "", txt, flags=re.M)
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        txt, _ = truncate_lines(txt.strip("\n"), head=0, tail=tail_lines)
        return {"session_id": session_id, "state": s["state"], "busy_with_run": s["pending_run"],
                "log_range": [start, stop], "log_size": size, "screen": txt}

    # ---------------------------------------------------------------- lifecycle
    def list(self) -> dict[str, Any]:
        live = self._reconcile()
        return {"sessions": [
            {"session_id": x["session_id"], "job_id": x["job_id"], "state": x["state"],
             "busy_with_run": x["pending_run"], "workdir": x["workdir"],
             "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(x["created_at"]))}
            for x in live]}

    def stop(self, session_id: str) -> dict[str, Any]:
        s = self._session(session_id)
        p = self._probe(s)
        job = p["job_id"]
        self.gate.run(f"""
{TMUX} send-keys -t {q(s['tmux_name'])} C-c 2>/dev/null
sleep 0.3
{TMUX} send-keys -t {q(s['tmux_name'])} -l 'exit' 2>/dev/null && {TMUX} send-keys -t {q(s['tmux_name'])} Enter 2>/dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do
  [ "$({TMUX} display -p -t {q(s['tmux_name'])} '#{{pane_dead}}' 2>/dev/null || echo 1)" = 1 ] && break
  sleep 0.5
done
{f"qstat {q(job)} >/dev/null 2>&1 && qdel {q(job)} 2>/dev/null" if job else ":"}
{TMUX} kill-session -t {q(s['tmux_name'])} 2>/dev/null
rm -rf {q(s['cmd_dir'])}
true
""")
        self.state.update_session(session_id, state="ended", pending_run=None, pending_off=None)
        return {"session_id": session_id, "job_id": job, "status": "stopped",
                "log_kept_at": s["log_path"]}
