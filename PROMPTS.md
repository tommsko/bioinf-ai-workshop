# Workshop prompts

The prompts from the slides, ready to copy. Numbers match the deck. Run them in order, in one project:
each step reads what the previous one wrote to `PROGRESS.md` on the cluster.

**Start:**
```bash
metacentrum-mcp login                 # once a day, in your own terminal
OPENCODE_ENABLE_EXA=1 opencode        # from this repository folder
```
Then in opencode, run `/model kimi-k3` and `/agents` → **core/openagent**.

---

## Concept 1 · Autonomy

**Prompt #1: connect to the cluster**
```
Use the metacentrum MCP tools to test the cluster connection: call connection_status, then
cluster_info. Tell me my MetaCentrum username and my working directory on the cluster.
Don't search local files.
```

**Prompt #2: project structure**
```
We are starting a bacterial resequencing project on MetaCentrum: does E. coli isolate Ec510423
carry fluoroquinolone-resistance mutations in gyrA/parC? ATCC 25922 is the susceptible control.

Using the metacentrum MCP tools, create the project directory `ecoli_fq` in my cluster workspace
with this layout:
  data/ref     reference genome + annotation
  data/reads   raw reads
  scripts      every script we run (jobs must be rerunnable without you)
  qc  results  logs
plus README.md (the question, samples, layout) and PROGRESS.md (a checklist of steps).
Show me the resulting tree. Do not download anything yet.
```

**Prompt #3: fetch reference and reads**
```
Fetch the inputs into ecoli_fq/data as a PBS batch job (not on the frontend):
- reference: E. coli K-12 MG1655, NCBI GCF_000005845.2, genomic FASTA + GFF (use datasets
  from the shared env)
- PE reads from ENA: Ec510423 = SRR15042476, ATCC25922 = SRR3608182 (verify FASTQ md5s
  against ENA)

Write scripts/fetch.sh, submit it, wait for it to finish. Requirements:
- Paths: MCP jobs start in the workspace root, so the submitted body must export
  PBS_O_WORKDIR=<abs path of ecoli_fq>; keep plain cd "$PBS_O_WORKDIR" in the saved script
  so qsub scripts/fetch.sh works from ecoli_fq/.
- Don't guess formats: inspect the datasets zip (unzip -l) before copying FASTA/GFF; look up
  fastq_ftp/fastq_md5 in ENA's TSV by header (accession is column 1).
- The script writes md5 report, R1/R2 read counts, and FASTA/GFF seq-ID check into logs/ and
  exits non-zero on any mismatch.

Then verify from logs + listing: files non-empty, md5s match, R1 = R2 read counts, seq IDs
equal. No trimming. Log all job ids (incl. failures) in PROGRESS.md and summarize.
```

## Concept 2 · Model ≠ harness

Try a different model for these: `/model` → for example `deepseek-v4.1-flash`, `glm-5.3` or `qwen3.8-flash-next`.

**Prompt #4: look around interactively**
```
Start a small interactive job on MetaCentrum and, inside it, find bwa-mem2 and samtools
(versions and paths) and tell me where the job's scratch directory is and how much space it has.
Then stop the interactive job.
```

**Prompt #5: QC (trim, subsample)**
```
Next step: QC. Read PROGRESS.md first to find the raw reads and the reference.

Write scripts/qc.sh as a PBS batch job, one job per sample (Ec510423, ATCC25922):
- the reads are paired-end: keep R1 and R2 in sync at every step
- subsample to ~30× coverage of the reference genome. Work out the fraction from the
  read count and length (seqkit stats) and the genome size, and use a fixed random seed
- trim and filter with fastp in paired-end mode; keep its HTML + JSON reports in qc/<sample>/
- work in $SCRATCHDIR and copy the final reads to data/qc/<sample>_R1.fastq.gz and _R2

After the jobs finish, show me a small table per sample: reads and bases before
subsampling, after subsampling, after fastp; the estimated coverage; and confirm
R1 and R2 have equal read counts. Use seqkit stats and the fastp JSON; do not print reads.
Update PROGRESS.md with the job IDs, the subsampling fraction and seed, and the output paths.
```

