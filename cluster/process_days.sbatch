#!/bin/bash
# Process receiver-days listed in /titan/frodrigues/scint_labeler/days.csv, one day per array task.
# Usage (from /titan/frodrigues/scint_labeler):
#   N=$(($(wc -l < days.csv) - 1))
#   sbatch -p rodrigues -c 2 --mem=40G --array=0-$((N-1))%12 -J scintlab code/cluster/process_days.sbatch $N
# Re-running skips days whose meta.json exists. Logs go to logs/.
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --time=08:00:00
set -euo pipefail
cd /titan/frodrigues/scint_labeler
N_TASKS="${1:?number of days}"
export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1} OPENBLAS_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1}
echo "host $(hostname) task $SLURM_ARRAY_TASK_ID code $(git -C code rev-parse --short HEAD)"
env/bin/scintlab process --task-id "$SLURM_ARRAY_TASK_ID" --n-tasks "$N_TASKS"
