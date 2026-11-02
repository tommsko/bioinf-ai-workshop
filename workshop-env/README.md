# Workshop software environment

Shared conda environment at `/storage/praha5-elixir/projects/bioinf-fi/tomsko/workshop`
(readable for the `bioinf-fi` group).

    source /storage/praha5-elixir/projects/bioinf-fi/tomsko/workshop/activate.sh

The MCP server sources it automatically in every batch job and interactive session
(`[defaults] software_env` in its config). Tool versions: `TOOLS.txt`; exact pins: `environment.lock.yml`.

Rebuild/update (from this directory, on a frontend):

    qsub -q elixircz@pbs-m1.metacentrum.cz -l select=1:ncpus=4:mem=16gb:scratch_local=40gb -l walltime=3:00:00 -- /bin/bash install_env.sh