## Section 3 · More capable agents

**Prompt #6: web research with a helper**
```
Search the internet and do a deep research: does E. coli isolate Ec510423 carry
fluoroquinolone-resistance mutations in gyrA/parC?
```

**Prompt #7: align, using a skill.** First look at `.opencode/skills/align-reads/` and try `/skills`.
```
@align-reads Align both samples (Ec510423, ATCC25922) to the reference.
```

## Section 4 · Context

**Prompt #8: survive a context reset**
```
Before the next step, make this project survive a context reset.

1. AGENTS.md (local, in this opencode project root, so it loads in every session).
   Keep it under ~40 lines, facts and rules only:
   - project: E. coli fluoroquinolone resistance (gyrA/parC); samples Ec510423, ATCC25922
   - the cluster project directory, where data/, scripts/, results/ live; scratch is $SCRATCHDIR
   - E. coli is haploid
   - all cluster work goes through the metacentrum MCP tools, as batch jobs; nothing heavy on the frontend
   - never print reads, SAM/BAM, VCF or long logs; read summaries (flagstat, stats, seqkit stats,
     job_output tails)
   - state lives on disk: read PROGRESS.md before acting; log decisions in NOTES.md
   - stop and ask before: deleting anything, resubmitting a finished job, more than 5 jobs, any final decision

2. Harvest this conversation into NOTES.md on the cluster: every decision we made and why
   (accessions, reference, subsampling fraction and seed, tools and parameters, anything we
   rejected), plus open questions. Facts with sources, not narrative.

3. Polish PROGRESS.md on the cluster: one checklist line per step with job IDs and output paths,
   then a "Resume here" section: current state, any in-flight jobs, the exact next step.

4. Update README.md on the cluster so the layout section matches what actually exists (check with list_dir).

Show me AGENTS.md in full and a 5-line summary of what changed elsewhere.
```

**Prompt #9: compact, then resume**
```
/compact
```
then:
```
resume
```

**Prompt #10: inspect the alignments**
```
Next step: inspect the alignments. Read PROGRESS.md first.

Write scripts/inspect.sh as one small PBS batch job covering both samples. Per sample:
- samtools flagstat → results/<sample>/aln/flagstat.txt
- mosdepth (genome-wide, 10 kb bins) → results/<sample>/aln/
- one summary line per sample into results/inspect_summary.tsv: total reads, % mapped,
  % properly paired, % duplicates, mean depth, % of genome at ≥10×

Read only flagstat.txt, the mosdepth summary and inspect_summary.tsv, never the BAMs or
per-base depth. Then give me a table per sample and compare it to what we planned (~30× after
subsampling). If something is off, say what you think the cause is and what evidence supports it,
but do not change or rerun anything. Log the numbers in PROGRESS.md and any interpretation in NOTES.md.
```

## Section 5 · Checkpoints

**Prompt #11: call variants.** When the jobs are submitted, try killing opencode, then start a new session and type `resume`.
```
Next step: call variants. Read PROGRESS.md first.

Write scripts/call.sh as a PBS batch job, one job per sample (Ec510423, ATCC25922):
- bcftools mpileup against the reference, using the BAM from the align step,
  with per-sample AD and DP annotations
- bcftools call for variant sites only
- normalise with bcftools norm against the reference, write bgzipped VCF + index
  to results/<sample>/calls/<sample>.vcf.gz
- bcftools stats → results/<sample>/calls/<sample>.stats.txt
- work in $SCRATCHDIR, modest resources (e.g. 4 CPUs, 8 GB, 1 h)

Submit both jobs. IMMEDIATELY after submitting, before waiting, record in PROGRESS.md:
the job IDs, the exact command lines, the expected outputs, and update "Resume here".
Then wait for the jobs. When done, report per sample from the stats file only (never print
the VCF): number of SNPs, indels and the ts/tv ratio. Log the numbers in PROGRESS.md.
```

## Section 6 · Validation

