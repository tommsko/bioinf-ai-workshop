"""End-to-end: real MCP stdio server -> real ssh ControlMaster -> local sshd -> fake PBS + tmux.

Run:  uv run pytest -v tests/test_e2e.py
Needs: /usr/bin/sshd, tmux, ssh-keygen on this machine.
"""

import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

HERE = Path(__file__).parent
pytestmark = pytest.mark.skipif(not (shutil.which("tmux") and Path("/usr/bin/sshd").exists()),
                                reason="needs sshd and tmux")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    # Short path: sockets and tmux are happier.
    w = Path(f"/tmp/mcmcp-test-{os.getpid()}")
    port = random.randint(30000, 40000)
    cfg = subprocess.check_output([str(HERE / "fakecluster" / "up.sh"), str(w), str(port)], text=True).strip()
    yield {"dir": w, "config": cfg}
    subprocess.run(["tmux", "-L", "mcp", "kill-server"], capture_output=True)
    for pid_file in [w / "sshd.pid"]:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
    for sock in [*(w / "state" / "cm").glob("*"), *Path(f"/tmp/metacentrum-mcp-{os.getuid()}").glob("*")]:
        subprocess.run(["ssh", "-o", f"ControlPath={sock}", "-O", "exit", "x"], capture_output=True)
    subprocess.run(["pkill", "-f", str(w / "pbs")], capture_output=True)
    shutil.rmtree(w, ignore_errors=True)


def server_params(cluster):
    env = dict(os.environ, METACENTRUM_MCP_CONFIG=cluster["config"])
    return StdioServerParameters(command=sys.executable, args=["-m", "metacentrum_mcp", "serve"],
                                 env=env, cwd=str(cluster["dir"]))


async def call(client, tool, **args):
    r = await client.call_tool(tool, args)
    text = r.content[0].text if r.content else ""
    if r.is_error:
        return {"__error__": text}
    return json.loads(text)


