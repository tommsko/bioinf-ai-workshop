---
name: align-reads
description: Align QC'd paired-end reads to the reference on MetaCentrum (bwa-mem2 + samtools) and produce a sorted, duplicate-marked, indexed BAM per sample, as PBS batch jobs through the metacentrum MCP tools. Use for the "align" step of the resequencing project.
---

# Align reads → sorted, indexed BAM

Everything runs on the cluster through the **metacentrum MCP tools**. Never run bwa-mem2 or
samtools on the frontend, and never on this laptop. The job script is fixed: `align.sh`, next to
this file. Use it unchanged, so that every run is identical and reruns without an agent.

## 1. Find the inputs (do not guess paths)
- Read `PROGRESS.md` in the project directory on the cluster (`read_file`). From it, take:
  - the reference FASTA (from the fetch step)
  - for each sample, the QC'd R1 and R2 reads (from the QC step, *not* the raw reads)
- Check that each path exists with `list_dir`.
- If a path is missing or ambiguous, stop and ask the user. Do not substitute other files.

## 2. Stage the script
Upload `align.sh` from this skill directory to `scripts/align.sh` in the project
(`upload_file`, `overwrite: true`). If the upload is refused, read `align.sh` and create it with
`write_file` instead, unchanged. The project's `scripts/` must hold everything that ran.

## 3. Index the reference (one job, once)
`submit_job` with `name: "align-index"`, `workdir` = the project directory,
`ncpus: 2`, `mem: "8gb"`, `walltime: "00:30:00"`, and the script:

    bash scripts/align.sh index <ref.fa>

It is a no-op if the index already exists. Do not index inside the per-sample jobs: two jobs
writing the same index files at once corrupt them.

## 4. Align each sample (one job per sample)
For every sample, call `submit_job` with:
- `name: "align-<sample>"`, `workdir` = the project directory
- `ncpus: 8`, `mem: "16gb"`, `walltime: "01:00:00"`, `scratch: "20gb"`
- `depends_on` = the index job id
- script:

      bash scripts/align.sh align <sample> <ref.fa> <R1> <R2> results/<sample>/aln

Submit all samples before waiting; they run in parallel.

## 5. Wait and verify
- Use `wait_for_job` (call again while `still_waiting`), never a polling loop.
- **On failure:** read `job_output` with `stream: "stderr"` first, then report the error.
  Do not change `align.sh` or the resources without asking the user.
- **On success:** check exit status 0, then `list_dir results/<sample>/aln`. It should contain
  `<sample>.bam` and `<sample>.bam.bai`, both non-empty. The job already ran
  `samtools quickcheck`.
- **Do not inspect the alignments here.** No viewing BAM/SAM content and no flagstat or depth.
  That is the next step (inspect).

## 6. Record
Append to `PROGRESS.md` (`write_file`; keep the existing content):
- the job ids (index + per sample)
- the exact command lines
- the output BAM paths
- the resources used

Report to the user a short table with these columns: sample, job id, state, exit status, BAM path.

## Don'ts
- No alignment on the frontend, and no `frontend_exec` for anything but looking around.
- No raw reads; only the QC'd reads recorded in `PROGRESS.md`.
- No resubmitting a finished job (`resubmit: true`) unless the user asks.
- No printing reads, SAM or BAM records into the conversation.
