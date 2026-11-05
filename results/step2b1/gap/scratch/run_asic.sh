# usage: run_asic.sh PYTHON LABEL SCRIPT  (SCRIPT: gpu_int4_floor.py = vLLM fla, gpu_int4_floor_pkgfla.py = fla package)
set -euo pipefail
PY=$1; LABEL=$2; SCRIPT=$3
OUT=$HOME/jeff-2b-scratch/gap/out; mkdir -p $OUT
cd $HOME/jeff-2b-scratch/gap/asic
/usr/lib/wsl/lib/nvidia-smi --query-gpu=timestamp,utilization.gpu,clocks.sm,power.draw,pstate,memory.used --format=csv,noheader,nounits -lms 200 > $OUT/asic_$LABEL.smi.csv &
SMI=$!
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$HOME/jeff-2b-scratch/gap/asic $PY $SCRIPT time --weights bf16 --fused \
  --base /mnt/d/jeff-models/v1.3/v13base/ll-v12-final --adapter nav \
  --adapter-dir /mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-nav-20261003-0149/final \
  --reference $HOME/jeff-asic/int4floor/reference.pt --reps 1000 --warmup 100 --output $OUT/asic_$LABEL.json
kill $SMI
echo ASIC-DONE $LABEL
