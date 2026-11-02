"""PBS (OpenPBS / PBS Pro, as used by MetaCentrum): resources, qsub, qstat, qdel.

Guardrails live here, not in the prompt: resource caps, queue allowlist, cap on
concurrently active jobs, submission rate limit, idempotent submission by job name,
and cancellation only of jobs this server submitted.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from .config import LimitsConfig
from .errors import NotFound, PolicyError, RemoteCommandError
from .gate import Gate, q
from .state import TERMINAL_STATES
from .textutil import (
    clean_terminal,
    format_size_pbs,
    format_walltime,
    parse_size,
    parse_walltime,
    truncate_lines,
)

STATE_NAMES = {
    "Q": "queued", "H": "held", "R": "running", "S": "suspended", "E": "exiting",
    "F": "finished", "X": "finished (subjob)", "W": "waiting (start time / stage-in)",
    "B": "array job running", "M": "moved to another server", "T": "transiting", "U": "user-suspended",
}
JOB_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,62}$")
JOB_ID_RE = re.compile(r"^\d+(\[\d*\])?(\.[A-Za-z0-9.\-]+)?$")
SCRATCH_TYPES = {"local", "ssd", "shared", "shm"}


def validate_job_id(job_id: str) -> str:
    job_id = job_id.strip()
    if not JOB_ID_RE.match(job_id):
        raise PolicyError(f"{job_id!r} does not look like a PBS job id (e.g. 18411451.pbs-m1.metacentrum.cz)")
    return job_id


# --------------------------------------------------------------------------- resources
@dataclass
class Resources:
    ncpus: int = 1
    mem: str = "4gb"
    walltime: str = "01:00:00"
    scratch: str | None = "10gb"
    scratch_type: str = "local"
    ngpus: int = 0
    queue: str | None = None

    def validate(self, limits: LimitsConfig, interactive: bool = False, default_queue: str = "") -> dict[str, str]:
        problems = []
        try:
            wt = parse_walltime(self.walltime)
        except ValueError as e:
            problems.append(str(e))
            wt = 0
        max_wt = parse_walltime(limits.max_interactive_walltime if interactive else limits.max_walltime)
        if wt > max_wt:
            problems.append(f"walltime {format_walltime(wt)} exceeds the cap {format_walltime(max_wt)}")
        if not 1 <= self.ncpus <= limits.max_ncpus:
            problems.append(f"ncpus={self.ncpus} outside 1..{limits.max_ncpus}")
        try:
            mem = parse_size(self.mem)
            if mem > limits.max_mem_gb * 1024**3:
                problems.append(f"mem={self.mem} exceeds the cap {limits.max_mem_gb}gb")
            if mem < 64 * 1024**2:
                problems.append("mem must be at least 64mb")
        except ValueError as e:
            problems.append(str(e))
            mem = 0
        scratch = None
        if self.scratch:
            if self.scratch_type not in SCRATCH_TYPES:
                problems.append(f"scratch_type must be one of {sorted(SCRATCH_TYPES)}")
            try:
                scratch = parse_size(self.scratch)
                if scratch > limits.max_scratch_gb * 1024**3:
                    problems.append(f"scratch={self.scratch} exceeds the cap {limits.max_scratch_gb}gb")
            except ValueError as e:
                problems.append(str(e))
        if not 0 <= self.ngpus <= limits.max_ngpus:
            problems.append(
                f"ngpus={self.ngpus} not allowed (cap {limits.max_ngpus}; GPUs are disabled unless the config allows them)"
            )
        allowed = [q for q in [default_queue, *limits.allowed_queues] if q]
        if self.queue and self.queue not in allowed:
            problems.append(
                f"queue {self.queue!r} not allowed; omit it to use the default"
                + (f" ({default_queue})" if default_queue else " routing")
                + (f", or pick one of {allowed}" if allowed else "")
            )
        if problems:
            raise PolicyError("resource request refused: " + "; ".join(problems) +
                              ". Limits are set in the server config, not negotiable from the prompt.")
        select = f"select=1:ncpus={self.ncpus}:mem={format_size_pbs(mem)}"
        if scratch:
            select += f":scratch_{self.scratch_type}={format_size_pbs(scratch)}"
        if self.ngpus:
            select += f":ngpus={self.ngpus}"
        out = {"select": select, "walltime": format_walltime(wt)}
        if self.queue:
            out["queue"] = self.queue
        return out


# ------------------------------------------------------------------------------ qstat
def _parse_qstat_text(text: str) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    last_key = None
    for raw in text.splitlines():
        if raw.startswith("Job Id:"):
            cur = {}
            jobs[raw.split(":", 1)[1].strip()] = cur
            last_key = None
        elif cur is not None and raw.startswith("\t") and last_key:
            cur[last_key] += raw.strip()
        elif cur is not None and " = " in raw:
            k, v = raw.strip().split(" = ", 1)
            cur[k] = v
            last_key = k
    # Nest dotted resource keys like the JSON output does.
    for attrs in jobs.values():
        for k in [k for k in attrs if "." in k and k.split(".", 1)[0] in ("Resource_List", "resources_used")]:
            group, sub = k.split(".", 1)
            attrs.setdefault(group, {})[sub] = attrs.pop(k)
        if isinstance(attrs.get("Variable_List"), str):
            attrs["Variable_List"] = dict(
                kv.split("=", 1) for kv in attrs["Variable_List"].split(",") if "=" in kv
            )
    return jobs


def _strip_host(path: str | None) -> str | None:
    if not path:
        return None
    return path.split(":", 1)[1] if ":" in path and not path.startswith("/") else path


def _secs(v: Any) -> int | None:
    try:
        return parse_walltime(str(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _bytes(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return parse_size(str(v).lower())
    except ValueError:
        return None


def _workdir(var: Any) -> str | None:
    if not isinstance(var, dict):
        return None
    if var.get("PBS_O_WORKDIR"):
        return var["PBS_O_WORKDIR"]
    # PBS 23 JSON quirk: an empty value (e.g. PBS_O_INTERACTIVE_ENCRYPT_METHOD) swallows the
    # following ",PBS_O_WORKDIR=..." into itself.
    for v in var.values():
        m = re.search(r"(?:^|,)PBS_O_WORKDIR=([^,]+)", str(v))
        if m:
            return m.group(1)
    return None


def exit_meaning(code: int | None) -> str | None:
    if code is None:
        return None
    if code == 0:
        return "success"
    if code == 127:
        return "status 127: a command or script was not found (typo, missing `module add`/env, wrong path)"
    if code == 126:
        return "status 126: a command was found but is not executable (permissions?)"
    if 0 < code < 128:
        return f"the job script exited with status {code} (a command in the script failed)"
    if 128 <= code < 256:
        return f"a command was killed by signal {code - 128}"
    if code >= 256:
        sig = code - 256
        extra = " (SIGTERM: qdel, walltime or memory limit)" if sig == 15 else " (SIGKILL)" if sig == 9 else ""
        return f"PBS killed the job with signal {sig}{extra}"
    return "PBS could not start or had to abort the job (negative PBS code); read `comment` and the stderr file"


def normalize_job(job_id: str, a: dict[str, Any]) -> dict[str, Any]:
    state = a.get("job_state")
    req = a.get("Resource_List") or {}
    used = a.get("resources_used") or {}
    var = a.get("Variable_List") or {}
    exit_status = a.get("Exit_status")
    try:
        exit_status = int(exit_status) if exit_status is not None else None
    except (TypeError, ValueError):
        exit_status = None
    exec_host = a.get("exec_host2") or a.get("exec_host") or ""
    first_host = exec_host.split("+")[0].split("/")[0].split(":")[0] if exec_host else None
    info = {
        "job_id": job_id,
        "name": a.get("Job_Name"),
        "state": state,
        "state_desc": STATE_NAMES.get(state, state),
        "finished": state in TERMINAL_STATES,
        "exit_status": exit_status,
        "exit_meaning": exit_meaning(exit_status) if state in TERMINAL_STATES else None,
        "queue": a.get("queue"),
        "exec_host": first_host,
        "comment": a.get("comment"),
        "requested": {k: req.get(k) for k in ("select", "ncpus", "mem", "walltime", "ngpus") if req.get(k)},
        "used": {k: used.get(k) for k in ("walltime", "cput", "mem", "ncpus", "cpupercent") if used.get(k) is not None},
        "times": {k: a.get(src) for k, src in
                  (("queued", "qtime"), ("started", "stime"), ("finished", "obittime")) if a.get(src)},
        "workdir": _workdir(var),
        "stdout_path": _strip_host(a.get("Output_Path")),
        "stderr_path": _strip_host(a.get("Error_Path")),
    }
    # Explain the usual suspects for a non-zero exit instead of leaving the agent to guess.
    if info["finished"] and exit_status not in (0, None):
        hints = []
        rw, uw = _secs(req.get("walltime")), _secs(used.get("walltime"))
        if rw and uw and uw >= rw - 60:
            hints.append("walltime limit reached: request more walltime")
        rm, um = _bytes(req.get("mem")), _bytes(used.get("mem"))
        if rm and um and um >= 0.95 * rm:
            hints.append("memory use reached the requested limit: request more mem")
        if hints:
            info["likely_cause"] = "; ".join(hints)
    return info


# ---------------------------------------------------------------------------- service
class PBS:
    def __init__(self, gate: Gate):
        self.gate = gate
        self.cfg = gate.cfg
        self.state = gate.state

    # ---- qstat
    def qstat(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Full info for the given ids (finished jobs included).

        Ids PBS reports as unknown are listed in the result under the key "__unknown__"; ids that
        are merely absent (e.g. a transient error) are not, so callers never conclude "finished"
        from silence.
        """
        if not job_ids:
            return {}
        # PBS 23: `qstat -F json id1 id2` prints only the first job, so query ids one by one
        # (still a single SSH round trip). Records are separated by an ASCII record separator.
        ids = " ".join(q(j) for j in job_ids)
        r = self.gate.run(f"for id in {ids}; do qstat -x -f -F json \"$id\"; printf '\\036\\n'; done")
        jobs: dict[str, dict[str, Any]] = {}
        failed_parse = []
        for jid, chunk in zip(job_ids, r.stdout.split("\x1e\n")):
            if not chunk.strip():
                continue
            try:
                jobs.update(json.loads(chunk, strict=False).get("Jobs", {}))
            except json.JSONDecodeError:
                failed_parse.append(jid)
        if failed_parse:
            r2 = self.gate.run("qstat -x -f " + " ".join(q(j) for j in failed_parse))
            jobs.update(_parse_qstat_text(r2.stdout))
        unknown = set(re.findall(r"Unknown Job Id (\S+)", r.stderr))
        if not jobs and not unknown and r.rc != 0:
            raise RemoteCommandError(f"qstat failed: {r.stderr.strip()[-500:]}")
        out: dict[str, Any] = {jid: normalize_job(jid, attrs) for jid, attrs in jobs.items()}
        if unknown:
            out["__unknown__"] = unknown
        return out

    def _match(self, infos: dict[str, Any], job_id: str) -> dict | None:
        if job_id in infos:
            return infos[job_id]
        num = job_id.split(".")[0]
        for jid, info in infos.items():
            if jid != "__unknown__" and jid.split(".")[0] == num:
                return info
        return None

    @staticmethod
    def _is_unknown(infos: dict[str, Any], job_id: str) -> bool:
        num = job_id.split(".")[0]
        return any(u.split(".")[0] == num for u in infos.get("__unknown__", ()))

    def _refresh(self, rows: list[dict]) -> dict[str, dict]:
        """qstat the registry rows in one call and persist their state."""
        if not rows:
            return {}
        infos = self.qstat([r["job_id"] for r in rows])
        now = time.time()
        for row in rows:
            info = self._match(infos, row["job_id"])
            if info:
                self.state.update_job(row["job_id"], last_state=info["state"],
                                      exit_status=info["exit_status"], last_checked=now)
            elif row["last_state"] not in TERMINAL_STATES and self._is_unknown(infos, row["job_id"]):
                # PBS says it no longer knows the job (history is kept ~24h): finished, outcome unknown.
                self.state.update_job(row["job_id"], last_state="F", last_checked=now)
        return infos

    def active_jobs(self) -> list[dict]:
        rows = self.state.jobs(self.gate.target, active_only=True)
        self._refresh(rows)
        return self.state.jobs(self.gate.target, active_only=True)

    # ---- submit
    def submit(self, name: str, script: str, resources: Resources, workdir: str | None,
               depends_on: list[str] | None, strict: bool, resubmit: bool, client: str | None,
               use_software_env: bool = True) -> dict[str, Any]:
        lim = self.cfg.limits
        if not JOB_NAME_RE.match(name):
            raise PolicyError("job name must start with a letter and contain only letters, digits, '_', '-', '.' "
                              "(max 63 chars). The name is also the idempotency key.")
        body = script.replace("\r\n", "\n")
        if body.startswith("#!"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        if re.search(r"^\s*#PBS", body, re.M):
            raise PolicyError("the script body must not contain #PBS directives; pass resources as tool "
                              "parameters (ncpus, mem, walltime, scratch, ...) so the caps can be enforced.")
        if not body.strip():
            raise PolicyError("the job script is empty")
        if len(body.encode()) > 256 * 1024:
            raise PolicyError("job script larger than 256 KB; put the logic in a script file in the workspace "
                              "and call it from a short job script")
        res = resources.validate(lim, default_queue=self.cfg.defaults.queue)
        deps = [validate_job_id(d) for d in depends_on or []]
        wd = self.gate.resolve(workdir or ".", write=True)
        target = self.gate.target

        with self.state.lock(f"submit-{self.gate.transport.key}"):
            prev = self.state.latest_job_by_name(target, name)
            if prev:
                self._refresh([prev])
                prev = self.state.get_job(prev["job_id"])
                if prev["last_state"] not in TERMINAL_STATES and not prev["cancelled"]:
                    return {"status": "already_submitted", "job_id": prev["job_id"], "name": name,
                            "state": prev["last_state"], "workdir": prev["workdir"],
                            "note": "A job with this name is already queued/running; returning its id instead of "
                                    "submitting a duplicate. Use job_status / wait_for_job."}
                if prev["exit_status"] == 0 and not prev["cancelled"] and not resubmit:
                    return {"status": "already_completed", "job_id": prev["job_id"], "name": name,
                            "workdir": prev["workdir"],
                            "note": "A job with this name already finished successfully. Pass resubmit=true "
                                    "to run it again, or use a new name."}

            hour_ago = time.time() - 3600
            if self.state.submissions_since(target, hour_ago) >= lim.max_submissions_per_hour:
                raise PolicyError(f"submission rate limit reached ({lim.max_submissions_per_hour}/hour)")
            active = self.active_jobs()
            if len(active) >= lim.max_active_jobs:
                raise PolicyError(
                    f"{len(active)} jobs submitted through this server are still queued/running "
                    f"(cap {lim.max_active_jobs}): {[a['job_id'] for a in active]}. Wait for one to finish "
                    "(wait_for_job) or cancel one before submitting more.")

            stamp = time.strftime("%Y%m%d-%H%M%S")
            script_path = f"{wd}/.mcp/jobs/{name}.{stamp}.pbs"
            text = self._render(name, body, res, wd, deps, strict, script_path, use_software_env)
            b64 = base64.b64encode(text.encode()).decode()
            remote = f"""
set -e
mkdir -p {q(wd + '/.mcp/jobs')}
printf %s {q(b64)} | base64 -d > {q(script_path)}
cd {q(wd)}
qsub {q(script_path)}
"""
            # Never retry qsub blindly: a dropped connection could otherwise submit twice.
            r = self.gate.run(remote, retry=False)
            jid = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
            if r.rc != 0 or not JOB_ID_RE.match(jid):
                raise RemoteCommandError(f"qsub failed (exit {r.rc}): {(r.stderr or r.stdout).strip()[-1500:]}")
            self.state.add_job(job_id=jid, target=target, name=name, workdir=wd, script_path=script_path,
                               resources={**asdict(resources), **res}, last_state="Q", client=client)
        seq = jid.split(".")[0]
        return {
            "status": "submitted", "job_id": jid, "name": name, "workdir": wd, "script_path": script_path,
            "resources": res, "depends_on": deps,
            "expected_stdout": f"{wd}/{name}.o{seq}", "expected_stderr": f"{wd}/{name}.e{seq}",
            "next": "Record the job id in PROGRESS.md, then use wait_for_job / job_status; do not poll in a loop.",
        }

    def _render(self, name: str, body: str, res: dict[str, str], wd: str, deps: list[str],
                strict: bool, script_path: str, use_software_env: bool = True) -> str:
        lines = ["#!/bin/bash", f"#PBS -N {name}", f"#PBS -l {res['select']}", f"#PBS -l walltime={res['walltime']}"]
        if "queue" in res:
            lines.append(f"#PBS -q {res['queue']}")
        if deps:
            lines.append(f"#PBS -W depend=afterok:{':'.join(deps)}")
        lines += [
            f"# Generated by metacentrum-mcp {time.strftime('%Y-%m-%d %H:%M:%S')}.",
            f"# Rerun without any agent:  cd {wd} && qsub {script_path}",
            "",
            "set -o pipefail" + ("; set -e" if strict else ""),
            # MetaCentrum asks jobs to clean their scratch; this runs on normal exit and on qdel/walltime kill.
            'if [ -n "${SCRATCHDIR:-}" ]; then trap \'clean_scratch\' TERM EXIT; fi',
            f"export WORKDIR={q(wd)}",
            'cd "$WORKDIR"',
            'echo "[mcp] job $PBS_JOBID on $(hostname) start $(date -Is) scratch=${SCRATCHDIR:-none}" >&2',
        ]
        senv = self.cfg.defaults.software_env
        if use_software_env and senv:
            lines.append(f"if [ -f {q(senv)} ]; then source {q(senv)}; fi  # shared software environment")
        lines += [
            "",
            "# ---- job body ----",
            body.rstrip("\n"),
            "",
        ]
        return "\n".join(lines)

    # ---- status / wait / output / cancel
    def status(self, job_id: str) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        row = self.state.find_job(self.gate.target, job_id)
        infos = self.qstat([row["job_id"] if row else job_id])
        info = self._match(infos, row["job_id"] if row else job_id)
        if info is None:
            if not self._is_unknown(infos, row["job_id"] if row else job_id):
                raise RemoteCommandError(f"qstat returned no information for {job_id}; retry shortly")
            if row:
                self.state.update_job(row["job_id"], last_state="F", last_checked=time.time())
                return {"job_id": row["job_id"], "name": row["name"], "state": row["last_state"] or "?",
                        "finished": True, "exit_status": row["exit_status"],
                        "exit_meaning": exit_meaning(row["exit_status"]),
                        "note": "The job is no longer known to PBS (finished jobs are kept ~24h); this is the "
                                "last recorded state. Output files remain in the workdir.",
                        "workdir": row["workdir"], "submitted_by_this_server": True}
            raise NotFound(f"PBS does not know job {job_id} (finished jobs disappear after ~24h)")
        if row:
            self.state.update_job(row["job_id"], last_state=info["state"], exit_status=info["exit_status"],
                                  last_checked=time.time())
        info["submitted_by_this_server"] = bool(row)
        return info

    def wait(self, job_id: str, timeout: int) -> dict[str, Any]:
        cap = self.cfg.limits.max_wait_seconds
        timeout = max(1, min(timeout, cap))
        start = time.monotonic()
        delay = 2.0
        while True:
            info = self.status(job_id)
            waited = round(time.monotonic() - start)
            if info.get("finished"):
                return {**info, "waited_seconds": waited}
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return {**info, "waited_seconds": waited, "still_waiting": True,
                        "note": f"not finished after {waited}s (max {cap}s per call). Call wait_for_job again "
                                "later, or do other work meanwhile; the job does not need the agent."}
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.6, 30)

    def output(self, job_id: str, stream: str, lines: int, pattern: str | None) -> dict[str, Any]:
        if stream not in ("stdout", "stderr"):
            raise PolicyError("stream must be 'stdout' or 'stderr'")
        lines = max(1, min(lines, self.cfg.limits.max_output_lines))
        info = self.status(job_id)
        jid = info["job_id"]
        if info.get("state") in ("Q", "H", "W", "T"):
            return {"job_id": jid, "state": info["state"], "output": "", "note": "job has not started yet"}
        filt = ""
        if pattern:
            filt = f" | grep -n -a -E -e {q(pattern)}"
        if info.get("finished"):
            path = info.get(f"{stream}_path")
            if not path:
                row = self.state.find_job(self.gate.target, jid)
                if not row:
                    raise NotFound("no output path known for this job")
                path = f"{row['workdir']}/{row['name']}.{'o' if stream == 'stdout' else 'e'}{jid.split('.')[0]}"
            path = self.gate.resolve(path)
            src = f"cat -- {q(path)}"
            where = path
            script = f"""
if [ ! -e {q(path)} ]; then echo "__MISSING__"; exit 0; fi
echo "__SIZE__ $(stat -c %s {q(path)})"
{src}{filt} | tail -n {lines}
"""
        else:
            host = info.get("exec_host")
            if not host:
                return {"job_id": jid, "state": info["state"], "output": "", "note": "no execution host yet"}
            spool = f"/var/spool/pbs/spool/{jid}.{'OU' if stream == 'stdout' else 'ER'}"
            where = f"{host}:{spool}"
            # Live output sits in the MOM spool on the compute node until the job ends.
            script = f"""
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {q(host)} \
  'f={spool}; if [ ! -e "$f" ]; then echo __MISSING__; exit 0; fi; echo "__SIZE__ $(stat -c %s "$f")"; cat "$f"{filt.replace("'", "'\\''")} | tail -n {lines}'
"""
        r = self.gate.run(script, timeout=60)
        out = r.stdout
        if "__MISSING__" in out:
            note = ("output file not found yet; PBS copies it to the workdir when the job ends (may lag a few seconds)"
                    if info.get("finished") else "live output not available yet")
            return {"job_id": jid, "state": info["state"], "output": "", "source": where, "note": note}
        if r.rc != 0 and "__SIZE__" not in out:
            return {"job_id": jid, "state": info["state"], "output": "", "source": where,
                    "note": "could not read live output from the compute node (needs a Kerberos ticket on the "
                            f"frontend); it will be in the workdir once the job ends. {r.stderr.strip()[-300:]}"}
        size = None
        if out.startswith("__SIZE__"):
            first, _, out = out.partition("\n")
            size = int(first.split()[1])
        text, clipped = truncate_lines(clean_terminal(out.rstrip("\n")), head=0, tail=lines)
        return {"job_id": jid, "state": info["state"], "stream": stream, "source": where,
                "file_bytes": size, "lines_returned": text.count("\n") + 1 if text else 0,
                "filtered_by": pattern, "output": text,
                "note": f"last {lines} lines" + (" (filtered)" if pattern else "") +
                        "; use read_file with offsets for a specific region"}

    def cancel(self, job_id: str) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        row = self.state.find_job(self.gate.target, job_id)
        if not row:
            raise PolicyError(f"job {job_id} was not submitted through this MCP server; refusing to cancel it")
        info = self._match(self.qstat([row["job_id"]]), row["job_id"])
        if info and info["finished"]:
            return {"job_id": row["job_id"], "status": "already_finished", "exit_status": info["exit_status"]}
        r = self.gate.run(f"qdel {q(row['job_id'])}", retry=True)
        if r.rc != 0 and "Unknown Job Id" not in r.stderr and "finished" not in r.stderr.lower():
            raise RemoteCommandError(f"qdel failed: {r.stderr.strip()[-500:]}")
        self.state.update_job(row["job_id"], cancelled=1, last_checked=time.time())
        return {"job_id": row["job_id"], "status": "cancel_requested",
                "note": "PBS is stopping the job; job_status will show F with a non-zero exit status."}

    def queue_info(self, queue: str) -> dict[str, Any]:
        """Limits and load of a queue and, for a routing queue, of its destinations."""
        name, _, server = queue.partition("@")
        suffix = f"@{server}" if server else ""
        out = self.gate.run(f"qstat -Qf {q(queue)} 2>&1").stdout
        attrs = self._parse_queues(out).get(name, {})
        info: dict[str, Any] = {"queue": queue, "type": attrs.get("queue_type"),
                                "max_walltime": attrs.get("resources_max.walltime"),
                                "default_walltime": attrs.get("resources_default.walltime"),
                                "groups": attrs.get("acl_groups")}
        dests = [d for d in (attrs.get("route_destinations") or "").split(",") if d]
        if dests:
            out = self.gate.run("qstat -Qf " + " ".join(q(d + suffix) for d in dests) + " 2>&1").stdout
            parsed = self._parse_queues(out)
            info["routes_to"] = [{
                "queue": d, "walltime": f"{parsed.get(d, {}).get('resources_min.walltime', '0')}"
                                        f"..{parsed.get(d, {}).get('resources_max.walltime', '?')}",
                "load": parsed.get(d, {}).get("state_count", ""),
            } for d in dests]
        else:
            info["load"] = attrs.get("state_count")
        return info

    @staticmethod
    def _parse_queues(text: str) -> dict[str, dict[str, str]]:
        queues: dict[str, dict[str, str]] = {}
        cur: dict[str, str] | None = None
        for line in text.splitlines():
            if line.startswith("Queue: "):
                cur = queues.setdefault(line[7:].strip().split("@")[0], {})
            elif cur is not None and " = " in line:
                k, v = line.strip().split(" = ", 1)
                cur[k] = v
        for qd in queues.values():
            sc = qd.get("state_count")
            if sc:
                pairs = [kv.split(":", 1) for kv in sc.split() if ":" in kv]
                qd["state_count"] = ", ".join(f"{k.lower()} {v}" for k, v in pairs if k in ("Queued", "Running", "Held"))
        return queues

    def list(self, scope: str, include_finished: bool) -> dict[str, Any]:
        if scope == "server":
            rows = self.state.jobs(self.gate.target, limit=200)
            if not include_finished:
                rows = [r for r in rows if r["last_state"] not in TERMINAL_STATES]
            infos = self._refresh([r for r in rows if r["last_state"] not in TERMINAL_STATES])
            jobs = []
            for row in rows[:50]:
                info = self._match(infos, row["job_id"])
                latest = self.state.get_job(row["job_id"])
                jobs.append({
                    "job_id": row["job_id"], "name": row["name"],
                    "state": (info or latest)["state" if info else "last_state"],
                    "exit_status": (info or {}).get("exit_status", latest["exit_status"]),
                    "submitted": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["submitted_at"])),
                    "workdir": row["workdir"], "cancelled": bool(row["cancelled"]),
                })
            if not include_finished:
                jobs = [j for j in jobs if j["state"] not in TERMINAL_STATES]
            return {"scope": "jobs submitted through this MCP server", "count": len(jobs), "jobs": jobs}
        if scope != "user":
            raise PolicyError("scope must be 'server' or 'user'")
        user = self.gate.facts().get("user")
        r = self.gate.run(f"qstat -w {'-x ' if include_finished else ''}-u {q(user)}")
        jobs = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 11 and JOB_ID_RE.match(parts[0]):
                jobs.append({"job_id": parts[0], "queue": parts[2], "name": parts[3], "req_mem": parts[7],
                             "req_time": parts[8], "state": parts[9], "elapsed": parts[10]})
        return {"scope": f"all PBS jobs of {user}", "count": len(jobs), "jobs": jobs[-100:]}
