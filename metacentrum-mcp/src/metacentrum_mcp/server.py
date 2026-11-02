"""MCP server: a narrow, guarded door from an agent harness to MetaCentrum (PBS).

The model only ever sees tool names and bounded, structured results. Credentials
stay with the user's SSH setup on this machine; every call is written to the
audit log (~/.local/state/metacentrum-mcp/audit.jsonl).
"""

import functools
import inspect
import json
import os
import time
import traceback
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import DefaultsConfig, load_config
from .errors import McpGridError
from .files import Files
from .gate import Gate, q
from .interactive import Interactive
from .pbs import PBS, Resources
from .state import State

INSTRUCTIONS = """\
Gateway to the MetaCentrum grid (PBS scheduler) for research computing.

Ground rules:
- The frontend (login node) is shared: never compute there. Explore and debug inside an interactive job
  (interactive_start -> interactive_run). Long or heavy work goes to a batch job (submit_job).
- All data, scripts and job directories live in the project workspace returned by `connect`
  (large shared storage). Relative paths resolve against it. Home has a small quota and is read-only here.
- In job scripts, do heavy I/O in $SCRATCHDIR (request `scratch`), copy results back to $WORKDIR.
  Software: `module add <name>` (find it with frontend_exec "module avail <name>").
- Results are truncated on purpose. Read summaries (samtools flagstat, bcftools stats, wc -l, grep -c),
  never whole SAM/VCF/FASTQ files or huge logs.
- Use wait_for_job instead of polling job_status in a loop. Record job ids in PROGRESS.md: jobs outlive
  this session. submit_job is idempotent by job name.
- Resource caps, queue allowlist and job-count limits are enforced by the server; a refusal is final for
  this session, so adjust the request instead of retrying.
"""


def _resources(d: DefaultsConfig, ncpus: int | None, mem: str | None, walltime: str | None,
               scratch: str | None, scratch_type: str | None, ngpus: int | None, queue: str | None,
               interactive: bool = False) -> Resources:
    """Fill unspecified parameters from [defaults]. scratch="none"/"0" disables scratch."""
    if scratch is None:
        scratch = d.scratch
    elif scratch.strip().lower() in ("", "0", "none", "no"):
        scratch = None
    return Resources(
        ncpus=ncpus or d.ncpus, mem=mem or d.mem,
        walltime=walltime or (d.interactive_walltime if interactive else d.walltime),
        scratch=scratch or None, scratch_type=scratch_type or d.scratch_type,
        ngpus=ngpus or 0, queue=queue or d.queue or None,
    )