**Prompt #12: variants in the QRDRs**
```
Next step: look up the variants in the quinolone-resistance-determining regions (QRDRs).
Read PROGRESS.md first.

- Take the gyrA and parC gene coordinates and strands from the reference GFF.
- QRDRs in standard E. coli codon numbering: gyrA codons 67–106, parC codons 56–108.
  Convert them to genome coordinates (mind the strand) and write data/ref/qrdr.bed.
- For each sample, intersect its VCF with qrdr.bed (bcftools or bedtools) and, for every hit,
  work out the codon number and the amino-acid change (e.g. S83L) from the reference sequence.
- Write scripts/qrdr.sh (and a small Python helper if needed), run it as a PBS batch job, and
  write results/<sample>/qrdr_hits.tsv with columns:
  gene, codon, ref_aa, alt_aa, change, pos, ref, alt, GT, DP, alt_AF

Report a table per sample of the QRDR hits and their depth and allele frequency.
Do not decide resistant/susceptible yet; that is the next step.
Log in PROGRESS.md; note any assumptions (coordinates, numbering) in NOTES.md.
```

## Section 7 · Decisions

**Prompt #13: write the decision rule.** Start a **fresh session**, then `/agents` → **rule-writer**. It
cannot see the data, on purpose.
```
Help me write DECISION.md for this project: is isolate Ec510423 fluoroquinolone-resistant,
based on gyrA/parC QRDR mutations? Research first, then ask me questions one at a time.
```

## Section 8 · Reproducibility

**Prompt #14: figures and report.** Switch back to **core/openagent**.
```
Final step: figures and the report. Read PROGRESS.md and DECISION.md first.

1. Copy the numeric thresholds from DECISION.md (min depth, min allele fraction, key codons)
   into results/decision_params.json, citing the DECISION.md line each came from.
   Do not change DECISION.md.

2. Write scripts/render.py (matplotlib) that reads ONLY pipeline outputs and decision_params.json
   (no numbers typed by hand) and writes to results/figures/:
   a. coverage.png: genome-wide depth per sample (mosdepth 10 kb bins), with the mean, the
      ~30× target and the gyrA/parC positions marked. Supports: "the genome was covered as planned".
   b. qrdr_map.png: one panel per sample × gene (gyrA, parC), x = codon number across the QRDR,
      bars = minimum depth per codon, a line at the min-depth threshold, the key codons highlighted,
      and each QRDR variant drawn as a lollipop labelled with its change and allele fraction.
      Supports: "the key codons were covered, and this is what was found there".
   c. evidence.png: every QRDR variant as a point, depth vs allele fraction, with the
      threshold lines from DECISION.md, so you can see which side of the rule each call falls on.
   Get per-base depth over the QRDRs inside the job (samtools depth -a -b data/ref/qrdr.bed);
   never read genome-wide per-base depth.
   Save the numbers behind each figure as a TSV next to it.

3. Run it as a PBS batch job, then download the PNGs to ./figures/ in this local project.

4. Apply DECISION.md and write results/report.md in exactly the format it specifies. Embed the
   figures, and give each a one-line caption naming the DECISION.md criterion it supports.
   If a figure contradicts the decision (e.g. a key codon below the depth threshold),
   say so in the report; do not fix it.
   Update PROGRESS.md.
```

**Prompt #15: independent review.** Start a **fresh session** and pick a **different model** with `/model`.
```
Review results/report.md against the pipeline outputs. Do not trust the report.
For each number and claim in it (decision, every row of the evidence table, every figure
caption), find the source file (VCF, qrdr_hits.tsv, the figure TSVs, mosdepth summary)
and check it matches. Check the decision follows from DECISION.md applied to those numbers.
List every discrepancy with file and value. Do not edit anything.
```

**Prompt #16: from scripts to a pipeline**
```
Write scripts/run_all.sh that reruns the whole pipeline from data/ to results/report.md
as chained PBS jobs (afterok dependencies), using only the scripts in scripts/.
Do not run it; show me the job chain and which outputs each job produces.
```
Or ask for a Snakemake workflow instead. Then rerun it **yourself**, without the agent: on the
frontend, `cd` into your `ecoli_fq` and run `bash scripts/run_all.sh`.
