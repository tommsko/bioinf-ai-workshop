# Bioinformatics with agentic AI: turning the dial

Hands-on workshop material. You drive an AI coding agent ([opencode](https://opencode.ai)) through a small
but real bacterial resequencing project on the Czech national grid
([MetaCentrum](https://metavo.metacentrum.cz/)), using open models hosted by
[e-INFRA CZ](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/ai-api).

**The question:** does *E. coli* isolate **Ec510423** carry fluoroquinolone-resistance mutations in
*gyrA*/*parC*? ATCC 25922 is the susceptible control. The answer is published, so we can check the
agent's work against the truth.

**What you learn:** how much autonomy to give an agent, and how to make its work checkable:
- scoped tools (an MCP server instead of a raw shell)
- context hygiene
- checkpoints on disk
- validation
- pre-registered decisions
- a pipeline that reruns without the agent

| You need | Time | When |
|---|---|---|
| A MetaCentrum account | minutes to fill in; approval can take **days** | **before the workshop** |
| Membership in the `bioinf-fi` group (project storage) | the instructor adds you | **before the workshop**: send your login |
| An e-INFRA CZ LLM API key | 2 min | before the workshop |
| The software setup below | 20–30 min | before the workshop |

---

## What is in this repository

```
.
├── README.md                    ← you are here: setup
├── PROMPTS.md                   ← every workshop prompt, ready to copy
├── Bioinformatics agentic AI workshop - full deck.pptx.pdf   ← the slides
├── opencode.json                ← opencode project config: e-INFRA models, permissions, MCP server
├── .opencode/
│   ├── skills/align-reads/      ← a skill (SKILL.md + the tested align.sh job script)
│   └── agents/rule-writer.md    ← an agent that interviews you to write DECISION.md, blind to the data
├── metacentrum-mcp/             ← the MCP server: the agent's narrow, guarded door to MetaCentrum
└── workshop-env/                ← how the shared conda environment on the cluster was built (reference)
```

During the workshop the agent creates `AGENTS.md`, `DECISION.md` and `figures/` in this folder. Your
data and results live **on the cluster**, in your own project directory.

---

## Setup

Follow the steps in order. Commands are for a terminal: on Linux or macOS your usual terminal, on Windows
the **Ubuntu (WSL)** terminal from step 0.

### 0. Windows only: install WSL2

The MetaCentrum MCP server needs OpenSSH connection sharing and POSIX file locks, which native Windows
does not provide. Run everything (opencode included) inside WSL2.

1. Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. Reboot, open **Ubuntu** from the Start menu, and create a Linux user when asked.
3. From now on, use the Ubuntu terminal and follow the **Linux** commands.
4. Keep the workshop folder inside the Linux filesystem (`~/…`), not under `/mnt/c/…`; the latter is slow.

### 1. Basic tools: git, curl, jq, OpenSSH, uv

**Linux (Debian/Ubuntu, also WSL):**
```bash
sudo apt update && sudo apt install -y git curl jq openssh-client
```
**Linux (Fedora):** `sudo dnf install -y git curl jq openssh-clients`
**Linux (Arch):** `sudo pacman -S --needed git curl jq openssh`

**macOS:**
1. Install [Homebrew](https://brew.sh) if you don't have it.
2. Run `brew install git jq`. curl and OpenSSH ship with macOS.

**Then, on all systems,** install [uv](https://docs.astral.sh/uv/), the Python package manager:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Close and reopen the terminal, then check: `uv --version`.

### 2. Get this repository

```bash
git clone https://github.com/tommsko/bioinf-ai-workshop.git
cd bioinf-ai-workshop
```

### 3. Install opencode **version 2**

This workshop uses **opencode 2**. Its config format differs from version 1. The main install command on
opencode.ai installs version 1, so use one of these instead:

```bash
curl -fsSL https://opencode.ai/v2/install | bash        # Linux, macOS, WSL
# macOS alternative: brew install anomalyco/tap/opencode-v2
```

Reopen the terminal and check:
```bash
opencode --version        # must print v2.x (tested with v2.0.32)
```
If it prints 1.x, you have opencode 1 first on your `PATH`. The two cannot be installed side by side under
the same command, so remove version 1 or put `~/.opencode/bin` first on your `PATH`. See
[opencode.ai/v2/docs](https://opencode.ai/v2/docs/).

### 4. Install OpenAgentsControl (the `core/openagent` agent)

[OpenAgentsControl](https://github.com/darrenhinde/OpenAgentsControl) adds ready-made agents. We use
**OpenAgent**, a general agent that proposes a plan and asks for approval before executing.

Install it **globally**, not into this repository:
```bash
cd ~
curl -fsSL https://raw.githubusercontent.com/darrenhinde/OpenAgentsControl/main/install.sh \
  | bash -s developer --install-dir="$HOME/.config/opencode"
cd -
```
- The installer needs `curl` and `jq` from step 1.
- **Why global:** by default it installs into `./.opencode` of the current folder, which would mix its
  files into this repository.
- OpenAgentsControl documents opencode 1. OpenAgent works with opencode 2 in our testing.

### 5. e-INFRA CZ LLM API key, connected to opencode

The models run on Czech academic infrastructure. Any MetaCentrum or Masaryk University account can
use them.

1. **Get a key.**
   1. Log in at <https://chat.ai.e-infra.cz>.
   2. Open **Settings → Account → API keys**.
   3. Ignore the JWT token. Create (or show) an **API key** and copy it.
   4. Treat it like a password: don't share it, don't commit it.

   Details: [AI API docs](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/ai-api).
2. **Give it to opencode.** The provider is already configured in this repo's `opencode.json`
   (`litellm`, base URL `https://llm.ai.e-infra.cz/v1`). You only add the key:
   ```bash
   opencode          # in the repository folder
   ```
   In opencode, type `/connect`, scroll to the end of the list, choose **LiteLLM**, and paste the key.
   opencode stores it in its own credential store, not in the repository. See
   [LLM integration docs](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/llm-integration).
3. **Check.** Type `/model kimi-k3`, then say *hello*. You should get an answer.
   - If a model errors with "cannot connect", it is probably down. Pick another from
     [the status page](https://llm.ai.e-infra.cz/status/) with `/model`.
   - Your usage is at <https://llm.ai.e-infra.cz/usage>.

Models configured in `opencode.json`:
- **Tested in the workshop:** `kimi-k3` (default), `glm-5.3`, `deepseek-v4.1-flash`, `qwen3.8-flash-next`
- **Others:** `glm-5.3-flash`, `deepseek-v4.1-flash-thinking`, `qwen3.8-27b`, `mistral-medium-3.5`,
  `gemma4`, `gpt-oss-120b`
- **Aliases** that e-INFRA re-points as models change: `agentic`, `coder`, `thinker`, `mini`

### 6. MetaCentrum account

1. **Register** at <https://metavo.metacentrum.cz/en/application/index.html>.
   - It is for students and staff of Czech academic and research institutions.
   - You verify your identity through your institution (eduID.cz) and choose a **login and password**.
   - Approval is manual: **apply days ahead**. You can check the state of your application on the same site.
2. **Send your login to the instructor,** who adds you to the **`bioinf-fi`** group. That gives you a
   project directory on `/storage/praha5-elixir/projects/bioinf-fi/` (more than 50 TB free; your home
   quota is small) and read access to the shared software environment.
3. **Optional:** membership in the **`elixir`** group lets jobs use the ELIXIR CZ queue, which is the
   default. If you are not in it, the MCP server warns you; switch to the general queue as shown in step 7.

### 7. Install and test the MetaCentrum MCP server

The MCP server keeps one shared SSH connection to the MetaCentrum frontend and offers the agent
about 25 narrow tools: submit a PBS job, check it, read a file, and so on. It never offers a raw shell. Your password
goes into **your** terminal only; the agent never sees it.

```bash
uv tool install ./metacentrum-mcp       # from the repository folder
uv tool update-shell                    # puts ~/.local/bin on your PATH; then REOPEN the terminal
metacentrum-mcp --help                  # check it is found
```

Tell it your login:
```bash
mkdir -p ~/.config/metacentrum-mcp
cat > ~/.config/metacentrum-mcp/config.toml <<'EOF'
[connection]
host = "skirit.metacentrum.cz"
user = "YOUR_METACENTRUM_LOGIN"
control_persist = "12h"
EOF
```
- **Not in the `elixir` group?** Append these lines to use MetaCentrum's general queue:
  ```toml
  [defaults]
  queue = ""
  ```
- **On WSL or Linux with fish:** `uv tool update-shell` may not cover fish. Run
  `fish_add_path ~/.local/bin` instead.

Log in once per day:
```bash
metacentrum-mcp login     # first time: answer "yes" to the host key question, then type your password
metacentrum-mcp status    # connection up, Kerberos ticket, workspace, default queue
```
The connection stays open 12 hours after its last use. When it expires, the agent tells you to run
`metacentrum-mcp login` again. It never retries a rejected password by itself.

Full end-to-end test. It runs a tiny batch job and a tiny interactive job, about 1–3 minutes:
```bash
metacentrum-mcp selftest -i     # expect: "20/20 checks passed"
```

Check that opencode sees the server (from the repository folder):
```bash
opencode mcp list         # expect: ✓ metacentrum  connected
```
More details, including SSH-key login: [metacentrum-mcp/README.md](metacentrum-mcp/README.md).

### 8. Start the workshop

From the repository folder:
```bash
metacentrum-mcp login                    # if not logged in today
OPENCODE_ENABLE_EXA=1 opencode           # EXA enables the web-search tool
```
In opencode:
1. Run `/model kimi-k3`.
2. Run `/agents` and pick **core/openagent**.
3. Paste **Prompt #1** from [PROMPTS.md](PROMPTS.md).

It should answer with your MetaCentrum username and your workspace
`/storage/praha5-elixir/projects/bioinf-fi/<you>`. You are ready.

---

## What is preconfigured, and why

**`opencode.json`, permissions.** Everything is denied unless listed:

| Allowed | Why |
|---|---|
| `edit`, `write` | the agent edits files in this project |
| `execute` | in opencode 2, **all MCP tools are reached through this one tool**; without it the agent sees no MetaCentrum tools |
| `metacentrum_*` | lets each MetaCentrum tool call through |
| `skill`, `question`, `websearch`, `webfetch`, `subagent` | skills, asking you questions, web research, helper agents |

OpenAgent adds its own rule: shell commands **ask** first. Heavy compute cannot run on the frontend at
all, because the MCP server has no general shell. Jobs go through PBS, within caps:
- 16 CPUs, 64 GB and 24 h per job
- 5 active jobs at a time
- 2 interactive sessions at a time

**`.opencode/skills/align-reads/`.** A skill: instructions plus a fixed, tested job script. Use it with
`@align-reads` in a prompt, or pick it from `/skills`.

**`.opencode/agents/rule-writer.md`.** An agent that researches resistance mutations and interviews you,
one question at a time, to write `DECISION.md` **before** you look at the results. Its permissions shut
out the cluster and all project files, so it cannot peek. Switch to it with `/agents`.

**`metacentrum-mcp` defaults,** which the agent can query with `cluster_info`:
- your workspace `/storage/praha5-elixir/projects/bioinf-fi/<login>`, enforced
- queue `elixircz@pbs-m1.metacentrum.cz`
- jobs default to 1 CPU, 4 GB, 1 h and 10 GB local scratch
- the shared conda environment (`fastp`, `seqkit`, `bwa-mem2`, `samtools`, `bcftools`, `mosdepth`, …),
  sourced automatically in every job

## Optional extras shown in the slides

Add these to your **global** `~/.config/opencode/opencode.json` (`"plugins": [...]`) if you want to try them:
- [cc-safety-net](https://github.com/kenryu42/cc-safety-net): blocks destructive shell commands
  (`"cc-safety-net@latest"`)
- [opencode-mem](https://github.com/tickernelz/opencode-mem): long-term memory across sessions
  (`"opencode-mem@latest"`)

## Troubleshooting

| Symptom | Fix |
|---|---|
| The agent says it has no MetaCentrum/MCP tools and starts searching files | Restart opencode in the repository folder. Check `opencode --version` is 2.x and `opencode mcp list` shows `connected`. The project `opencode.json` must allow `execute`. |
| `opencode mcp list`: "No MCP servers configured" | You are on opencode 1, or not in the repository folder. |
| `metacentrum-mcp: command not found` (in the terminal or in opencode) | Run `uv tool update-shell` (fish: `fish_add_path ~/.local/bin`), then reopen the terminal **and** opencode. |
| Tools say "not connected" or "login required" | Run `metacentrum-mcp login` in your terminal. |
| `status` warns: no Kerberos ticket / workspace unusable | Run `metacentrum-mcp login` (password login gets a ticket) or `metacentrum-mcp kinit`. |
| `status` warns: not in group `elixir` | Set `[defaults] queue = ""` in `~/.config/metacentrum-mcp/config.toml` (step 7). |
| Workspace permission denied | You are not in `bioinf-fi` yet: ask the instructor. |
| Model error 500, "cannot connect to host" | That model is down. Pick another with `/model`; see [status](https://llm.ai.e-infra.cz/status/). |
| `401`, "No api key passed" | Run `/connect` → LiteLLM again and paste the key. |
| `core/openagent` missing from `/agents` | Rerun step 4. Check `~/.config/opencode/agent/core/openagent.md` exists. |
| Native Windows | Not supported. Use WSL2 (step 0). |

## Credentials and data

- **MetaCentrum password:** typed only into `metacentrum-mcp login` in your own terminal. The model
  never sees it; the server only uses the open SSH connection.
- **e-INFRA API key:** kept in opencode's credential store (`/connect`), not in this repository.
- **Data:** stays on MetaCentrum. Only small summaries and figures come back to your computer.
- **Audit:** every tool call is logged locally. See it with `metacentrum-mcp audit`.

## Links

- opencode 2 docs: <https://opencode.ai/v2/docs/>
- OpenAgentsControl: <https://github.com/darrenhinde/OpenAgentsControl>
- MetaCentrum registration: <https://metavo.metacentrum.cz/>, docs: <https://docs.metacentrum.cz/>
- e-INFRA CZ AI API: <https://docs.cerit-sc.cz/en/docs/ai-as-a-service/ai-api>
- opencode with e-INFRA: <https://docs.cerit-sc.cz/en/docs/ai-as-a-service/llm-integration>
- Model status: <https://llm.ai.e-infra.cz/status/>
