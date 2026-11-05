set -x
until grep -q CHAIN-E0-DONE logs/chain_e0.log; do sleep 10; done
A=$HOME/jeff-2b-scratch/venv-asic/bin/python
D=$HOME/jeff-dev-next/.venv/bin/python
bash run_asic.sh $A asicenv-t213-vllmfla gpu_int4_floor.py
bash run_asic.sh $A asicenv-t213-fla052 gpu_int4_floor_pkgfla.py
bash run_fp.sh $A asicenv-t213-fla052
bash run_fp.sh $A asicenv-t213-vllmfla vllmfla_wrap.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$HOME/jeff-2b-scratch/gap/asic $D p_kernels.py out/kernels_dev.json
echo CHAIN-E1-DONE
