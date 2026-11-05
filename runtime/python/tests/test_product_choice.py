"""The per-graph product kernel choice (engine/product_choice.py, plan 2b-2 Task 5), checked without a GPU: a bfloat16
base has cuBLAS only and nothing is timed (its graphs run what they ran before Task 5); an INT4 base times both
kernels once per graph and records the faster with both timings, cuBLAS on a tie; broken or impossible requests are
refused, naming what is wrong.

BatchedPath (which needs Triton to import) calls these with its fused groups and a timer that records each kernel's
products as a CUDA graph; its source is checked here for how the recorded passes call the fused groups, and
test_batched_path_cuda.py and test_int4_fast_path_cuda.py check it on the GPU."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jeff_runtime.engine.product_choice import (CUBLAS, INT4, PAIRS, PRODUCTS, ProductChoice, choose_product,
                                                product_candidates)

GROUPS = ("layers.0.mlp.gate_proj, layers.0.mlp.up_proj", "layers.0.mlp.down_proj", "layers.0.linear_attn.out_proj")


def never_timed(kernel: str) -> float:
    raise AssertionError(f"{kernel} was timed, but there is nothing to choose between")


def test_a_bfloat16_base_has_cublas_only_and_times_nothing() -> None:
    candidates = product_candidates(dict.fromkeys(GROUPS, False))
    assert candidates == (CUBLAS,)
    assert choose_product(candidates, never_timed) == ProductChoice(CUBLAS, {})


def test_an_int4_base_times_both_kernels_in_alternating_pairs_and_records_the_faster_by_median() -> None:
    candidates = product_candidates(dict.fromkeys(GROUPS, True))
    assert candidates == PRODUCTS == (CUBLAS, INT4)
    assert PAIRS == 3
    # each kernel's timings in the order they are taken; the medians decide (cuBLAS 2.4 / 1.4, INT4 1.7 / 2.2)
    cases = (({CUBLAS: [2.4, 9.0, 2.3], INT4: [1.7, 1.6, 8.0]}, {CUBLAS: 2.4, INT4: 1.7}, INT4),
             ({CUBLAS: [1.4, 1.3, 1.5], INT4: [2.2, 0.5, 2.3]}, {CUBLAS: 1.4, INT4: 2.2}, CUBLAS))
    for times, medians, winner in cases:
        calls: list[str] = []
        left = {kernel: list(values) for kernel, values in times.items()}

        def timer(kernel: str, left: dict[str, list[float]] = left) -> float:
            calls.append(kernel)
            return left[kernel].pop(0)

        assert choose_product(candidates, timer) == ProductChoice(winner, medians)
        assert calls == [CUBLAS, INT4] * PAIRS  # alternating, so drift falls on both alike


def test_a_tie_records_cublas() -> None:
    assert choose_product(PRODUCTS, lambda kernel: 1.0) == ProductChoice(CUBLAS, {CUBLAS: 1.0, INT4: 1.0})
    assert choose_product(PRODUCTS, lambda kernel: 1.0, pairs=1) == ProductChoice(CUBLAS, {CUBLAS: 1.0, INT4: 1.0})


def test_a_requested_kernel_is_recorded_without_timing() -> None:
    int4_base = dict.fromkeys(GROUPS, True)
    assert product_candidates(int4_base, [INT4]) == (INT4,)
    assert product_candidates(int4_base, [CUBLAS]) == (CUBLAS,)
    assert product_candidates(int4_base, [INT4, CUBLAS]) == PRODUCTS  # always in PRODUCTS order (the tie rule)
    assert choose_product((INT4,), never_timed) == ProductChoice(INT4, {})
    assert product_candidates(dict.fromkeys(GROUPS, False), [CUBLAS]) == (CUBLAS,)


def test_impossible_requests_and_broken_fast_paths_are_refused() -> None:
    with pytest.raises(ValueError, match=r"\['int4'\] need an INT4 base"):
        product_candidates(dict.fromkeys(GROUPS, False), [INT4])
    with pytest.raises(ValueError, match="Unknown product kernel"):
        product_candidates(dict.fromkeys(GROUPS, True), ["atomic"])
    with pytest.raises(ValueError, match="once, at least one"):
        product_candidates(dict.fromkeys(GROUPS, True), [])
    with pytest.raises(ValueError, match="once, at least one"):
        product_candidates(dict.fromkeys(GROUPS, True), [INT4, INT4])
    with pytest.raises(ValueError, match=r"1 of 3 fused groups hold no INT4 weights \(first: layers.0.mlp.down_proj\)"):
        product_candidates({GROUPS[0]: True, GROUPS[1]: False, GROUPS[2]: True})
    with pytest.raises(ValueError, match="no fused groups"):
        product_candidates({})
    with pytest.raises(ValueError, match="No product kernel"):
        choose_product((), never_timed)
    with pytest.raises(ValueError, match="at least once"):
        choose_product(PRODUCTS, never_timed, pairs=0)


def test_every_fused_product_of_the_recorded_passes_goes_through_the_graphs_choice() -> None:
    """In batched_path.py, the decision pass (body) and the prefix pass (prefix_body) compute each of a layer's four
    fused products (proj, out, mlp_in, mlp_down) through the product kernel chosen for the graph, never by calling a
    fused group directly; and the cuBLAS product is the group's own call, lin(x, ws, adapters), exactly what both
    passes called before plan 2b-2 Task 5 (so a bfloat16 base's graphs are unchanged)."""
    source = Path(__file__).resolve().parents[1] / "src/jeff_runtime/engine/batched_path.py"
    module = ast.parse(source.read_text())

    def functions(node: ast.Module | ast.ClassDef) -> dict[str, ast.FunctionDef]:
        return {item.name: item for item in node.body if isinstance(item, ast.FunctionDef)}

    classes = {item.name: item for item in module.body if isinstance(item, ast.ClassDef)}
    methods = functions(classes["BatchedPath"])
    for name in ("body", "prefix_body"):
        calls = [node for node in ast.walk(methods[name]) if isinstance(node, ast.Call)]
        direct = [ast.unparse(call) for call in calls
                  if isinstance(call.func, ast.Subscript) and ast.unparse(call.func.value) == "layer"]
        assert not direct, (name, direct)
        through = sorted(ast.unparse(call.args[0]) for call in calls
                         if isinstance(call.func, ast.Name) and call.func.id == "product")
        assert through == ["layer['mlp_down']", "layer['mlp_in']", "layer['out']", "layer['proj']"], (name, through)
    returned = [node.value for node in ast.walk(functions(module)["_cublas_product"]) if isinstance(node, ast.Return)]
    assert [ast.unparse(value) for value in returned if value is not None] == ["lin(x, ws, adapters)"]