async def test_full_workflow(cluster):
    async with Client(server_params(cluster)) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert {"connect", "submit_job", "interactive_run", "disconnect"} <= tools

        con = await call(c, "connect")
        assert con["connected"] and con["workspace_ok"], con
        ws = con["workspace"]
        assert ws.endswith(f"/project/{con['remote_user']}")

        # ---- defaults visible to the model
        info = await call(c, "cluster_info")
        assert info["defaults"]["queue"] == "fastq" and info["defaults"]["workdir"] == ws
        assert info["software_env"]["available"] and "samtools       1.21" in info["software_env"]["tools"]
        assert "fake test cluster" in info["notes"]
        assert info["workspace_rules"]["writable"] == [ws]

        # ---- upload / download over the shared connection
        (cluster["dir"] / "local_in.txt").write_text("payload\n")
        up = await call(c, "upload_file", local_path="local_in.txt", remote_path="xfer/")
        assert up["remote"] == f"{ws}/xfer/local_in.txt", up
        down = await call(c, "download_file", remote_path="xfer/local_in.txt", local_path="local_out.txt")
        assert (cluster["dir"] / "local_out.txt").read_text() == "payload\n", down
        outside = await call(c, "upload_file", local_path="/etc/hostname", remote_path="xfer/")
        assert "outside the allowed local roots" in outside["__error__"]

        # ---- files and workspace policy
        w = await call(c, "write_file", path="scripts/hello.sh", content="echo hello\n", executable=True)
        assert w["path"] == f"{ws}/scripts/hello.sh"
        again = await call(c, "write_file", path="scripts/hello.sh", content="x")
        assert "already exists" in again["__error__"]
        home = await call(c, "write_file", path="~/oops.txt", content="x")
        assert "outside the writable roots" in home["__error__"] and "quota" in home["__error__"]
        esc = await call(c, "write_file", path="../escape.txt", content="x")
        assert "__error__" in esc
        r = await call(c, "read_file", path="scripts/hello.sh")
        assert r["content"] == "echo hello\n"
        ls = await call(c, "list_dir", path=".")
        assert any(e["name"] == "scripts/" for e in ls["entries"])
        big = "\n".join(f"line {i}" for i in range(20000))
        await call(c, "write_file", path="big.txt", content=big)
        part = await call(c, "read_file", path="big.txt", max_bytes=1000)
        assert part["truncated"] and len(part["content"]) == 1000
        tail = await call(c, "read_file", path="big.txt", tail_lines=3)
        assert tail["content"].strip().endswith("line 19999")

        # ---- frontend_exec allowlist
        ok = await call(c, "frontend_exec", command=f"ls {ws}")
        assert "scripts" in ok["output"]
        assert "not allowed" in (await call(c, "frontend_exec", command="bwa mem ref.fa r.fq"))["__error__"]
        assert "shell syntax" in (await call(c, "frontend_exec", command="ls; rm -rf ~"))["__error__"]

        # ---- batch: guardrails
        huge = await call(c, "submit_job", name="too_big", script="true", ncpus=999, walltime="999:00:00")
        assert "ncpus=999" in huge["__error__"] and "walltime" in huge["__error__"]
        pbsline = await call(c, "submit_job", name="sneaky", script="#PBS -l ncpus=64\ntrue")
        assert "#PBS" in pbsline["__error__"]

        # ---- batch: submit, idempotency, wait, output
        sub = await call(c, "submit_job", name="align_test", workdir="jobs/align",
                         script='echo "running in $PWD env=$WORKSHOP_ENV"; echo to-stderr >&2; sleep 2; echo done')
        assert sub["resources"]["queue"] == "fastq"  # default queue applied
        badq = await call(c, "submit_job", name="other_queue", script="true", queue="gpu")
        assert "queue 'gpu' not allowed" in badq["__error__"]
        assert sub["status"] == "submitted", sub
        jid = sub["job_id"]
        dup = await call(c, "submit_job", name="align_test", script="echo other")
        assert dup["status"] == "already_submitted" and dup["job_id"] == jid
        st = await call(c, "job_status", job_id=jid.split(".")[0])
        assert st["state"] in ("Q", "R") and st["submitted_by_this_server"]
        done = await call(c, "wait_for_job", job_id=jid, timeout_seconds=25)
        assert done["finished"] and done["exit_status"] == 0, done
        out = await call(c, "job_output", job_id=jid)
        assert f"running in {ws}/jobs/align env=fake" in out["output"] and out["output"].strip().endswith("done")
        err = await call(c, "job_output", job_id=jid, stream="stderr")
        assert "to-stderr" in err["output"] and "[mcp] job" in err["output"]
        grep = await call(c, "job_output", job_id=jid, pattern="^done")
        assert grep["output"].strip().endswith("done") and "running" not in grep["output"]
        again = await call(c, "submit_job", name="align_test", script="echo other")
        assert again["status"] == "already_completed"
        script = await call(c, "read_file", path=sub["script_path"])
        assert "#PBS -l select=1:ncpus=1:mem=4gb:scratch_local=10gb" in script["content"]
        assert "qsub" in script["content"]  # rerun-without-agent hint

        # ---- failing job: strict mode + exit meaning
        noenv = await call(c, "submit_job", name="noenv", script='echo "env=${WORKSHOP_ENV:-none}"',
                           use_software_env=False)
        await call(c, "wait_for_job", job_id=noenv["job_id"], timeout_seconds=25)
        assert "env=none" in (await call(c, "job_output", job_id=noenv["job_id"]))["output"]
        bad = await call(c, "submit_job", name="fails", script="false\necho unreachable")
        res = await call(c, "wait_for_job", job_id=bad["job_id"], timeout_seconds=25)
        assert res["exit_status"] == 1 and "exited with status 1" in res["exit_meaning"]

        # ---- walltime kill + likely cause
        slow = await call(c, "submit_job", name="slow", script="sleep 30", walltime="00:00:02")
        res = await call(c, "wait_for_job", job_id=slow["job_id"], timeout_seconds=25)
        assert res["exit_status"] == 271 and "walltime" in res.get("likely_cause", ""), res

        # ---- dependencies
        a = await call(c, "submit_job", name="step_a", script="echo A > a.txt")
        b = await call(c, "submit_job", name="step_b", script="cat a.txt", depends_on=[a["job_id"]])
        res = await call(c, "wait_for_job", job_id=b["job_id"], timeout_seconds=25)
        assert res["exit_status"] == 0
        assert "A" in (await call(c, "job_output", job_id=b["job_id"]))["output"]

        # ---- cancel: own job yes, foreign job no
        long = await call(c, "submit_job", name="long", script="sleep 300")
        cn = await call(c, "cancel_job", job_id=long["job_id"])
        assert cn["status"] == "cancel_requested"
        res = await call(c, "wait_for_job", job_id=long["job_id"], timeout_seconds=15)
        assert res["finished"]
        foreign = await call(c, "cancel_job", job_id="424242.fake-pbs.local")
        assert "not submitted through this MCP server" in foreign["__error__"]

        # ---- concurrent job cap
        ids = []
        for i in range(5):
            s = await call(c, "submit_job", name=f"cap{i}", script="sleep 60")
            ids.append(s["job_id"])
        over = await call(c, "submit_job", name="cap_over", script="true")
        assert "cap 5" in over["__error__"]
        active = await call(c, "list_jobs")
        assert {j["job_id"] for j in active["jobs"]} >= set(ids), active  # every queued job still active
        for j in ids:
            await call(c, "cancel_job", job_id=j)
        lst = await call(c, "list_jobs", include_finished=True)
        assert lst["count"] >= 8
        await call(c, "list_jobs", scope="user")

        # ---- interactive
        it = await call(c, "interactive_start", walltime="00:10:00", wait_seconds=15)
        sid = it["session_id"]
        assert it["state"] == "ready", it
        r1 = await call(c, "interactive_run", session_id=sid, command="echo $PBS_JOBID; pwd; echo env=$WORKSHOP_ENV")
        assert r1["status"] == "completed" and r1["exit_code"] == 0, r1
        assert it["job_id"] in r1["output"] and ws in r1["output"] and "env=fake" in r1["output"]
        r2 = await call(c, "interactive_run", session_id=sid, command="cd scripts && export FOO=bar; false")
        assert r2["exit_code"] == 1 and r2["output"] == ""
        r3 = await call(c, "interactive_run", session_id=sid, command='echo "$FOO $(basename $PWD)"; ./hello.sh')
        assert r3["output"].splitlines() == ["bar scripts", "hello"], r3
        multi = await call(c, "interactive_run", session_id=sid,
                           command="for i in 1 2 3; do\n  echo n$i\ndone\ncat <<EOF\nheredoc ok\nEOF")
        assert multi["output"].splitlines() == ["n1", "n2", "n3", "heredoc ok"], multi
        long_cmd = "echo " + " ".join(f"w{i}" for i in range(1500)) + " | wc -w"
        lc = await call(c, "interactive_run", session_id=sid, command=long_cmd)  # staged via a file
        assert lc["exit_code"] == 0 and lc["output"].strip() == "1500", lc
        syn = await call(c, "interactive_run", session_id=sid, command="echo 'unterminated")
        assert "syntax error" in syn["__error__"]
        flood = await call(c, "interactive_run", session_id=sid, command="seq 1 200000")
        assert flood["truncated"] and flood["output"].startswith("1\n") and flood["output"].endswith("200000")
        assert len(flood["output"]) < 70000
        # timeout -> busy -> wait
        t = await call(c, "interactive_run", session_id=sid, command="sleep 4; echo slept", timeout_seconds=1)
        assert t["status"] == "running"
        busy = await call(c, "interactive_run", session_id=sid, command="echo hi")
        assert "still running" in busy["__error__"]
        w = await call(c, "interactive_wait", session_id=sid, timeout_seconds=10)
        assert w["status"] == "completed" and w["output"] == "slept", w
        # interrupt
        t = await call(c, "interactive_run", session_id=sid, command="sleep 100", timeout_seconds=1)
        assert t["status"] == "running"
        await call(c, "interactive_send", session_id=sid, key="C-c")
        ok = await call(c, "interactive_run", session_id=sid, command="echo after-interrupt")
        assert ok["output"] == "after-interrupt", ok
        screen = await call(c, "interactive_read", session_id=sid, tail_lines=20)
        assert "after-interrupt" in screen["screen"] and "__MCP_" not in screen["screen"]
        lst = await call(c, "interactive_list")
        assert any(s["session_id"] == sid for s in lst["sessions"])
        stop = await call(c, "interactive_stop", session_id=sid)
        assert stop["status"] == "stopped"
        dead = await call(c, "interactive_run", session_id=sid, command="echo x")
        assert "__error__" in dead

        # ---- audit
        audit = await call(c, "audit_log", last=200)
        assert any(e["tool"] == "submit_job" and e.get("ok") is False for e in audit["entries"])


