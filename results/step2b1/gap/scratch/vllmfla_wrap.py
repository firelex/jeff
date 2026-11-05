"""Runs a script with flash-linear-attention chunk_gated_delta_rule replaced by vLLM 0.30.0 copy of it (the kernel
library jeff-asic measured with). Usage: python vllmfla_wrap.py SCRIPT ARGS..."""
import runpy
import sys

import fla.ops.gated_delta_rule as pkg
from vllm.third_party.flash_linear_attention.ops.chunk import chunk_gated_delta_rule as vllm_chunk

pkg.chunk_gated_delta_rule = vllm_chunk
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
