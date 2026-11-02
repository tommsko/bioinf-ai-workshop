#!/usr/bin/env bash
# Align paired-end reads to a reference -> sorted, duplicate-marked, indexed BAM.
# Runs as a PBS job on MetaCentrum (uses $SCRATCHDIR and $NCPUS); reruns without any agent.
#
#   align.sh index <ref.fa>                              build bwa-mem2 + samtools indexes (once)
#   align.sh align <sample> <ref.fa> <R1> <R2> <outdir>  -> <outdir>/<sample>.bam(.bai)
set -euo pipefail

T=${NCPUS:-4}
mode=${1:?usage: align.sh index <ref.fa> | align.sh align <sample> <ref.fa> <R1> <R2> <outdir>}

if [ "$mode" = index ]; then
  REF=${2:?ref.fa}
  [ -s "$REF" ] || { echo "ERROR: reference $REF missing or empty" >&2; exit 2; }
  [ -s "$REF.fai" ] || samtools faidx "$REF"
  if [ -s "$REF.bwt.2bit.64" ]; then
    echo "index: bwa-mem2 index already present, nothing to do"
  else
    bwa-mem2 index "$REF"
  fi
  ls -l "$REF".*
  exit 0
fi

[ "$mode" = align ] || { echo "ERROR: unknown mode $mode" >&2; exit 2; }
S=${2:?sample}; REF=${3:?ref.fa}; R1=${4:?R1}; R2=${5:?R2}; OUT=${6:?outdir}
for f in "$REF" "$R1" "$R2"; do
  [ -s "$f" ] || { echo "ERROR: input $f missing or empty" >&2; exit 2; }
done
[ -s "$REF.bwt.2bit.64" ] || { echo "ERROR: no bwa-mem2 index for $REF; run 'align.sh index' first" >&2; exit 2; }
[ "$R1" != "$R2" ] || { echo "ERROR: R1 and R2 are the same file" >&2; exit 2; }

W=${SCRATCHDIR:?run as a PBS job with scratch}/$S
mkdir -p "$W" "$OUT"
echo "== $S: bwa-mem2 mem -t $T (paired-end) -> fixmate -> sort -> markdup"
bwa-mem2 mem -t "$T" -R "@RG\tID:$S\tSM:$S\tPL:ILLUMINA" "$REF" "$R1" "$R2" 2> "$W/bwa.log" \
  | samtools fixmate -m -u - - \
  | samtools sort -@ "$T" -u -T "$W/sort" - \
  | samtools markdup -@ "$T" -s - "$W/$S.bam" 2> "$W/markdup.txt"
samtools index "$W/$S.bam"
samtools quickcheck "$W/$S.bam"

cp "$W/$S.bam" "$W/$S.bam.bai" "$W/bwa.log" "$W/markdup.txt" "$OUT/"
echo "== done: $OUT/$S.bam"
ls -l "$OUT/$S.bam" "$OUT/$S.bam.bai"
