#!/bin/bash
# Process one receiver-day: sbatch -p rodrigues -c 2 --mem=40G code/cluster/process_one.sbatch US-PR1 2024-10-10
#SBATCH --output=logs/%x_%j.out
#SBATCH --time=08:00:00
set -euo pipefail
cd /titan/frodrigues/scint_labeler
set +u; source /etc/profile.d/lmod.sh; module load rxtools/25.0.0; set -u   # bin2asc for ScintPi 4 (Apptainer)
which apptainer
export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1} OPENBLAS_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1}
echo "host $(hostname) code $(git -C code rev-parse --short HEAD)"
env/bin/scintlab process --receiver "$1" --date "$2" ${3:-}
