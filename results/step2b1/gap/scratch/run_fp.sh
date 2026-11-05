# usage: run_fp.sh PYTHON LABEL [extra python -c preamble module]
set -euo pipefail
PY=$1; LABEL=$2; WRAP=${3:-}
OUT=$HOME/jeff-2b-scratch/gap/out; mkdir -p $OUT
ARGS=()
for n in emotion ground guard legal-clauses nav spam support-intents tools triage; do
  d=$(ls -d /mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-$n-*/final); ARGS+=(--adapter "$n=$d")
done
for f in /mnt/d/jeff-adapters/apps/*/decision_config.json; do d=$(dirname $f); ARGS+=(--adapter "app-$(basename $d)=$d"); done
cd $HOME/jeff-dev-next
if [ -n "$WRAP" ]; then RUN=($PY $HOME/jeff-2b-scratch/gap/$WRAP scripts/bench_fast_path.py); else RUN=($PY scripts/bench_fast_path.py); fi
PYTHONDONTWRITEBYTECODE=1 "${RUN[@]}" --base /mnt/d/jeff-models/v1.3/v13base/ll-v12-final "${ARGS[@]}" --use nav \
  --text /mnt/d/jeff-models/v1.3/casdgx01/testsets/legal-clauses/test.jsonl --label "$LABEL" --output $OUT/fp_$LABEL.json
echo FP-DONE $LABEL
