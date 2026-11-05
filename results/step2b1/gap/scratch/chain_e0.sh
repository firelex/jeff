set -x
D=$HOME/jeff-dev-next/.venv/bin/python
T=$HOME/jeff-2b-scratch/venv-t213/bin/python
bash run_asic.sh $D dev-t214-fla052 gpu_int4_floor_pkgfla.py
bash run_fp.sh $D dev-t214-fla052
bash run_asic.sh $T t213-fla052 gpu_int4_floor_pkgfla.py
bash run_fp.sh $T t213-fla052
echo CHAIN-E0-DONE
