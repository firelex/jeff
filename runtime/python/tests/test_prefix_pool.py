"""The prefix pool's bookkeeping (uniform pages, block tables, least-recently-used eviction, pinning) and page layout
on CPU tensors, with tiny state shapes. Runs on the Mac; the fast path's own use of the pool is tested on CUDA
(test_fast_path_cuda.py)."""

import pytest
import torch

from jeff.errors import EngineError
from jeff_runtime.engine.prefix_pool import (FAST_PATH_SHAPES, PrefixPool, PrefixShapes, check_pool_holds,
                                             check_pool_size, pool_pages, prefix_pool_mb_for, prefixes_held)

# 256-byte pages: 4 state pages (4 layers x 4 heads of 4 x 4 float32, 4 heads a page), 2 convolution pages (2 layers
# a page), so every prefix takes 6 pages plus one per 32 tokens.
TINY = PrefixShapes(gdn_layers=4, conv=(3, 16), state=(4, 4, 4), attn_layers=1, kv=(1, 2))
F = 6


def pool(pages: int) -> PrefixPool:
    return PrefixPool(pages, "cpu", shapes=TINY)


def test_the_tiny_layout() -> None:
    assert (TINY.page_bytes, TINY.state_pages, TINY.heads_per_page, TINY.conv_pages, TINY.fixed_pages) == \
        (256, 4, 4, 2, F)


def test_the_fast_path_layout_matches_the_design() -> None:
    """Section 4 of the design as amended: 0.375 MB pages, 48 state pages of 6 heads, 2 convolution pages of 9
    layers, so a prefix takes 50 pages plus one per 32 tokens; 8 GB holds 21,845 pages, 266 prefixes of 1,000
    tokens, and 64 prefixes of 8,192 tokens need 19,584 pages."""
    shapes = FAST_PATH_SHAPES
    assert shapes.page_bytes == 393216
    assert (shapes.state_pages, shapes.heads_per_page, shapes.conv_pages, shapes.layers_per_conv_page) == (48, 6, 2, 9)
    assert shapes.pages_per_prefix(1000) == 82 and shapes.pages_per_prefix(8192) == 306
    assert pool_pages(8192) == 21845 and prefixes_held(8192, 1000) == 266
    assert 64 * shapes.pages_per_prefix(8192) == 19584


@pytest.mark.parametrize(("tokens", "pages"), [(0, 0), (1, 1), (31, 1), (32, 1), (33, 2), (2000, 63)])
def test_page_count_matches_the_token_count(tokens: int, pages: int) -> None:
    assert TINY.pages_per_prefix(tokens) == F + pages
    assert FAST_PATH_SHAPES.pages_per_prefix(tokens) == 50 + pages
    made = pool(2 + F + 64)
    slot = made.reserve(1, "p", tokens)
    assert len(made.block_table(slot)) == F + pages
    assert made.length(slot) == tokens


def test_slot_zero_is_the_empty_prefix_and_pages_zero_and_one_are_never_handed_out() -> None:
    made = pool(2 + 2 * (F + 2))
    assert made.length(0) == 0 and made.block_table(0) == [1] * F
    slots = {made.reserve(1, "a", 64), made.reserve(1, "b", 64)}
    assert slots == {1, 2}
    pages = made.block_table(1) + made.block_table(2)
    assert sorted(pages) == list(range(2, 2 + 2 * (F + 2)))
    conv, state, kv = made.gather(0)
    assert not conv.any() and not state.any() and kv.shape[2] == 0


def test_lookup_finds_a_reserved_prefix_by_adapter_index_and_text() -> None:
    made = pool(40)
    slot = made.reserve(2, "text", 40)
    assert made.lookup(2, "text") == slot
    assert made.lookup(1, "text") is None
    assert made.lookup(2, "other") is None
    with pytest.raises(ValueError, match="already"):
        made.reserve(2, "text", 40)


def test_key_names_the_adapter_index_and_text_a_slot_holds() -> None:
    """A batched pass checks each row's slot against the row's own (adapter, prefix text) with key()."""
    made = pool(40)
    slot = made.reserve(2, "text", 40)
    assert made.key(slot) == (2, "text")
    with pytest.raises(ValueError, match="holds no prefix"):
        made.key(0)
    made.release(slot)
    with pytest.raises(ValueError, match="holds no prefix"):
        made.key(slot)


