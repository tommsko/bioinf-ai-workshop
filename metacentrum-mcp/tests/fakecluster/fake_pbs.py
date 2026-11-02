#!/usr/bin/env python3
"""A tiny PBS imitation for tests: qsub (batch and -I), qstat (-f, -F json, -u table), qdel.

Invoked through symlinks named qsub/qstat/qdel. State lives in $FAKE_PBS_DIR.
Jobs really run (bash, in the background), honour walltime, and write .o/.e files
into the submission directory like PBS does.
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

D = Path(os.environ.get("FAKE_PBS_DIR", "/tmp/fake_pbs"))
D.mkdir(parents=True, exist_ok=True)
(D / "spool").mkdir(exist_ok=True)
DB = D / "jobs.json"
SERVER = "fake-pbs.local"
QUEUE_DELAY = float(os.environ.get("FAKE_PBS_QUEUE_DELAY", "1"))


@contextmanager
def db():
    with open(D / "lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = json.loads(DB.read_text()) if DB.exists() else {"next": 1000, "jobs": {}}
        yield data
        DB.write_text(json.dumps(data))


def wt_secs(s):
    p = [int(x) for x in s.split(":")]
    while len(p) < 3:
        p.insert(0, 0)
    return p[0] * 3600 + p[1] * 60 + p[2]


def fmt(secs):
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def parse_qsub(args):
    opts = {"N": None, "l": [], "q": None, "I": False, "W": None, "script": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-I":
            opts["I"] = True
        elif a in ("-N", "-q", "-W"):
            opts[a[1]] = args[i + 1]
            i += 1
        elif a == "-l":
            opts["l"].append(args[i + 1])
            i += 1
        else:
            opts["script"] = a
        i += 1
    if opts["script"]:
        for line in Path(opts["script"]).read_text().splitlines():
            m = re.match(r"#PBS\s+-(\w)\s+(.*)", line)
            if m:
                k, v = m.group(1), m.group(2).strip()
                if k == "l":
                    opts["l"].append(v)
                elif k in ("N", "q", "W") and not opts[k]:
                    opts[k] = v
    res = {}
    for l in opts["l"]:
        for part in l.split(","):
            if part.startswith("select="):
                res["select"] = part[7:]
                for kv in part[7:].split(":")[1:]:
                    k, v = kv.split("=")
                    res[k] = v
            elif "=" in part:
                k, v = part.split("=", 1)
                res[k] = v
    return opts, res


def new_job(opts, res, interactive):
    with db() as data:
        n = data["next"]
        data["next"] += 1
        jid = f"{n}.{SERVER}"
        name = opts["N"] or (Path(opts["script"]).name if opts["script"] else "STDIN")
        wd = os.getcwd()
        data["jobs"][jid] = {
            "Job_Name": name, "Job_Owner": os.environ.get("USER", "u"), "job_state": "Q",
            "queue": opts["q"] or "default", "Resource_List": res, "qtime": time.ctime(),
            "Output_Path": f"{SERVER}:{wd}/{name}.o{n}", "Error_Path": f"{SERVER}:{wd}/{name}.e{n}",
            "Variable_List": {"PBS_O_WORKDIR": wd, "PBS_O_HOME": os.environ.get("HOME", "")},
            "comment": "Not Running: waiting for resources", "interactive": interactive,
            "depend": opts["W"], "script": opts["script"],
        }
    return jid, n


def set_job(jid, **kw):
    with db() as data:
        data["jobs"][jid].update(kw)


def get_job(jid):
    with db() as data:
        return data["jobs"].get(jid)


def run_batch(jid):
    """Detached runner: queue delay, execute, enforce walltime, deliver output files."""
    job = get_job(jid)
    time.sleep(QUEUE_DELAY)
    dep = job.get("depend")
    if dep and dep.startswith("afterok:"):
        for d in dep[8:].split(":"):
            while True:
                dj = get_job(d)
                if not dj or dj["job_state"] == "F":
                    break
                time.sleep(0.5)
            if dj and dj.get("Exit_status") != 0:
                set_job(jid, job_state="F", Exit_status=-1, comment="dependency failed")
                return
    if get_job(jid)["job_state"] == "F":  # deleted while queued
        return
    seq = jid.split(".")[0]
    scratch = D / "scratch" / f"job_{jid}"
    scratch.mkdir(parents=True, exist_ok=True)
    ou, er = D / "spool" / f"{jid}.OU", D / "spool" / f"{jid}.ER"
    env = dict(os.environ, PBS_JOBID=jid, PBS_O_WORKDIR=job["Variable_List"]["PBS_O_WORKDIR"],
               SCRATCHDIR=str(scratch), PBS_JOBNAME=job["Job_Name"])
    wd = job["Variable_List"]["PBS_O_WORKDIR"]
    with open(ou, "wb") as o, open(er, "wb") as e:
        p = subprocess.Popen(["bash", job["script"]], cwd=os.environ.get("HOME"), env=env,
                             stdout=o, stderr=e, start_new_session=True)
        set_job(jid, job_state="R", stime=time.ctime(), exec_host="localhost/0", runner_pid=p.pid,
                comment="Job run")
        start = time.time()
        limit = wt_secs(job["Resource_List"].get("walltime", "01:00:00"))
        rc = None
        while rc is None:
            rc = p.poll()
            if rc is None and time.time() - start > limit:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait()
                rc = 256 + 15
            time.sleep(0.2)
    if rc < 0:
        rc = 256 - rc
    cur = get_job(jid)
    if cur.get("deleted"):
        rc = 256 + 15
    used = fmt(int(time.time() - start))
    Path(f"{wd}/{job['Job_Name']}.o{seq}").write_bytes(ou.read_bytes())
    Path(f"{wd}/{job['Job_Name']}.e{seq}").write_bytes(er.read_bytes())
    set_job(jid, job_state="F", Exit_status=rc, obittime=time.ctime(),
            resources_used={"walltime": used, "cput": used, "mem": "10240kb", "ncpus": 1})


def qsub(args):
    opts, res = parse_qsub(args)
    if "walltime" not in res:
        res["walltime"] = "01:00:00"
    jid, n = new_job(opts, res, opts["I"])
    if not opts["I"]:
        subprocess.Popen([sys.executable, __file__, "__run", jid], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(jid)
        return 0
    print(f"qsub: waiting for job {jid} to start", flush=True)
    time.sleep(QUEUE_DELAY)
    if get_job(jid)["job_state"] == "F":
        print(f"qsub: job {jid} was deleted")
        return 1
    print(f"qsub: job {jid} ready", flush=True)
    print("", flush=True)
    scratch = D / "scratch" / f"job_{jid}"
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PBS_JOBID=jid, SCRATCHDIR=str(scratch), PBS_O_WORKDIR=os.getcwd())
    p = subprocess.Popen(["bash", "--norc", "-i"], env=env, cwd=os.environ.get("HOME"))
    set_job(jid, job_state="R", stime=time.ctime(), exec_host="localhost/0", runner_pid=p.pid, comment="Job run")
    start = time.time()
    limit = wt_secs(res["walltime"])
    while p.poll() is None:
        if time.time() - start > limit or get_job(jid).get("deleted"):
            p.terminate()
            p.wait()
            print(f"\nqsub: job {jid} completed")
            break
        time.sleep(0.2)
    set_job(jid, job_state="F", Exit_status=p.returncode if p.returncode >= 0 else 256 - p.returncode,
            obittime=time.ctime())
    return 0


def qstat(args):
    full = "-f" in args
    js = "-F" in args and "json" in args
    ids = [a for a in args if re.match(r"^\d+(\.\S+)?$", a)]
    with db() as data:
        jobs = data["jobs"]
    if "-u" in args:
        user = args[args.index("-u") + 1]
        print(f"\n{SERVER}:\n{'Job ID':<30} Username Queue    Jobname  SessID NDS TSK Memory Time  S Time")
        print("-" * 90)
        for jid, j in jobs.items():
            if j["job_state"] == "F" and "-x" not in args:
                continue
            print(f"{jid:<30} {user} {j['queue']} {j['Job_Name']} 1234 1 1 "
                  f"{j['Resource_List'].get('mem', '-')} {j['Resource_List'].get('walltime')} {j['job_state']} --")
        return 0
    rc = 0
    found = {}
    for i in ids:
        full_id = i if "." in i else f"{i}.{SERVER}"
        if full_id in jobs and (jobs[full_id]["job_state"] != "F" or "-x" in args):
            found[full_id] = jobs[full_id]
        else:
            print(f"qstat: Unknown Job Id {i}", file=sys.stderr)
            rc = 153
    if js:
        # Faithful to PBS 23.06: with several ids, `-F json` prints only the first job.
        found = dict(list(found.items())[:1])
        out = {"timestamp": int(time.time()), "pbs_version": "fake", "pbs_server": SERVER, "Jobs": {}}
        for jid, j in found.items():
            out["Jobs"][jid] = {k: v for k, v in j.items() if k not in ("runner_pid", "deleted", "script", "interactive", "depend")}
        print(json.dumps(out, indent=4))
    elif full:
        for jid, j in found.items():
            print(f"Job Id: {jid}")
            for k, v in j.items():
                if k in ("runner_pid", "deleted", "script", "interactive", "depend"):
                    continue
                if isinstance(v, dict):
                    if k == "Variable_List":
                        print(f"    {k} = " + ",".join(f"{a}={b}" for a, b in v.items()))
                    else:
                        for a, b in v.items():
                            print(f"    {k}.{a} = {b}")
                else:
                    print(f"    {k} = {v}")
            print()
    else:
        for jid, j in found.items():
            print(f"{jid} {j['Job_Name']} {j['job_state']}")
    return rc


def qdel(args):
    rc = 0
    for i in args:
        with db() as data:
            j = data["jobs"].get(i)
            if not j or j["job_state"] == "F":
                print(f"qdel: Unknown Job Id {i}", file=sys.stderr)
                rc = 153
                continue
            j["deleted"] = True
            pid = j.get("runner_pid")
            if j["job_state"] == "Q":
                j.update(job_state="F", Exit_status=271)
        if pid and not j.get("interactive"):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    return rc


if __name__ == "__main__":
    prog = Path(sys.argv[0]).name
    if len(sys.argv) > 2 and sys.argv[1] == "__run":
        run_batch(sys.argv[2])
        sys.exit(0)
    sys.exit({"qsub": qsub, "qstat": qstat, "qdel": qdel}[prog](sys.argv[1:]))
