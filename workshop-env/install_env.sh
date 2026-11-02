#!/bin/bash
# (Re)build the shared workshop environment. Meant to run as a PBS job (needs $SCRATCHDIR, ~4 CPUs, 16 GB):
#   qsub -l select=1:ncpus=4:mem=16gb:scratch_local=40gb -l walltime=3:00:00 -- install_env.sh
# Idempotent: installs Miniforge once, then creates or updates the env from environment.yml.
set -euo pipefail
ROOT=${WORKSHOP_ROOT:-/storage/praha5-elixir/projects/bioinf-fi/tomsko/workshop}
MF="$ROOT/miniforge"
ENV="$MF/envs/workshop"
# Package cache on fast node-local scratch, not on NFS.
export CONDA_PKGS_DIRS="${SCRATCHDIR:?run inside a PBS job with scratch}/pkgs"
cd "$SCRATCHDIR"

if [ ! -x "$MF/bin/conda" ]; then
  echo "== installing Miniforge into $MF"
  curl -fsSL -o miniforge.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
  bash miniforge.sh -b -p "$MF"
fi
# Keep users' personal ~/.condarc from leaking into the shared install.
cat > "$MF/.condarc" <<'RC'
channels: [conda-forge, bioconda]
channel_priority: strict
auto_activate_base: false
RC
source "$MF/etc/profile.d/conda.sh"

if [ -d "$ENV/conda-meta" ]; then
  echo "== updating $ENV"
  conda env update -p "$ENV" -f "$ROOT/environment.yml" --prune
else
  echo "== creating $ENV"
  conda env create -y -p "$ENV" -f "$ROOT/environment.yml"
fi

conda activate "$ENV"
conda env export -p "$ENV" --no-builds | grep -v '^prefix:' > "$ROOT/environment.lock.yml"
{
  echo "# Tools in the shared workshop env (generated $(date -I) by install_env.sh)"
  conda list -p "$ENV" | awk '$1 ~ /^(fastp|seqkit|bwa-mem2|minimap2|samtools|bcftools|htslib|bedtools|mosdepth|fastqc|multiqc|snakemake-minimal|ncbi-datasets-cli|sra-tools|pysam|biopython|pandas|matplotlib|r-ggplot2)$/ {printf "%-18s %s\n", $1, $2}'
  printf '%-18s %s\n' python "$(python -c 'import sys; print(sys.version.split()[0])')"
  printf '%-18s %s\n' R "$(R --version | head -1 | awk '{print $3}')"
} > "$ROOT/TOOLS.txt"
cat "$ROOT/TOOLS.txt"

# Group (bioinf-fi) access comes from inherited NFSv4 ACLs; make sure modes don't mask it.
chmod -R g+rwX "$MF" 2>/dev/null || true
echo "== done"