def test_eviction_takes_the_least_recently_used_first() -> None:
    made = pool(2 + 3 * (F + 1))  # room for three prefixes of up to 32 tokens
    a = made.reserve(1, "a", 10)
    b = made.reserve(1, "b", 10)
    c = made.reserve(1, "c", 10)
    assert made.lookup(1, "a") == a  # a is now the most recently used: b is the oldest
    d = made.reserve(1, "d", 10)
    assert d == b
    assert made.lookup(1, "b") is None
    assert [made.lookup(1, key) for key in "acd"] == [a, c, d]  # order now: a, c, d (oldest first)
    e = made.reserve(1, "e", 10)
    assert e == a and made.lookup(1, "a") is None


def test_eviction_frees_pages_until_a_long_prefix_fits() -> None:
    made = pool(2 + 4 * (F + 2))  # four prefixes of 64 tokens fill it
    for key in "abcd":
        made.reserve(1, key, 64)
    made.lookup(1, "a")  # order: b, c, d, a
    slot = made.reserve(1, "long", 4 * 32)  # needs F + 4 pages: b and c go, d and a stay
    assert len(made.block_table(slot)) == F + 4
    assert made.lookup(1, "b") is None and made.lookup(1, "c") is None
    assert made.lookup(1, "d") is not None and made.lookup(1, "a") is not None


def test_a_pinned_slot_is_never_evicted() -> None:
    made = pool(2 + 3 * (F + 1))
    a = made.reserve(1, "a", 10)
    b = made.reserve(1, "b", 10)
    made.reserve(1, "c", 10)
    made.pin([a, b])  # a and b are the two oldest, but pinned: c goes
    made.reserve(1, "d", 10)
    assert made.lookup(1, "c") is None
    assert made.lookup(1, "a") == a and made.lookup(1, "b") == b
    made.unpin([a, b])
    made.lookup(1, "d")  # order: a, b, d
    made.reserve(1, "e", 10)
    assert made.lookup(1, "a") is None


def test_pins_are_counted() -> None:
    made = pool(2 + 2 * (F + 1))
    a = made.reserve(1, "a", 10)
    made.reserve(1, "b", 10)
    made.pin([a, a])  # two rows of one pass share the prefix
    made.unpin([a])
    made.lookup(1, "b")
    made.reserve(1, "c", 10)  # a is still pinned once: b goes
    assert made.lookup(1, "a") == a and made.lookup(1, "b") is None
    made.unpin([a])
    with pytest.raises(ValueError, match="not pinned"):
        made.unpin([a])


def test_everything_pinned_is_an_engine_error_and_evicts_nothing() -> None:
    made = pool(2 + 2 * (F + 1))
    a = made.reserve(1, "a", 10)
    b = made.reserve(1, "b", 10)
    made.pin([a, b])
    with pytest.raises(EngineError, match="pinned"):
        made.reserve(1, "c", 10)
    assert made.lookup(1, "a") == a and made.lookup(1, "b") == b


def test_pages_held_by_pinned_slots_are_an_engine_error_too() -> None:
    made = pool(2 + 2 * (F + 2) + 4)  # two prefixes of 64 tokens and four free pages
    a = made.reserve(1, "a", 64)
    made.reserve(1, "b", 64)
    made.pin([a])
    with pytest.raises(EngineError, match="pinned"):
        made.reserve(1, "long", 7 * 32)  # needs F + 7 = 13: b's 8 and the 4 free make 12, a's are pinned
    assert made.lookup(1, "b") is not None  # nothing was evicted for a reservation that could not succeed


def test_a_prefix_longer_than_every_page_is_an_engine_error() -> None:
    made = pool(2 + F + 2)
    with pytest.raises(EngineError, match="--prefix-pool-mb"):
        made.reserve(1, "long", 3 * 32)


def test_pin_refuses_a_slot_not_in_use() -> None:
    made = pool(40)
    with pytest.raises(ValueError, match="slot 2"):
        made.pin([2])


def test_release_forgets_a_prefix_whose_pass_failed() -> None:
    """BatchedPath.ensure_prefixes releases each slot it reserved but did not write when a prefix pass raises, so the
    key never maps to an unwritten state."""
    made = pool(40)
    free = made.free_page_count()
    slot = made.reserve(1, "a", 40)
    made.release(slot)
    assert made.lookup(1, "a") is None and len(made) == 0 and made.free_page_count() == free
    with pytest.raises(ValueError, match="holds no prefix"):
        made.release(slot)
    pinned = made.reserve(1, "b", 40)
    made.pin([pinned])
    with pytest.raises(ValueError, match="pinned"):
        made.release(pinned)