def build_server(config_path: str | None = None) -> MCPServer:
    cfg = load_config(config_path)
    state = State(cfg.state_dir)
    gate = Gate(cfg, state)
    pbs = PBS(gate)
    inter = Interactive(gate)
    files = Files(gate)
    client = os.environ.get("METACENTRUM_MCP_CLIENT") or f"pid{os.getpid()}"
    # Refusals are expected traffic and already in the audit log; keep stderr for real problems.
    mcp = MCPServer("metacentrum", instructions=INSTRUCTIONS, version="0.2.0", log_level="WARNING")

    def tool(read_only: bool = False, destructive: bool = False, idempotent: bool = False):
        """Register a tool: JSON text result, McpGridError -> ToolError, audit every call."""
        def deco(fn):
            sig = inspect.signature(fn, eval_str=True)

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                t0 = time.monotonic()
                entry: dict[str, Any] = {"tool": fn.__name__, "client": client, "target": gate.target,
                                         "args": {k: (f"<{len(v)} chars>" if k in ("content", "script", "command")
                                                      and isinstance(v, str) and len(v) > 300 else v)
                                                  for k, v in kwargs.items()}}
                try:
                    result = fn(*args, **kwargs)
                    entry["ok"] = True
                    for k in ("job_id", "status", "session_id", "exit_code", "state"):
                        if isinstance(result, dict) and k in result:
                            entry.setdefault("result", {})[k] = result[k]
                    return json.dumps(result, indent=1, ensure_ascii=False, default=str)
                except McpGridError as e:
                    entry.update(ok=False, error=str(e))
                    raise ToolError(f"{type(e).__name__}: {e}") from e
                except Exception as e:  # unexpected: keep the server alive, report briefly
                    entry.update(ok=False, error=repr(e), trace=traceback.format_exc()[-2000:])
                    raise ToolError(f"internal error in {fn.__name__}: {e!r}") from e
                finally:
                    entry["ms"] = round((time.monotonic() - t0) * 1000)
                    try:
                        state.audit(entry)
                    except OSError:
                        pass

            wrapper.__signature__ = sig.replace(return_annotation=str)
            mcp.add_tool(wrapper, name=fn.__name__, description=inspect.getdoc(fn),
                         annotations=ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive,
                                                     idempotent_hint=idempotent, open_world_hint=True),
                         structured_output=False)
            return fn
        return deco

    # Shared parameter types --------------------------------------------------------------------
    d = cfg.defaults
    Ncpus = Annotated[int | None, Field(description=f"CPU cores (default {d.ncpus})", ge=1)]
    Mem = Annotated[str | None, Field(description=f"memory, e.g. '8gb' or '500mb' (default {d.mem})")]
    Walltime = Annotated[str | None, Field(description="max run time HH:MM:SS (default "
                                                       f"{d.walltime} batch / {d.interactive_walltime} interactive)")]
    Scratch = Annotated[str | None, Field(description=f"scratch space available as $SCRATCHDIR, e.g. '20gb' "
                                                      f"(default {d.scratch or 'none'}; 'none' to disable)")]
    ScratchType = Annotated[Literal["local", "ssd", "shared", "shm"] | None,
                            Field(description=f"scratch kind (default {d.scratch_type})")]
    Ngpus = Annotated[int | None, Field(description="GPUs (usually disabled by config)", ge=0)]
    Queue = Annotated[str | None, Field(description=f"PBS queue (default {d.queue or 'MetaCentrum routing'}); "
                                                    "usually leave unset")]
    UseEnv = Annotated[bool, Field(description="source the shared software environment first "
                                               "(see cluster_info)")]
    JobId = Annotated[str, Field(description="PBS job id, e.g. 18411451.pbs-m1.metacentrum.cz (numeric part is enough)")]
    SessionId = Annotated[str, Field(description="interactive session id from interactive_start")]
    Wait = Annotated[int, Field(description="seconds to block (capped by the server)", ge=0)]

    # ------------------------------------------------------------------ connection
    @tool(idempotent=True)
    def connect(host: Annotated[str | None, Field(description="frontend hostname; omit for the configured default")] = None,
                user: Annotated[str | None, Field(description="grid username; omit for the configured default")] = None) -> dict:
        """Open (or reuse) the persistent SSH connection to a MetaCentrum frontend and prepare the project
        workspace. Safe to call repeatedly: an existing connection, possibly opened by another agent, is reused.
        Returns the workspace path, Kerberos status and warnings. Call this first."""
        res = gate.connect(host, user)
        res["next"] = "Call cluster_info once for defaults, limits and the shared software environment."
        return res

    @tool(read_only=True, idempotent=True)
    def connection_status() -> dict:
        """Report whether the SSH connection is up, Kerberos ticket validity, workspace, open interactive
        sessions and jobs still queued/running. Does not open a connection."""
        alive = gate.transport.master_alive()
        res: dict[str, Any] = {"target": gate.target, "connected": alive,
                               "explicitly_disconnected": gate.transport.disconnect_flag.exists()}
        if alive:
            res["master_pid"] = gate.transport.master_pid()
            res.update(gate.summary(gate.probe()))
            res["interactive_sessions"] = inter.list()["sessions"]
            res["active_jobs"] = [{"job_id": j["job_id"], "name": j["name"], "state": j["last_state"]}
                                  for j in pbs.active_jobs()]
        else:
            res["note"] = "not connected; call connect"
            fail = gate.transport.last_failure()
            if fail:
                res["last_login_failure"] = {**fail, "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(fail["at"]))}
                if fail["kind"] == "auth":
                    res["note"] = "login was rejected; the user must run `metacentrum-mcp login` in a terminal"
        return res

    @tool(read_only=True, idempotent=True)
    def cluster_info() -> dict:
        """Everything worth knowing before working on the grid: the project workspace (relative paths
        resolve there), default resources and queue used when you omit them, hard limits, the shared
        software environment (tools available in jobs without installing anything), queue load,
        Kerberos and site notes. Call once after `connect`."""
        facts = gate.probe()
        summary = gate.summary(facts)
        senv = d.software_env
        env_info: dict[str, Any] = {"activate_script": senv or None, "available": facts["software_env_ok"]}
        if senv and facts["software_env_ok"]:
            env_dir = senv.rsplit("/", 1)[0]
            r = gate.run(f"head -c 6000 {q(env_dir + '/TOOLS.txt')} 2>/dev/null")
            env_info["sourced_automatically"] = ("yes, at the start of every batch job and interactive shell "
                                                 "(pass use_software_env=false to skip)")
            env_info["tools"] = r.stdout.strip().splitlines() or "see " + env_dir
            env_info["env_file"] = env_dir + "/environment.yml"
        elif senv:
            env_info["note"] = "not installed yet; use `module avail <tool>` / `module add <tool>` instead"
        return {
            **summary,
            "workspace_rules": {
                "relative_paths_resolve_to": summary["workspace"],
                "writable": gate._roots(write=True),
                "readable": gate._roots(write=False),
                "job_scripts_saved_to": "<workdir>/.mcp/jobs/",
                "interactive_logs": f"{summary['workspace']}/.mcp/interactive/",
            },
            "defaults": {
                "queue": d.queue or "default (MetaCentrum routing by walltime)",
                "ncpus": d.ncpus, "mem": d.mem, "walltime": d.walltime,
                "interactive_walltime": d.interactive_walltime,
                "scratch": f"{d.scratch} ({d.scratch_type}) as $SCRATCHDIR" if d.scratch else None,
                "workdir": summary["workspace"],
            },
            "limits": {
                "max_walltime": cfg.limits.max_walltime, "max_interactive_walltime": cfg.limits.max_interactive_walltime,
                "max_ncpus": cfg.limits.max_ncpus, "max_mem_gb": cfg.limits.max_mem_gb,
                "max_scratch_gb": cfg.limits.max_scratch_gb, "max_ngpus": cfg.limits.max_ngpus,
                "queues": [x for x in [d.queue, *cfg.limits.allowed_queues] if x] or ["default routing only"],
                "max_active_jobs": cfg.limits.max_active_jobs,
                "max_interactive_sessions": cfg.limits.max_interactive_sessions,
                "max_wait_seconds_per_call": cfg.limits.max_wait_seconds,
            },
            "software_env": env_info,
            "queue": pbs.queue_info(d.queue) if d.queue else None,
            "notes": [
                "The frontend is shared: compute only in jobs (interactive_run or submit_job).",
                "Heavy I/O in $SCRATCHDIR, then copy results to $WORKDIR (the job's workdir).",
                "Software outside the shared env: `module avail <name>` (frontend_exec), `module add` in jobs.",
                *d.notes,
            ],
        }

    @tool(destructive=True, idempotent=True)
    def disconnect(stop_interactive_sessions: Annotated[bool, Field(
            description="also end open interactive jobs (otherwise they keep running in tmux on the frontend)")] = False) -> dict:
        """Close the shared SSH connection (for every agent on this machine). Batch jobs keep running.
        Other agents will not silently reconnect until someone calls `connect` again."""
        stopped = []
        if stop_interactive_sessions and gate.transport.master_alive():
            for s in inter.list()["sessions"]:
                stopped.append(inter.stop(s["session_id"])["session_id"])
        was_open = gate.disconnect()
        return {"disconnected": True, "was_connected": was_open, "stopped_sessions": stopped,
                "note": "batch jobs continue on the grid; reconnect later to check them"}

    # ------------------------------------------------------------------------ batch
    @tool()
    def submit_job(
        name: Annotated[str, Field(description="job name; also the idempotency key (same name -> same job)")],
        script: Annotated[str, Field(description="bash job body WITHOUT #PBS lines; runs in `workdir` ($WORKDIR). "
                                                 "Use module add, $SCRATCHDIR for heavy I/O, copy results back.")],
        workdir: Annotated[str | None, Field(description="job directory (relative = inside the workspace); "
                                                         "stdout/stderr files land here")] = None,
        ncpus: Ncpus = None, mem: Mem = None, walltime: Walltime = None, scratch: Scratch = None,
        scratch_type: ScratchType = None, ngpus: Ngpus = None, queue: Queue = None,
        use_software_env: UseEnv = True,
        depends_on: Annotated[list[str] | None, Field(description="start only after these jobs succeed (afterok)")] = None,
        strict: Annotated[bool, Field(description="add `set -e -o pipefail` so any failing command fails the job")] = True,
        resubmit: Annotated[bool, Field(description="run again even if a job with this name already succeeded")] = False,
    ) -> dict:
        """Submit a PBS batch job. The server writes the script (with #PBS resource lines it generated and
        validated against caps) to <workdir>/.mcp/jobs/ and runs qsub. Returns the job id immediately.
        If a job with the same name is queued/running (or already succeeded), its id is returned instead
        of submitting a duplicate."""
        res = _resources(d, ncpus, mem, walltime, scratch, scratch_type, ngpus, queue)
        return pbs.submit(name, script, res, workdir, depends_on, strict, resubmit, client, use_software_env)

    @tool(read_only=True, idempotent=True)
    def job_status(job_id: JobId) -> dict:
        """Structured status of one job: state, exit status with meaning, resources requested/used,
        execution host, output file paths, scheduler comment (e.g. why it is still queued)."""
        return pbs.status(job_id)

    @tool(read_only=True, idempotent=True)
    def wait_for_job(job_id: JobId, timeout_seconds: Wait = 20) -> dict:
        """Block until the job finishes or the timeout passes, then return its status. Use this instead of
        calling job_status in a loop. If `still_waiting` is true, call again later."""
        return pbs.wait(job_id, timeout_seconds)

    @tool(read_only=True, idempotent=True)
    def job_output(job_id: JobId,
                   stream: Annotated[Literal["stdout", "stderr"], Field(description="which stream")] = "stdout",
                   lines: Annotated[int, Field(description="tail length", ge=1)] = 50,
                   pattern: Annotated[str | None, Field(description="only lines matching this extended regex, "
                                                                    "e.g. 'error|warn' (case-sensitive)")] = None) -> dict:
        """Last lines of a job's stdout/stderr (live from the compute node while running, from the workdir
        once finished). Check stderr first when a job fails."""
        return pbs.output(job_id, stream, lines, pattern)

    @tool(destructive=True, idempotent=True)
    def cancel_job(job_id: JobId) -> dict:
        """Cancel (qdel) a queued or running job. Only jobs submitted through this server can be cancelled."""
        return pbs.cancel(job_id)

    @tool(read_only=True, idempotent=True)
    def list_jobs(scope: Annotated[Literal["server", "user"], Field(
                      description="'server': jobs submitted through this MCP server; 'user': all PBS jobs of the user")] = "server",
                  include_finished: bool = False) -> dict:
        """List jobs with their current state."""
        return pbs.list(scope, include_finished)

    # ------------------------------------------------------------------ interactive
    @tool()
    def interactive_start(
        ncpus: Ncpus = None, mem: Mem = None, walltime: Walltime = None, scratch: Scratch = None,
        scratch_type: ScratchType = None, ngpus: Ngpus = None, queue: Queue = None,
        name: Annotated[str, Field(description="PBS job name")] = "mcp_interactive",
        use_software_env: UseEnv = True,
        wait_seconds: Wait = 20,
    ) -> dict:
        """Start an interactive PBS job (qsub -I) bridged as a persistent terminal. Returns a session id.
        The job may wait in the queue: check with interactive_status. Commands then run on the compute node
        via interactive_run. The session survives agent restarts; stop it when done (it holds resources)."""
        res = _resources(d, ncpus, mem, walltime, scratch, scratch_type, ngpus, queue, interactive=True)
        return inter.start(res, name, wait_seconds, use_software_env)

    @tool(read_only=True, idempotent=True)
    def interactive_status(session_id: SessionId, wait_seconds: Wait = 0) -> dict:
        """State of an interactive session (starting / queued / ready / ended); optionally wait for it to
        become ready."""
        return inter.status(session_id, wait_seconds)

    @tool()
    def interactive_run(session_id: SessionId,
                        command: Annotated[str, Field(description="bash command(s) to run on the compute node")],
                        timeout_seconds: Wait = 20) -> dict:
        """Run a command in the interactive job's shell (on the compute node, never the frontend) and return
        its exit code and (truncated) output. Shell state (cwd, env, loaded modules) persists between calls.
        Stdin is /dev/null. If it is still running at the timeout, collect it later with interactive_wait."""
        return inter.run(session_id, command, timeout_seconds)

    @tool(read_only=True)
    def interactive_wait(session_id: SessionId, timeout_seconds: Wait = 20) -> dict:
        """Wait for the command still running in a session (after interactive_run timed out) and return
        its result."""
        return inter.wait(session_id, timeout_seconds)

    @tool(read_only=True, idempotent=True)
    def interactive_read(session_id: SessionId,
                         tail_lines: Annotated[int, Field(description="lines from the end", ge=1)] = 50,
                         log_offset: Annotated[int | None, Field(description="read from this byte offset of the "
                                                                             "session log instead of the end")] = None) -> dict:
        """Show the session's terminal as a human would see it (cleaned of escape codes)."""
        return inter.read(session_id, tail_lines, log_offset)

    @tool()
    def interactive_send(session_id: SessionId,
                         text: Annotated[str | None, Field(description="text to type (e.g. an answer to a prompt)")] = None,
                         key: Annotated[str | None, Field(description="special key: C-c, C-d, C-z, Escape, Enter, "
                                                                      "Tab, Up, Down, q, ...")] = None,
                         enter: Annotated[bool, Field(description="press Enter after the text")] = True) -> dict:
        """Low-level terminal input: type text or press a key (C-c interrupts a running command). Returns
        the screen after a short pause."""
        return inter.send(session_id, text, key, enter)

    @tool(read_only=True, idempotent=True)
    def interactive_list() -> dict:
        """List open interactive sessions on the current frontend."""
        return inter.list()

    @tool(destructive=True, idempotent=True)
    def interactive_stop(session_id: SessionId) -> dict:
        """End an interactive session: exit the shell, qdel the job if needed, remove the tmux session.
        The session log is kept in the workspace."""
        return inter.stop(session_id)

    # ------------------------------------------------------------------------ files
    @tool(read_only=True, idempotent=True)
    def list_dir(path: Annotated[str, Field(description="directory (relative = inside the workspace)")] = ".",
                 pattern: Annotated[str | None, Field(description="glob on names, e.g. '*.fastq.gz'")] = None) -> dict:
        """List a directory on grid storage (name, type, size, mtime), capped."""
        return files.list_dir(path, pattern)

    @tool(read_only=True, idempotent=True)
    def read_file(path: Annotated[str, Field(description="file (relative = inside the workspace); .gz is decompressed")],
                  max_bytes: Annotated[int | None, Field(description="bytes to return (capped)")] = None,
                  offset: Annotated[int, Field(description="byte offset to start at", ge=0)] = 0,
                  tail_lines: Annotated[int | None, Field(description="return the last N lines instead")] = None) -> dict:
        """Read the beginning (or a slice, or the tail) of a text file, with its total size. Refuses binary
        files: use summary tools in a job for BAM/CRAM/BCF."""
        return files.read_file(path, max_bytes, offset, tail_lines)

    @tool(idempotent=True)
    def write_file(path: Annotated[str, Field(description="destination inside the workspace")],
                   content: Annotated[str, Field(description="text content")],
                   overwrite: bool = False,
                   executable: Annotated[bool, Field(description="chmod +x")] = False) -> dict:
        """Write a small text file (script, config, sample sheet) into the project workspace, atomically."""
        return files.write_file(path, content, overwrite, executable)

    @tool(idempotent=True)
    def make_dir(path: Annotated[str, Field(description="directory inside the workspace")]) -> dict:
        """Create a directory (and parents) inside the project workspace."""
        return files.make_dir(path)

    @tool()
    def upload_file(local_path: Annotated[str, Field(description="file on this machine (inside the harness project)")],
                    remote_path: Annotated[str, Field(description="destination file or directory in the workspace")],
                    overwrite: bool = False) -> dict:
        """Copy a local file to grid storage over the existing connection."""
        return files.upload(local_path, remote_path, overwrite)

    @tool()
    def download_file(remote_path: Annotated[str, Field(description="file on grid storage")],
                      local_path: Annotated[str, Field(description="destination on this machine (inside the harness project)")],
                      overwrite: bool = False) -> dict:
        """Copy a (result) file from grid storage to this machine, e.g. a plot or a summary table."""
        return files.download(remote_path, local_path, overwrite)

    @tool(read_only=True)
    def frontend_exec(command: Annotated[str, Field(description="one allowlisted command, no shell syntax, "
                                                                "e.g. 'module avail samtools' or 'qstat -Q'")]) -> dict:
        """Run one cheap, allowlisted, read-only command on the shared frontend (module avail/show, which,
        ls, df, quota, klist, qstat, ...). No pipes or redirects. Real work belongs in interactive_run or
        submit_job."""
        return files.frontend_exec(command)

    @tool(read_only=True, idempotent=True)
    def audit_log(last: Annotated[int, Field(description="number of recent entries", ge=1, le=200)] = 20) -> dict:
        """Recent calls made through this server by any agent on this machine (the audit trail)."""
        return {"path": str(state.audit_path), "entries": state.read_audit(last)}

    return mcp


def main(config_path: str | None = None) -> None:
    build_server(config_path).run("stdio")
