#!/usr/bin/env bash
# Start a throwaway "frontend": an unprivileged sshd on localhost plus fake PBS commands.
# Usage: up.sh <work_dir> [port]   -> writes <work_dir>/config.toml for metacentrum-mcp
#        down: kill "$(cat <work_dir>/sshd.pid)"
set -euo pipefail
W=$(realpath -m "$1"); PORT=${2:-22922}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$W"/{bin,pbs,state,project}
chmod 700 "$W"
[ -f "$W/host_key" ] || ssh-keygen -q -t ed25519 -N '' -f "$W/host_key"
[ -f "$W/user_key" ] || ssh-keygen -q -t ed25519 -N '' -f "$W/user_key"
cp "$W/user_key.pub" "$W/authorized_keys"
for c in qsub qstat qdel; do ln -sf "$HERE/fake_pbs.py" "$W/bin/$c"; done
cat > "$W/bin/clean_scratch" <<'EOF'
#!/bin/bash
[ -n "${SCRATCHDIR:-}" ] && rm -rf "$SCRATCHDIR"
EOF
chmod +x "$W/bin/clean_scratch" "$HERE/fake_pbs.py"
mkdir -p "$W/project/shared"
echo 'export WORKSHOP_ENV=fake' > "$W/project/shared/activate.sh"
printf 'samtools       1.21\nbcftools       1.21\n' > "$W/project/shared/TOOLS.txt"
cat > "$W/sshd_config" <<EOF
Port $PORT
ListenAddress 127.0.0.1
HostKey $W/host_key
PidFile $W/sshd.pid
AuthorizedKeysFile $W/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes no
MaxSessions 50
MaxStartups 50
Subsystem sftp internal-sftp
EOF
/usr/bin/sshd -f "$W/sshd_config" -E "$W/sshd.log"
cat > "$W/config.toml" <<EOF
state_dir = "$W/state"

[connection]
host = "127.0.0.1"
user = "$(id -un)"
port = $PORT
allowed_hosts = ["127.0.0.1"]
identity_file = "$W/user_key"
gssapi = false
control_persist = "10m"
ssh_options = ["UserKnownHostsFile=$W/known_hosts", "IdentitiesOnly=yes", "IdentityAgent=none"]

[workspace]
project_root = "$W/project"

[limits]
max_wait_seconds = 30

[defaults]
queue = "fastq"
software_env = "$W/project/shared/activate.sh"
notes = ["fake test cluster"]

[remote_env]
PATH = "$W/bin:\$PATH"
FAKE_PBS_DIR = "$W/pbs"
FAKE_PBS_QUEUE_DELAY = "1"
EOF
echo "$W/config.toml"
