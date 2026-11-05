set -euxo pipefail
U=$HOME/.local/bin/uv
V=$HOME/jeff-2b-scratch/venv-t213
$U venv --python 3.13 $V
$U pip install --python $V/bin/python -r $HOME/jeff-2b-scratch/gap/reqs-t213.txt
$U pip install --python $V/bin/python --no-deps -e $HOME/jeff-dev-next/packages/python -e $HOME/jeff-dev-next/runtime/python
$U pip list --python $V/bin/python | grep -iE "^(torch|triton|flash|fla|nvidia-cublas|transformers|jeff)"
echo BUILD-DONE
