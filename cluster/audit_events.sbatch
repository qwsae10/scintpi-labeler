#!/bin/bash
# Rebuild the event index with events joined over UTC midnight, measure each event's 1 s data
# (coverage of L1, 2nd band, TEC; gaps; loss of lock at its edges) and install all of it, so the
# queue's data filters work. Labels on events merged over midnight follow them (events/id_map.csv).
# Usage (from /titan/frodrigues/scint_labeler):
#   sbatch -p rodrigues -c 8 --mem=40G -J audit code/cluster/audit_events.sbatch
# Add --no-install to only write the report. Output: events/audit_<time>/report.txt and the files
# beside it; installed: events/events.parquet (old one kept, dated), quality.parquet, id_map.csv.
# Running labelers restart scintlab serve afterwards to see the new index.
# Run (and write the log) in the project folder wherever sbatch is called from; Slurm opens the log
# before the script starts, so a missing or unwritable logs/ fails the job with exit code 0:53 and no log.
#SBATCH --chdir=/titan/frodrigues/scint_labeler
#SBATCH --output=logs/%x_%j.out
#SBATCH --time=04:00:00
set -euo pipefail
cd /titan/frodrigues/scint_labeler
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
echo "host $(hostname) code $(git -C code rev-parse --short HEAD)"
INSTALL=--install
for a in "$@"; do [ "$a" = "--no-install" ] && INSTALL=""; done
ARGS=(); for a in "$@"; do [ "$a" != "--no-install" ] && ARGS+=("$a"); done
env/bin/scintlab audit-events --workers "${SLURM_CPUS_PER_TASK:-1}" $INSTALL "${ARGS[@]+"${ARGS[@]}"}"
