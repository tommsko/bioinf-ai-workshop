"""Live self-test against the configured cluster, driven exactly like a harness: MCP over stdio.

    metacentrum-mcp selftest                 # connection, workspace, files, guardrails, a tiny batch job
    metacentrum-mcp selftest --interactive   # also an interactive job (may wait in the queue)

Uses minimal resources (1 CPU, 500 MB, 5 min) and leaves only small files in <workspace>/.mcp/selftest/.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters


@dataclass
class Report:
    rows: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
        print(f"  {mark}  {name:<34} {detail}", flush=True)
        return ok

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)


async def _call(c: Client, tool: str, **args):
    r = await c.call_tool(tool, args)
    text = r.content[0].text if r.content else ""
    if r.is_error:
        return {"__error__": text}
    return json.loads(text)


async def _wait_job(c: Client, job_id: str, deadline: float) -> dict:
    while True:
        res = await _call(c, "wait_for_job", job_id=job_id, timeout_seconds=20)
        if "__error__" in res or res.get("finished") or time.monotonic() > deadline:
            return res


async def run(config: str | None, interactive: bool, timeout_min: int) -> Report:
    rep = Report()
    env = dict(os.environ)
    if config:
        env["METACENTRUM_MCP_CONFIG"] = config
    params = StdioServerParameters(command=sys.executable, args=["-m", "metacentrum_mcp", "serve"], env=env)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    deadline = time.monotonic() + timeout_min * 60
    async with Client(params, read_timeout_seconds=300) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        rep.add("server starts, tools listed", "submit_job" in tools, f"{len(tools)} tools")

        con = await _call(c, "connect")
        if not rep.add("connect", "__error__" not in con, con.get("__error__", con.get("target", ""))[:120]):
            return rep
        rep.add("kerberos ticket", con["kerberos"]["valid"],
                f"{con['kerberos'].get('principal')} {con['kerberos'].get('hours_left')} h left")
        rep.add("project workspace", con["workspace_ok"], f"{con['workspace']} ({con.get('workspace_free')})")
        for w in con.get("warnings", []):
            print(f"        warning: {w}")

        info = await _call(c, "cluster_info")
        ok = "__error__" not in info
        rep.add("cluster_info", ok, f"default queue {info.get('defaults', {}).get('queue')}" if ok else info["__error__"][:120])
        if ok:
            senv = info["software_env"]
            rep.add("shared software env", bool(senv.get("available")) or not senv.get("activate_script"),
                    senv.get("activate_script") or "disabled")

        base = f".mcp/selftest/{stamp}"
        w = await _call(c, "write_file", path=f"{base}/hello.txt", content="hello grid\n")
        r = await _call(c, "read_file", path=f"{base}/hello.txt")
        rep.add("write + read in workspace", r.get("content") == "hello grid\n", w.get("path", w.get("__error__", ""))[:120])
        home = await _call(c, "write_file", path="~/mcp-selftest.txt", content="x")
        rep.add("home writes refused", "__error__" in home)
        fx = await _call(c, "frontend_exec", command="module avail samtools")
        rep.add("frontend_exec (module avail)", "__error__" not in fx, (fx.get("output") or fx.get("__error__", ""))[-60:].strip())
        bad = await _call(c, "frontend_exec", command="bwa mem x y")
        rep.add("heavy command on frontend refused", "__error__" in bad)
        big = await _call(c, "submit_job", name=f"selftest_big_{stamp}", script="true", ncpus=9999)
        rep.add("oversized job refused", "__error__" in big)

        sub = await _call(c, "submit_job", name=f"selftest_{stamp}", workdir=base, walltime="00:05:00", mem="500mb",
                          scratch="100mb", script='echo "node=$(hostname)"; echo "scratch=$SCRATCHDIR"; '
                                                  'command -v samtools >/dev/null && samtools --version | head -1 || true')
        if rep.add("submit_job", sub.get("status") == "submitted", sub.get("job_id") or sub.get("__error__", "")[:120]):
            dup = await _call(c, "submit_job", name=f"selftest_{stamp}", workdir=base, script="true")
            rep.add("duplicate name -> same job", dup.get("job_id") == sub["job_id"], dup.get("status", ""))
            print(f"        waiting for {sub['job_id']} (queue: {sub['resources'].get('queue')}) ...", flush=True)
            res = await _wait_job(c, sub["job_id"], deadline)
            rep.add("batch job finished", res.get("finished") is True and res.get("exit_status") == 0,
                    f"state={res.get('state')} exit={res.get('exit_status')} queue={res.get('queue')} host={res.get('exec_host')}")
            out = await _call(c, "job_output", job_id=sub["job_id"])
            rep.add("job_output", "node=" in out.get("output", ""), out.get("output", out.get("__error__", "")).replace("\n", " | ")[:110])
            if res.get("finished") is not True:
                await _call(c, "cancel_job", job_id=sub["job_id"])

        if interactive:
            it = await _call(c, "interactive_start", walltime="00:10:00", mem="500mb", scratch="100mb",
                             name="mcp_selftest", wait_seconds=20)
            if rep.add("interactive_start", "__error__" not in it, it.get("job_id") or it.get("__error__", "")[:120]):
                sid = it["session_id"]
                while it.get("state") in ("starting", "queued") and time.monotonic() < deadline:
                    print("        interactive job queued ...", flush=True)
                    it = await _call(c, "interactive_status", session_id=sid, wait_seconds=20)
                if rep.add("interactive job ready", it.get("state") == "ready", it.get("state", "")):
                    r1 = await _call(c, "interactive_run", session_id=sid, command="hostname; echo $PBS_JOBID; pwd")
                    rep.add("interactive_run", r1.get("exit_code") == 0, (r1.get("output") or r1.get("__error__", "")).replace("\n", " | ")[:110])
                    r2 = await _call(c, "interactive_run", session_id=sid, command="false")
                    rep.add("exit code propagated", r2.get("exit_code") == 1)
                stop = await _call(c, "interactive_stop", session_id=sid)
                rep.add("interactive_stop", stop.get("status") == "stopped")
    return rep


def main(config: str | None, interactive: bool, timeout_min: int) -> int:
    print("metacentrum-mcp selftest (MCP stdio -> server -> SSH -> PBS)")
    rep = asyncio.run(run(config, interactive, timeout_min))
    n_fail = sum(1 for _, ok, _ in rep.rows if not ok)
    print(f"\n{len(rep.rows) - n_fail}/{len(rep.rows)} checks passed")
    return 0 if rep.ok else 1