async def test_crash_resume_and_multi_agent(cluster):
    params = server_params(cluster)
    # Agent 1 opens a session, then its server dies hard.
    async with Client(params) as c1:
        con = await call(c1, "connect")
        it = await call(c1, "interactive_start", walltime="00:10:00", wait_seconds=15)
        sid = it["session_id"]
        await call(c1, "interactive_run", session_id=sid, command="export KEEP=me")
        pids = subprocess.run(["pgrep", "-f", "metacentrum_mcp serve"], capture_output=True, text=True).stdout.split()
        for p in pids:
            os.kill(int(p), signal.SIGKILL)
    # Fresh server: the connection and the interactive shell (with its state) are still there.
    async with Client(params) as c2:
        con = await call(c2, "connect")
        assert con["reused_existing_connection"] is True
        r = await call(c2, "interactive_run", session_id=sid, command="echo $KEEP")
        assert r["output"] == "me", r

        # Two agents at once, same job name -> exactly one job.
        async with Client(params) as c3:
            r_a, r_b = await asyncio.gather(
                call(c2, "submit_job", name="race", script="sleep 3"),
                call(c3, "submit_job", name="race", script="sleep 3"),
            )
            assert {r_a["status"], r_b["status"]} == {"submitted", "already_submitted"}, (r_a, r_b)
            assert r_a["job_id"] == r_b["job_id"]
            # Concurrent commands on one interactive session are serialised, not interleaved.
            x, y = await asyncio.gather(
                call(c2, "interactive_run", session_id=sid, command="sleep 1; echo from-2"),
                call(c3, "interactive_run", session_id=sid, command="echo from-3"),
            )
            outs = sorted(o.get("output", o.get("__error__", "")) for o in (x, y))
            assert outs[0] == "from-2" or "still running" in outs[0] or outs == ["from-2", "from-3"], outs

        # Explicit disconnect is respected: no silent reconnect.
        d = await call(c2, "disconnect", stop_interactive_sessions=True)
        assert d["disconnected"] and sid in d["stopped_sessions"]
        st = await call(c2, "connection_status")
        assert st["connected"] is False
        refused = await call(c2, "list_dir", path=".")
        assert "Call `connect` first" in refused["__error__"]
        again = await call(c2, "connect")
        assert again["connected"] and again["reused_existing_connection"] is False


async def test_rejected_login_is_not_retried(cluster, tmp_path):
    """A password-only account cannot log in from BatchMode; hammering the frontend could lock it."""
    cfg = Path(cluster["config"]).read_text()
    wrong_key = tmp_path / "wrong_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_key)], check=True)
    cfg = cfg.replace(f'identity_file = "{cluster["dir"]}/user_key"', f'identity_file = "{wrong_key}"')
    cfg = cfg.replace(f'state_dir = "{cluster["dir"]}/state"', f'state_dir = "{tmp_path}/state"')
    (tmp_path / "config.toml").write_text(cfg)
    env = dict(os.environ, METACENTRUM_MCP_CONFIG=str(tmp_path / "config.toml"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "metacentrum_mcp", "serve"], env=env)
    async with Client(params) as c:
        first = await call(c, "list_dir", path=".")
        assert "authentication failed" in first["__error__"]
        for _ in range(3):
            again = await call(c, "list_dir", path=".")
            assert "Not retrying automatically" in again["__error__"]
        st = await call(c, "connection_status")
        assert st["last_login_failure"]["kind"] == "auth"
    log = next((tmp_path / "state").glob("master-*.log")).read_text()
    assert log.count("Permission denied") == 1, log
