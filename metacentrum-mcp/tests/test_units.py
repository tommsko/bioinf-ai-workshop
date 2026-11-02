import pytest

from metacentrum_mcp.config import LimitsConfig
from metacentrum_mcp.errors import PolicyError
from metacentrum_mcp.files import SHELL_META
from metacentrum_mcp.pbs import Resources, _parse_qstat_text, exit_meaning, normalize_job
from metacentrum_mcp.textutil import clean_terminal, parse_size, parse_walltime, truncate_lines

QSTAT_F = """Job Id: 18411451.pbs-m1.metacentrum.cz
    Job_Name = align
    Job_Owner = alice@META
    resources_used.cput = 00:59:58
    resources_used.mem = 4190000kb
    resources_used.walltime = 01:00:02
    job_state = F
    queue = q_1h
    exec_host = elmo3-1/0*4
    exec_host2 = elmo3-1.hw.elixir-czech.cz:15002/0*4
    Output_Path = skirit.ics.muni.cz:/storage/praha5-elixir/projects/bioinf-fi/alice/jobs/al
\tign.o18411451
    Error_Path = skirit.ics.muni.cz:/storage/praha5-elixir/projects/bioinf-fi/alice/jobs/align.e18411451
    Resource_List.mem = 4gb
    Resource_List.ncpus = 4
    Resource_List.walltime = 01:00:00
    Variable_List = PBS_O_HOME=/storage/brno2/home/alice,PBS_O_WORKDIR=/storage/praha5-elixir/projects/bioinf-fi/alice/jobs
    Exit_status = 271
"""


def test_parse_qstat_text_and_normalize():
    jobs = _parse_qstat_text(QSTAT_F)
    jid, attrs = next(iter(jobs.items()))
    info = normalize_job(jid, attrs)
    assert info["state"] == "F" and info["finished"]
    assert info["exit_status"] == 271 and "SIGTERM" in info["exit_meaning"]
    assert info["exec_host"] == "elmo3-1.hw.elixir-czech.cz"
    assert info["stdout_path"].endswith("/jobs/align.o18411451")  # continuation line joined, host stripped
    assert info["workdir"] == "/storage/praha5-elixir/projects/bioinf-fi/alice/jobs"
    assert "walltime" in info["likely_cause"] and "memory" in info["likely_cause"]


@pytest.mark.parametrize("v,secs", [("01:00:00", 3600), ("90:00", 5400), ("2h", 7200), ("1:00:00:00", 86400), (60, 60)])
def test_walltime(v, secs):
    assert parse_walltime(v) == secs


@pytest.mark.parametrize("v,n", [("4gb", 4 << 30), ("4G", 4 << 30), ("500mb", 500 << 20), ("1.5gb", int(1.5 * (1 << 30))), (8, 8 << 30)])
def test_size(v, n):
    assert parse_size(v) == n


def test_resources_caps():
    lim = LimitsConfig()
    out = Resources(ncpus=4, mem="8gb", walltime="2:00:00", scratch="20gb").validate(lim)
    assert out == {"select": "select=1:ncpus=4:mem=8gb:scratch_local=20gb", "walltime": "02:00:00"}
    with pytest.raises(PolicyError) as e:
        Resources(ncpus=64, mem="1tb", walltime="100:00:00", ngpus=1, queue="gpu").validate(lim)
    msg = str(e.value)
    for frag in ("ncpus=64", "mem=1tb", "walltime", "ngpus=1", "queue 'gpu'"):
        assert frag in msg
    with pytest.raises(PolicyError):
        Resources(walltime="08:00:00").validate(lim, interactive=True)


def test_exit_meaning():
    assert exit_meaning(0) == "success"
    assert "status 2" in exit_meaning(2)
    assert "signal 9" in exit_meaning(137)
    assert "negative" in exit_meaning(-11)


def test_clean_terminal():
    raw = "\x1b[?2004h\x1b[1;32mok\x1b[0m\r\n 10%\r 50%\r100%\r\ndone"
    assert clean_terminal(raw) == "ok\n100%\ndone"


def test_truncate():
    text = "\n".join(str(i) for i in range(100))
    out, clipped = truncate_lines(text, head=2, tail=2)
    assert clipped and out.splitlines() == ["0", "1", "…[96 lines omitted]…", "98", "99"]


@pytest.mark.parametrize("cmd", ["ls; rm -rf ~", "ls | head", "ls $(whoami)", "ls > x", "ls *", "ls `id`"])
def test_frontend_meta_rejected(cmd):
    assert SHELL_META.search(cmd)


def test_workdir_recovered_from_pbs_json_quirk():
    # PBS 23 JSON: an empty value swallows the following ",PBS_O_WORKDIR=..." (seen on pbs-m1).
    attrs = {"job_state": "F", "Exit_status": 127,
             "Variable_List": {"PBS_O_INTERACTIVE_ENCRYPT_METHOD": ",PBS_O_WORKDIR=/storage/x/wd", "PBS_O_QUEUE": "q"}}
    info = normalize_job("1.pbs", attrs)
    assert info["workdir"] == "/storage/x/wd"
    assert "not found" in info["exit_meaning"]


def test_default_queue_always_allowed():
    lim = LimitsConfig()
    out = Resources(queue="elixircz@pbs-m1.metacentrum.cz").validate(lim, default_queue="elixircz@pbs-m1.metacentrum.cz")
    assert out["queue"] == "elixircz@pbs-m1.metacentrum.cz"
    with pytest.raises(PolicyError, match="omit it to use the default"):
        Resources(queue="gpu").validate(lim, default_queue="elixircz@pbs-m1.metacentrum.cz")


def test_tool_defaults_come_from_config():
    from metacentrum_mcp.config import DefaultsConfig
    from metacentrum_mcp.server import _resources

    d = DefaultsConfig()
    r = _resources(d, None, None, None, None, None, None, None)
    assert (r.ncpus, r.mem, r.walltime, r.scratch, r.queue) == (1, "4gb", "01:00:00", "10gb", d.queue)
    r = _resources(d, 2, "8gb", None, "none", None, None, None, interactive=True)
    assert r.scratch is None and r.walltime == d.interactive_walltime and r.ncpus == 2