def test_forget_adapter_frees_exactly_that_adapters_slots_and_pages() -> None:
    made = pool(80)
    kept = {key: made.reserve(1, key, 40) for key in ("a", "b")}
    gone = {key: made.reserve(2, key, 70) for key in ("a", "c")}
    assert len(gone) == 2
    kept_pages = {slot: made.block_table(slot) for slot in kept.values()}
    free_slots, free_pages = made.free_slot_count(), made.free_page_count()
    made.forget_adapter(2)
    assert made.lookup(2, "a") is None and made.lookup(2, "c") is None
    assert {key: made.lookup(1, key) for key in kept} == kept
    assert {slot: made.block_table(slot) for slot in kept.values()} == kept_pages
    assert made.free_slot_count() == free_slots + 2
    assert made.free_page_count() == free_pages + 2 * (F + 3)
    made.forget_adapter(3)  # no prefixes: nothing changes
    assert made.free_slot_count() == free_slots + 2


def test_forget_adapter_refuses_a_pinned_slot() -> None:
    made = pool(40)
    made.pin([made.reserve(1, "a", 10)])
    with pytest.raises(ValueError, match="pinned"):
        made.forget_adapter(1)


def test_the_block_table_is_on_the_device_too() -> None:
    made = pool(60)
    made.reserve(1, "a", 64)
    slot = made.reserve(1, "b", 100)
    assert made.tables.shape == (made.slots, F + 256)
    assert made.tables[slot, :F + 4].tolist() == made.block_table(slot)
    assert made.tables[0, :F].tolist() == [1] * F
    with pytest.raises(ValueError, match="longest prefix"):
        made.reserve(1, "c", 8193)


def test_a_prefix_written_through_its_block_table_reads_back_exactly() -> None:
    """write() as the prefix pass calls it: the block table padded with page 0 for the bucket's padding pages, keys
    and values for the whole bucket (here 4 pages for a 70-token prefix, which owns 3). gather() gives back the same
    state and the prefix's own 70 tokens of keys and values; the zero page stays zero, and the padding page went to
    page 0."""
    made = pool(40)
    made.reserve(1, "other", 10)
    slot = made.reserve(1, "p", 70)
    generator = torch.Generator().manual_seed(0)
    conv = torch.randn(4, 3, 16, generator=generator).to(torch.bfloat16)
    state = torch.randn(4, 4, 4, 4, generator=generator)
    kv = torch.randn(1, 2, 4 * 32, 1, 2, generator=generator).to(torch.bfloat16)
    pages = torch.zeros(F + 256, dtype=torch.long)
    pages[:F + 3] = made.tables[slot, :F + 3]
    made.write(pages, conv, state, kv)
    got_conv, got_state, got_kv = made.gather(slot)
    assert torch.equal(got_conv, conv) and torch.equal(got_state, state) and torch.equal(got_kv, kv[:, :, :70])
    assert not made.data[1].any()  # the zero page
    assert torch.equal(made.kv[0], kv[:, :, 96:128])  # the padding page went to page 0
    assert not made.gather(made.lookup(1, "other"))[1].any()  # another prefix's pages are untouched


def test_pool_pages_and_the_smallest_pools() -> None:
    assert pool_pages(8192) == 8192 * 2**20 // 393216
    assert prefix_pool_mb_for(1) == 116  # (306 + 2) pages of 0.375 MB, rounded up
    assert prefix_pool_mb_for(64) == 7345
    assert pool_pages(prefix_pool_mb_for(64)) - 2 >= 64 * 306
    assert pool_pages(prefix_pool_mb_for(64) - 1) - 2 < 64 * 306


def test_the_default_size_of_the_deploy_examples_holds_64_rows_of_the_longest_prefix() -> None:
    check_pool_holds(8192, 64)
    check_pool_holds(prefix_pool_mb_for(64), 64)


def test_a_pool_too_small_for_the_batch_is_refused_naming_both_settings() -> None:
    with pytest.raises(EngineError) as error:
        check_pool_holds(4096, 64)
    message = str(error.value)
    assert "--prefix-pool-mb 4096" in message and "--max-batch-rows 64" in message and "at least 7345" in message


@pytest.mark.parametrize("bad", [None, 115, 0, -3, 1.5, "2048", True])
def test_the_pool_size_is_whole_megabytes_from_the_smallest_pool_up(bad: object) -> None:
    with pytest.raises(EngineError, match="--prefix-pool-mb") as error:
        check_pool_size(bad)
    assert "at least 116" in str(error.value) and "2048 holds 66 prefixes of 1,000 tokens" in str(error.value)


def test_the_smallest_pool_is_accepted() -> None:
    assert check_pool_size(116) == 116
