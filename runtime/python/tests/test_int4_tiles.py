"""The INT4 kernel's tiles (engine/int4_tiles.py, plan 2b-2 Task 4), checked without a GPU: every fused group of the
Qwen3.5-0.8B layout and every pass the batched fast path records gets tiles the kernel can run (whole 64-input
groups per split, whole column blocks, the warps the LoRA first factor runs with), the batch-1 passes get jeff-asic's
tuned tiles, and shapes the kernel cannot run are refused.

The pass sizes are written out here because batched_path.py needs Triton to import; test_int4_kernels_cuda.py checks
the kernel itself on every pass of batched_path's own lists."""

from __future__ import annotations

import pytest

from jeff_runtime.engine.int4_tiles import GROUP, PROGRAM_TARGET, TUNED, WARPS, Int4Tiles, int4_tiles

# (kernel columns, inputs) of the fused groups: Gated DeltaNet projections (8,224 outputs padded to 8,256), attention
# projections, the MLP's gate and up, the two output projections (2,048 inputs), the MLP's down projection
FUSED = ((8256, 1024), (5120, 1024), (7168, 1024), (1024, 2048), (1024, 3584))
BATCH = (1, 2, 4, 8, 16, 32, 64)
NEW = (32, 64, 128, 256, 512, 1024)
DECISION_TOKENS = {b * n for b in BATCH for n in NEW if b * n <= 16384}
PREFIX_ROWS = {64: 16, 128: 32, 256: 16, 512: 8, 768: 4, 1024: 4, 1536: 4, 2048: 2, 3072: 2, 4096: 1, 6144: 1, 8192: 1}
PREFIX_TOKENS = {b * nb for nb, cap in PREFIX_ROWS.items() for b in BATCH if b <= cap}


@pytest.mark.parametrize("columns,inputs", FUSED)
def test_every_recorded_pass_gets_tiles_the_kernel_can_run(columns: int, inputs: int) -> None:
    for tokens in sorted(DECISION_TOKENS | PREFIX_TOKENS):
        t = int4_tiles(columns, inputs, tokens)
        assert t.warps == WARPS
        assert columns % t.bn == 0
        assert (inputs // GROUP) % t.split == 0
        assert t.bm in (16, 32, 64, 128) and t.bn in (32, 64)


def test_batch_one_passes_take_jeff_asics_tiles() -> None:
    for columns, inputs in FUSED:
        for tokens in (32, 64, 128):
            assert int4_tiles(columns, inputs, tokens) == Int4Tiles(*TUNED[(columns, inputs, tokens)])


def test_generic_tiles_keep_the_programs_near_the_target() -> None:
    """The largest split that keeps programs * split within PROGRAM_TARGET, or no split when even one range has more
    programs than that."""
    for columns, inputs in FUSED:
        for tokens in sorted(DECISION_TOKENS | PREFIX_TOKENS):
            if (columns, inputs, tokens) in TUNED:
                continue
            t = int4_tiles(columns, inputs, tokens)
            assert t.bm == (16 if tokens <= 16 else 32 if tokens <= 32 else 64)
            uncut = t.programs(columns, tokens) // t.split
            assert t.split == 1 or uncut * t.split <= PROGRAM_TARGET
            bigger = [s for s in range(t.split + 1, inputs // GROUP + 1) if (inputs // GROUP) % s == 0]
            assert not bigger or uncut * bigger[0] > PROGRAM_TARGET


def test_large_passes_are_not_cut() -> None:
    assert int4_tiles(8256, 1024, 16384).split == 1
    assert int4_tiles(1024, 3584, 4096).split == 1


def test_programs_counts_partial_token_tiles() -> None:
    assert Int4Tiles(32, 64, 4, 4, 3).programs(8256, 33) == 129 * 4 * 2


@pytest.mark.parametrize("columns,inputs,tokens", [(8224, 1024, 32), (8256, 1000, 32), (0, 1024, 32),
                                                   (8256, 1024, 0), (8256, -64, 32)])
def test_shapes_the_kernel_cannot_run_are_refused(columns: int, inputs: int, tokens: int) -> None:
    with pytest.raises(ValueError, match="positive multiples of 64"):
        int4_tiles(columns, inputs, tokens)
