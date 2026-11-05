"""How the fast path's INT4 kernel (int4_kernels.py) cuts one fused product into programs: the tiles of the INT4 base
product for a pass of a given number of tokens. Kept apart from int4_kernels.py, which needs Triton and a GPU to
import, so the choice can be checked anywhere.

The base product y = x W^T, W = bf16(q * s) of a fused group (fast_path.Lin) in the kernel layout
(int4.KernelInt4: `columns` outputs, a multiple of 64), runs in programs of `bm` tokens by `bn` output columns, the
input dimension cut into `split` equal ranges of whole 64-input groups. Each range is summed in float32 in input
order; with split > 1 each range writes its own float32 partial product and the epilogue adds them in split order, so
a given shape always gives the same bits (no atomic additions).

The tiles are jeff-asic's (benchmark/gpu_int4_fused.py TUNED, found by its w4_tune.py on the RTX PRO 6000) for the
batch-1 passes of 32, 64 and 128 tokens and the fused shapes of the Qwen3.5-0.8B layout; every other shape takes
generic tiles that keep about 376 programs. Re-tuning them for the deterministic kernel is open (plan 2b-2 Task 4
report)."""

from __future__ import annotations

from dataclasses import dataclass

GROUP = 64  # inputs per scale, and per block of the input dimension the kernel sums at a time
PROGRAM_TARGET = 376  # the generic tiles' split keeps programs * split at most this (jeff-asic's choice)
WARPS = 4  # every INT4 launch: the LoRA first factor shares it and must run as fast_path.lora_add runs it

# (columns, inputs, tokens) -> (bm, bn, split, warps, stages): jeff-asic's tuning on the RTX PRO 6000
TUNED: dict[tuple[int, int, int], tuple[int, int, int, int, int]] = {
    (8256, 1024, 32): (32, 64, 4, 4, 3), (8256, 1024, 64): (64, 64, 4, 4, 3), (8256, 1024, 128): (128, 64, 2, 4, 3),
    (1024, 2048, 32): (32, 32, 16, 4, 2), (1024, 2048, 64): (64, 32, 16, 4, 2), (1024, 2048, 128): (64, 64, 8, 4, 2),
    (5120, 1024, 32): (32, 64, 4, 4, 3), (5120, 1024, 64): (64, 64, 4, 4, 2), (5120, 1024, 128): (128, 64, 4, 4, 3),
    (7168, 1024, 32): (32, 64, 4, 4, 2), (7168, 1024, 64): (64, 64, 4, 4, 3), (7168, 1024, 128): (64, 64, 2, 4, 3),
    (1024, 3584, 32): (32, 64, 28, 4, 2), (1024, 3584, 64): (64, 32, 14, 4, 3), (1024, 3584, 128): (64, 64, 14, 4, 3)}


@dataclass(frozen=True)
class Int4Tiles:
    """One INT4 base product's programs: `bm` tokens by `bn` output columns, the input dimension in `split` ranges of
    whole 64-input groups; `warps` and `stages` are the launch's."""
    bm: int
    bn: int
    split: int
    warps: int
    stages: int

    def programs(self, columns: int, tokens: int) -> int:
        """The base product's programs (the LoRA first factor's come on top, in the same launch)."""
        return (columns // self.bn) * self.split * -(-tokens // self.bm)


def _split(groups: int, programs: int) -> int:
    """The largest split of `groups` 64-input groups (a divisor) keeping programs * split at most PROGRAM_TARGET."""
    best = 1
    for s in range(1, groups + 1):
        if groups % s == 0 and programs * s <= PROGRAM_TARGET:
            best = s
    return best


def int4_tiles(columns: int, inputs: int, tokens: int) -> Int4Tiles:
    """The tiles of the INT4 base product of `tokens` tokens through a fused group of `inputs` inputs and `columns`
    kernel columns (int4.KernelInt4: outputs padded to 64). ValueError for a shape the kernel cannot run."""
    if columns <= 0 or columns % 64 or inputs <= 0 or inputs % GROUP or tokens <= 0:
        raise ValueError(f"The INT4 kernel needs columns and inputs that are positive multiples of 64 and at least one "
                         f"token; got {columns} columns, {inputs} inputs, {tokens} tokens")
    if (columns, inputs, tokens) in TUNED:
        tiles = Int4Tiles(*TUNED[(columns, inputs, tokens)])
    else:
        bm = 16 if tokens <= 16 else 32 if tokens <= 32 else 64
        bn = 64
        tiles = Int4Tiles(bm, bn, _split(inputs // GROUP, (columns // bn) * -(-tokens // bm)), WARPS, 3)
    if tiles.warps != WARPS or columns % tiles.bn or (inputs // GROUP) % tiles.split:
        raise ValueError(f"INT4 tiles {tiles} do not fit {columns} columns and {inputs} inputs with {WARPS} warps")
    return tiles
