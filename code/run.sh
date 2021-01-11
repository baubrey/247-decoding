#!/bin/bash
#SBATCH --time=01:30:00
#SBATCH --mem=16GB
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=1
#SBATCH -o 'logs/%A_%a.log'

set -e

# The first argument to this script is the number of times to run the program
# specified by the rest of the arguments
# e.g. bash run.sh 5 '--version' will run `python --version` 5 times.

if [[ "$HOSTNAME" == *"tiger"* ]]
then
    module load anaconda
    conda activate 247-podcast-tf2
else
    module load anacondapy
    source activate 247-main
fi

echo 'Requester:' $USER
echo 'Node:' $HOSTNAME
echo "$@"
for run in $(seq 1 $1); do
    echo 'Run start time:' `date`
    python "${@:2}"
    echo 'Run end time:' `date`
done

echo 'Ensemble start:' `date`
python "${@:2}" --ensemble
echo 'End time:' `date`
