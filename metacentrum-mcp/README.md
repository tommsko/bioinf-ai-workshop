# metacentrum-mcp

An MCP server that gives agent harnesses (opencode, Claude Code, Goose, …) a **narrow, guarded door** to
the [MetaCentrum](https://docs.metacentrum.cz/) grid. It provides:
- a persistent SSH gate to a frontend
- PBS batch jobs
- interactive jobs bridged as a terminal
- bounded file access in your project directory on the ELIXIR storage

The model sees tool names and small structured results. It never sees your password or keys, never gets a
raw shell on the login node, and never gets more resources than the config allows.

```
 your computer                                       frontend (skirit)                    compute node
┌──────────────────────────────┐   one SSH login   ┌───────────────────────────────┐
│ opencode ─┐ stdio            │  (ControlMaster,  │ bash -l (per tool call)       │
│ opencode ─┼─ metacentrum-mcp ─┼── multiplexed) ──▶│  qsub / qstat / qdel          │──▶ batch job
│ claude   ─┘   │              │                   │  tmux -L mcp                  │
│               ├ state.db     │                   │   └ qsub -I ──────────────────┼──▶ interactive shell
│               └ audit.jsonl  │                   │ /storage/praha5-elixir/...    │
└──────────────────────────────┘                   └───────────────────────────────┘
```

---

## 1. Install

You need: a MetaCentrum account (in the `elixir` group for the default queue, and in `bioinf-fi` for
the project storage), [uv](https://docs.astral.sh/uv/), OpenSSH, and an MCP-capable harness such as
[opencode](https://opencode.ai).

### Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv (skip if installed)
uv tool install /path/to/metacentrum-mcp                 # puts `metacentrum-mcp` in ~/.local/bin
curl -fsSL https://opencode.ai/install | bash            # opencode (or your harness of choice)
```

If `metacentrum-mcp` is not found, add `~/.local/bin` to `PATH` (`uv tool update-shell`, or
`fish_add_path ~/.local/bin` in fish).

### macOS

Same as Linux. macOS ships OpenSSH with connection sharing, which is all the server needs.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # or: brew install uv
uv tool install /path/to/metacentrum-mcp
brew install sst/tap/opencode                            # or the curl installer above
```

### Windows: use WSL2

The server relies on OpenSSH connection sharing (ControlMaster) and POSIX file locks. The OpenSSH
shipped with Windows does not support either, so the server refuses to run natively on Windows. Use WSL2:

```powershell
wsl --install -d Ubuntu          # in an administrator PowerShell, then reboot and open "Ubuntu"
```

Inside the Ubuntu terminal, follow the **Linux** steps, and run opencode inside WSL too. The harness and
the MCP server must live on the same side. Your Windows files are at `/mnt/c/Users/<you>/…`, but keep the
project inside the WSL filesystem (`~/…`) for speed.

## 2. Configure

```bash
mkdir -p ~/.config/metacentrum-mcp
cat > ~/.config/metacentrum-mcp/config.toml <<'EOF'
[connection]
host = "skirit.metacentrum.cz"
user = "YOURLOGIN"
control_persist = "12h"
EOF
```

Every other option has a workshop-ready default; see `config.example.toml` for all of them.
`metacentrum-mcp config` prints the effective configuration.

## 3. Log in (credentials never go to the model)

Pick one method. You do this in your own terminal, not through the agent.

**A. Password (simplest).**

```bash
metacentrum-mcp login        # asks for your MetaCentrum password once, opens the shared connection
```

A password login also gives the frontend session a Kerberos ticket, which the project storage needs. The
connection stays open for `control_persist` (12 h above) after its last use. When it expires, run
`login` again.

**B. SSH key.**

```bash
ssh-keygen -t ed25519                                   # if you have no key yet
ssh-copy-id YOURLOGIN@skirit.metacentrum.cz             # asks for the password one last time
metacentrum-mcp login                                   # now passwordless
metacentrum-mcp kinit                                   # key logins carry NO Kerberos ticket: get one
```

With a key, the server can reopen a dropped connection by itself. Without a Kerberos ticket,
`/storage/praha5-elixir` (a different site from skirit's home) and PBS commands can fail. `kinit` fixes
that for about 10 h; `connection_status` shows the remaining time.

**C. Local Kerberos (advanced).** With MetaCentrum's `krb5.conf`, run `kinit YOURLOGIN@META` on your own
computer. GSSAPI forwarding is on by default, so the frontend receives the ticket.

Then check everything end to end:

```bash
metacentrum-mcp status
metacentrum-mcp selftest -i      # ~1–3 min: tiny batch + interactive job; expect "20/20 checks passed"
```

## 4. Connect your harness

### opencode

opencode v2 (tested with 2.0.32) starts the server itself, as a child process speaking stdio. You only
tell it the command.

**a. Find the absolute path of the server.** opencode does not always inherit your shell's `PATH`, so a
bare `metacentrum-mcp` may not be found:

```bash
command -v metacentrum-mcp       # typically /home/<you>/.local/bin/metacentrum-mcp (macOS: /Users/<you>/…)
```

**b. Add it to opencode.** Either let opencode write the entry:

```bash
opencode mcp add --global --env METACENTRUM_MCP_MAX_WAIT=120 metacentrum -- /home/<you>/.local/bin/metacentrum-mcp serve
```

or edit the config by hand. The file is `~/.config/opencode/opencode.json` (or `opencode.jsonc`) for all
projects, or `opencode.json` in a project root for that project only. Merge the `mcp` block into what is
already there; keep your other keys (`plugins`, `model`, …):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "metacentrum": {
        "type": "local",
        "command": ["/home/<you>/.local/bin/metacentrum-mcp", "serve"],
        "environment": { "METACENTRUM_MCP_MAX_WAIT": "120" },
        "timeout": 180000
      }
    }
  }
}
```

The same snippet is in `opencode.example.json`. `opencode mcp add` has no flag for `timeout`, so add that line by hand afterwards.
`METACENTRUM_MCP_MAX_WAIT` (seconds a `wait_*` tool may block) must stay below the harness `timeout`
(ms). The built-in defaults (25 s / 30 s) are safe. The values above let `wait_for_job` block for 2
minutes per call, which saves the model tokens while it waits for a job.

> **Shape matters.** v2 reads `"mcp": {"servers": {...}}`. The opencode website still shows the v1
> shape `"mcp": {"metacentrum": {...}}`, which v2 silently ignores ("No MCP servers configured").

> **Permissions matter.** In v2 the model reaches every MCP tool through one built-in tool, `execute`
> ("code mode"), and each call is then checked as `metacentrum_<tool>`. With a restrictive
> `"permission": {"*": "deny", ...}`, allow both, or the model sees no MetaCentrum tools at all and
> goes searching local files instead:
>
> ```json
> "permission": { "*": "deny", "execute": "allow", "metacentrum_*": "allow" }
> ```

**c. Log in, then verify.** The server uses the SSH connection from step 3; it never logs in with a
password itself.

```bash
metacentrum-mcp status           # connection up?
opencode mcp list                # expect: ✓ metacentrum connected
```

Then start opencode and ask *"call cluster_info"*. The answer should show the default queue
`elixircz@pbs-m1.metacentrum.cz`, your workspace `/storage/praha5-elixir/projects/bioinf-fi/<you>`
and the shared workshop software environment. If the connection is down, the tool says so and tells you to
run `metacentrum-mcp login` in your own terminal.

### Claude Code

```bash
claude mcp add metacentrum -- /home/<you>/.local/bin/metacentrum-mcp serve
```

---

## What the model gets

### Defaults it can rely on (and discover with `cluster_info`)

| | Default | Why |
|---|---|---|
| Working directory | `/storage/praha5-elixir/projects/bioinf-fi/<user>` | project storage with 100+ TB free; created on first connect |
| Relative paths | resolve to the workspace | the model never needs to guess absolute paths |
| Queue | `elixircz@pbs-m1.metacentrum.cz` | ELIXIR CZ routing queue → `elixir_1d` (≤24 h) / `elixir_2w` |
| Resources | 1 CPU, 4 GB, 1 h, 10 GB local scratch (`$SCRATCHDIR`) | small and fast to schedule |
| Interactive walltime | 1 h (cap 4 h) | interactive jobs hold resources while idle |
| Software | shared conda env `…/tomsko/workshop/activate.sh`, sourced in every job and interactive shell | `fastp`, `seqkit`, `bwa-mem2`, `minimap2`, `samtools`, `bcftools`, `bedtools`, `mosdepth`, `snakemake`, `ncbi-datasets`, Python + R plotting… |

`cluster_info` returns all of this in one call, along with hard limits, live queue load, Kerberos
lifetime, the tool versions of the shared environment, and site notes from the config (`[defaults] notes`).
`connect` reminds the model to call it.

### Tools

| Tool | Returns | Guardrail |
|---|---|---|
| `connect` / `connection_status` / `disconnect` | workspace, Kerberos, warnings | allowed frontends only; disconnect is sticky across agents |
| `cluster_info` | defaults, limits, software env, queue load, notes | read-only |
| `submit_job` | job id | caps (walltime/CPU/mem/scratch/GPU), queue allowlist, ≤ N active jobs, submissions/hour; **same name ⇒ same job**; no `#PBS` lines in the body; workdir inside the workspace |
| `job_status` | state, exit status **with meaning**, used vs requested, `likely_cause` | structured, not raw qstat |
| `wait_for_job` | final state or `still_waiting` | replaces polling loops; blocking capped |
| `job_output` | last N lines, optional regex filter | tail by default; live from the node while running |
| `cancel_job` | confirmation | only jobs this server submitted |
| `list_jobs` | this server's jobs, or all of the user's | — |
| `interactive_start` / `_status` / `_list` / `_stop` | session id, job id, state | lower walltime cap, max sessions |
| `interactive_run` | exit code + truncated output | **only inside a running interactive job**; bash syntax pre-check; one command at a time per session |
| `interactive_wait` / `_read` / `_send` | result / screen / screen | special keys allowlisted (C-c, C-d, …) |
| `list_dir` / `read_file` | entries / first N bytes + size | byte caps, binary refused, `.gz` transparently |
| `write_file` / `make_dir` / `upload_file` / `download_file` | path, size | writes only in the workspace; size caps; local side limited to the harness project |
| `frontend_exec` | output | allowlisted read-only programs (`module avail`, `which`, `ls`, `qstat`, …), no shell syntax |
| `audit_log` | recent calls by all agents | — |

### Batch jobs

`submit_job` takes a bash body without `#PBS` lines; resources are parameters. The server writes a
complete PBS script to `<workdir>/.mcp/jobs/` that can be **rerun without the agent**:
`cd <workdir> && qsub <script>`. The script includes:
- `set -e -o pipefail` (unless `strict=false`)
- `trap clean_scratch`
- `cd $WORKDIR`
- the shared software env
- a header line on stderr

`depends_on` maps to `-W depend=afterok:…`. Exit codes come back explained (127 means command not
found; 271 means killed by PBS, plus whether walltime or memory ran out).

### Interactive jobs (the terminal bridge)

`interactive_start` runs `qsub -I` inside a detached `tmux -L mcp` session on the frontend, so it survives
agent restarts, dropped connections and a sleeping laptop. All output is logged to
`<workspace>/.mcp/interactive/<id>.log`. `interactive_run` types the command with start/end markers (the
end marker carries `$?`) and returns only that command's cleaned, truncated output. Shell state persists
between commands. When the job ends, the pane dies, so nothing can fall through to the login node.

Humans can watch the agent's terminal live (great on a projector) or take over:

```bash
metacentrum-mcp attach <session_id>        # read-only; --rw to type; Ctrl-b d to detach
```

### Reliability

| Situation | Behaviour |
|---|---|
| Many agents/harnesses at once | one shared SSH login; SQLite registry (WAL) and file locks serialise logins, submissions (cap + idempotency check + qsub) and each interactive session |
| MCP server crash/restart | the SSH master runs outside the server; a new server reuses it; interactive shells live on in tmux |
| Network drop / laptop sleep | dead master detected, reopened on the next call (automatically with a key; after `login` with a password); `qsub` never retried blindly |
| `disconnect` | closes the shared connection for everyone; no agent silently reconnects until `connect` |
| Job vanishes from qstat (>24 h) | last known state from the local registry; output files stay in the workdir |

### What this is not

The guardrails bound **where** things go and **how much** is requested. They are not a sandbox: job
scripts and interactive commands are arbitrary code running under your grid identity. Combine the server
with harness permissions (for example, approval for `submit_job`), and read `.mcp/jobs/*.pbs` and the
audit log (`metacentrum-mcp audit`).

---

## Testing

```bash
uv run pytest -q                                          # unit + end-to-end on a fake cluster (no account needed)
metacentrum-mcp selftest -i                               # live: the real cluster through MCP stdio
METACENTRUM_LIVE=interactive uv run pytest -s tests/test_live.py   # the same, as a pytest
```

The fake cluster (`tests/fakecluster/`) is an unprivileged `sshd` on localhost with fake
`qsub`/`qstat`/`qdel` where jobs really run, honour walltime and dependencies, and `qsub -I` gives a shell.
The end-to-end suite covers:
- defaults, `cluster_info` and the software env
- workspace policy, upload and download
- caps, queue policy, idempotency, dependencies, walltime kills and cancel
- interactive run, timeout/wait and C-c
- a SIGKILLed server resuming its sessions
- two agents racing to submit the same job
- sticky disconnect

The live selftest submits a 5-minute, 1-CPU job and (with `-i`) a 10-minute interactive job, and checks
every layer, from Kerberos to exit codes.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `authentication failed (BatchMode…)` | the shared connection is gone and there is no key: `metacentrum-mcp login` |
| warning `No valid Kerberos ticket` / `workspace … not usable` | `metacentrum-mcp kinit`, then call `connect` again |
| jobs rejected by the `elixircz` queue | you are not in the `elixir` group: set `[defaults] queue = ""` |
| tool calls time out in the harness | raise the harness MCP `timeout`, keep `METACENTRUM_MCP_MAX_WAIT` below it |
| connection seems stuck | `metacentrum-mcp disconnect && metacentrum-mcp login` |
| refused: `outside the writable roots` | intended: data and scripts belong in the workspace, not home (quota) |
| Windows: "needs a POSIX system" | run everything inside WSL2 |

## Files on your computer

`~/.local/state/metacentrum-mcp/` holds:
- `state.db`: job and session registry
- `audit.jsonl`: every tool call, with long content elided
- `master-*.log`

Control sockets live in a short private directory (`$XDG_RUNTIME_DIR` or `/tmp/metacentrum-mcp-$UID`)
because of the Unix socket path limit.

## CLI

```
metacentrum-mcp serve | login [--host H --user U] | kinit | status | selftest [-i]
                | attach <sid> [--rw] | audit [-n N] | disconnect | config
```
