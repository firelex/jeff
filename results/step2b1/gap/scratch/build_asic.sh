set -euxo pipefail
U=$HOME/.local/bin/uv
V=$HOME/jeff-2b-scratch/venv-asic
[ -x $V/bin/python ] || $U venv --python 3.12 $V
$U pip install --python $V/bin/python -r $HOME/jeff-2b-scratch/gap/qwen-rtx-freeze.txt
$U pip install --python $V/bin/python -c $HOME/jeff-2b-scratch/gap/qwen-rtx-freeze.txt flash-linear-attention==0.5.2 fla-core==0.5.2 peft==0.21.1 ruamel.yaml msgpack
SP=$($V/bin/python -c "import site; print(site.getsitepackages()[0])")
printf "%s\n%s\n" $HOME/jeff-dev-next/packages/python/src $HOME/jeff-dev-next/runtime/python/src > $SP/jeff_dev_next_src.pth
$U pip list --python $V/bin/python | grep -iE "^(torch|triton|vllm|flash|fla|nvidia-cublas|transformers|peft)"
$V/bin/python -c "import jeff_runtime.engine.fast_path, vllm.third_party.flash_linear_attention.ops.chunk; print(\"IMPORTS-OK\")"
echo BUILD-DONE
