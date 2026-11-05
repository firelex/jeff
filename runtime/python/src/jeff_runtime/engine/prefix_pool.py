"""The fast path's prefix pool (design 2b-1 Sections 2.2, 2.3 and 4): every cached prefix's state on the GPU, at
fixed addresses, in one pool of uniform pages, so decision graphs can read it in place through a block table.

- **Pages.** All of the pool is pages of one size: what 32 tokens of attention keys and values take (0.375 MB for
  Qwen3.5-0.8B: 6 layers x keys and values x 32 tokens x 2 heads x 256 x bfloat16). Three typed views of the same
  memory:
  - `kv[page, layer, 0 keys | 1 values, token, head, dim]` (bfloat16);
  - `state[page, head, key dim, value dim]` (float32): the Gated DeltaNet recurrent states, every layer's heads in
    order (layer 0 heads 0..15, layer 1 heads 0..15, ...), a whole number of heads per page (6), so no head
    straddles two pages: 48 pages for 18 layers of 16 heads;
  - `conv[page, layer, 3, channels]` (bfloat16): the last three convolution inputs of each Gated DeltaNet layer, a
    whole number of layers per page: 2 pages of 9 layers.
- **A prefix** of p tokens takes 50 + ceil(p / 32) pages, listed in one block table in this order: its 48 state
  pages, its 2 convolution pages, then its key/value pages in token order. The block tables are kept on the host
  (block_table) and on the device (`tables[slot]`, one row per slot).
- **Slots** number the prefixes (a slot is a row of `tables`). Slot 0 is the empty prefix, never handed out: its
  state and convolution entries all point at page 1, which is all zeros and never written, and it has no key/value
  pages. Padding rows of a batched pass point at it.
- **Page 0** is never handed out either: the prefix pass writes there whatever has no page of its own (the keys and
  values of its padding tokens past the prefix's last page), and nothing reads it.
- Prefixes are found by (adapter index, prefix text). The fast path forgets an adapter's prefixes whenever its row in
  the adapter pools is filled or freed (forget_adapter), so a reused row never sees an older adapter's prefixes.
- When a new prefix needs pages that are not free, the least recently used prefixes are evicted, never a pinned one.
  A batched pass pins the slots of the rows it is forming, so nothing it reads is freed under it.

The pool's size is the engine's --prefix-pool-mb, required on the fast backend (no default). The engine refuses at
start a pool that cannot hold --max-batch-rows prefixes of the longest prefix (8,192 tokens) at once
(check_pool_holds); prefix_pool_mb_for(rows) is the smallest size that holds `rows` of them, and
prefix_pool_mb_for(1) the smallest pool the engine takes at all.

Bookkeeping only runs between decisions, on the thread that runs them; nothing here is thread safe."""

from __future__ import annotations

import heapq
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

import torch

from jeff.errors import EngineError

PAGE_TOKENS = 32
# The longest prefix the fast path processes (its largest prefix bucket, fast_path.PREFIX_BUCKETS).
LONGEST_PREFIX_TOKENS = 8192
SINK_PAGE, ZERO_PAGE = 0, 1  # never handed out (see the module docstring)
RESERVED_PAGES = 2


def _cdiv(a: int, b: int) -> int:
    return -(-a // b)


@dataclass(frozen=True)
class PrefixShapes:
    """The shapes of one prefix's state: per Gated DeltaNet layer a (3, channels) convolution tail and a (heads, key
    dim, value dim) recurrent state; per attention layer and token, (key/value heads, head dim) keys and values."""
    gdn_layers: int
    conv: tuple[int, int]
    state: tuple[int, int, int]
    attn_layers: int
    kv: tuple[int, int]
    page_tokens: int = PAGE_TOKENS

    @property
    def page_bytes(self) -> int:
        return self.attn_layers * 2 * self.page_tokens * self.kv[0] * self.kv[1] * 2  # keys and values, bfloat16

    @property
    def state_pages(self) -> int:
        heads = self.gdn_layers * self.state[0]
        return _cdiv(heads, self.page_bytes // (self.state[1] * self.state[2] * 4))

    @property
    def heads_per_page(self) -> int:
        return self.gdn_layers * self.state[0] // self.state_pages

    @property
    def conv_pages(self) -> int:
        return _cdiv(self.gdn_layers, self.page_bytes // (self.conv[0] * self.conv[1] * 2))

    @property
    def layers_per_conv_page(self) -> int:
        return self.gdn_layers // self.conv_pages

    @property
    def fixed_pages(self) -> int:
        """The pages every prefix takes whatever its length: its Gated DeltaNet states and convolution tails."""
        return self.state_pages + self.conv_pages

    def pages_per_prefix(self, tokens: int) -> int:
        return self.fixed_pages + _cdiv(tokens, self.page_tokens)

    def check(self) -> None:
        heads = self.gdn_layers * self.state[0]
        if self.page_bytes % 4 or heads % self.state_pages or self.gdn_layers % self.conv_pages:
            raise ValueError(f"{self}: the states and convolution tails do not split evenly into pages")


# Qwen3.5-0.8B, the layout the fast path is built for (fast_path.FastPath checks the model has it).
FAST_PATH_SHAPES = PrefixShapes(gdn_layers=18, conv=(3, 6144), state=(16, 128, 128), attn_layers=6, kv=(2, 256))


def pool_pages(prefix_pool_mb: int, shapes: PrefixShapes = FAST_PATH_SHAPES) -> int:
    """The pages a pool of `prefix_pool_mb` MB holds, the two reserved pages included."""
    return prefix_pool_mb * 2**20 // shapes.page_bytes


def prefixes_held(prefix_pool_mb: int, tokens: int, shapes: PrefixShapes = FAST_PATH_SHAPES) -> int:
    """How many prefixes of `tokens` tokens a pool of `prefix_pool_mb` MB holds at once."""
    return max(0, pool_pages(prefix_pool_mb, shapes) - RESERVED_PAGES) // shapes.pages_per_prefix(tokens)


def prefix_pool_mb_for(rows: int, shapes: PrefixShapes = FAST_PATH_SHAPES) -> int:
    """The smallest pool, in MB, that holds `rows` prefixes of the longest prefix at once."""
    pages = rows * shapes.pages_per_prefix(LONGEST_PREFIX_TOKENS) + RESERVED_PAGES
    return _cdiv(pages * shapes.page_bytes, 2**20)


def check_pool_size(prefix_pool_mb: object) -> int:
    """--prefix-pool-mb as given: a whole number of MB, at least prefix_pool_mb_for(1) (one prefix of the longest
    length). EngineError otherwise."""
    smallest = prefix_pool_mb_for(1)
    if isinstance(prefix_pool_mb, bool) or not isinstance(prefix_pool_mb, int) or prefix_pool_mb < smallest:
        raise EngineError(f"--prefix-pool-mb {prefix_pool_mb!r}: give a whole number of megabytes, at least {smallest} "
                          f"(one prefix of {LONGEST_PREFIX_TOKENS} tokens); for example 2048 holds "
                          f"{prefixes_held(2048, 1000)} prefixes of 1,000 tokens")
    return prefix_pool_mb


def check_pool_holds(prefix_pool_mb: int, max_batch_rows: int) -> None:
    """Refuse (EngineError) a pool that cannot hold `max_batch_rows` prefixes of the longest prefix at once: a pass of
    that many rows, each with its own prefix, would otherwise be refused while serving (design Risk 5)."""
    needed = prefix_pool_mb_for(max_batch_rows)
    if prefix_pool_mb < needed:
        raise EngineError(
            f"--prefix-pool-mb {prefix_pool_mb} cannot hold --max-batch-rows {max_batch_rows} prefixes of "
            f"{LONGEST_PREFIX_TOKENS} tokens (the longest prefix) at once, as one pass of {max_batch_rows} rows may "
            f"need; give --prefix-pool-mb at least {needed}, or lower --max-batch-rows.")


@dataclass
class _Entry:
    key: tuple[int, str]
    length: int
    pages: list[int]


class PrefixPool:
    """Pages (see the module docstring), allocated once on `device`, and which prefix holds which."""

    def __init__(self, pages: int, device: str | torch.device = "cuda", *,
                 shapes: PrefixShapes = FAST_PATH_SHAPES) -> None:
        shapes.check()
        if pages < RESERVED_PAGES + shapes.fixed_pages:
            raise ValueError(f"A prefix pool of {pages} pages cannot hold a single prefix ({shapes.fixed_pages} pages "
                             "and the 2 reserved)")
        self.shapes, self.pages, self.page_tokens = shapes, pages, shapes.page_tokens
        self.fixed_pages = shapes.fixed_pages
        self.slots = 1 + (pages - RESERVED_PAGES) // self.fixed_pages  # every prefix takes fixed_pages at least
        words = shapes.page_bytes // 4
        self.data = torch.zeros(pages, words, device=device, dtype=torch.int32)
        halves = self.data.view(torch.bfloat16)
        self.kv = halves.view(pages, shapes.attn_layers, 2, self.page_tokens, *shapes.kv)
        heads = shapes.heads_per_page
        self.state = self.data.view(torch.float32)[:, :heads * shapes.state[1] * shapes.state[2]].view(
            pages, heads, shapes.state[1], shapes.state[2])
        layers = shapes.layers_per_conv_page
        self.conv = halves[:, :layers * shapes.conv[0] * shapes.conv[1]].view(pages, layers, *shapes.conv)
        self.max_kv_pages = _cdiv(LONGEST_PREFIX_TOKENS, self.page_tokens)
        self.tables = torch.zeros(self.slots, self.fixed_pages + self.max_kv_pages, device=device, dtype=torch.long)
        self.tables[0, :self.fixed_pages] = ZERO_PAGE
        self._entries: OrderedDict[tuple[int, str], int] = OrderedDict()  # least recently used first
        self._slot: dict[int, _Entry] = {}
        self._pinned: dict[int, int] = {}  # slot: pin count
        self._free_slots = list(range(1, self.slots))  # a heap: the lowest free slot is handed out first
        self._free_pages = list(range(pages - 1, RESERVED_PAGES - 1, -1))  # a stack (in order at start)

    @property
    def nbytes(self) -> int:
        return self.data.numel() * self.data.element_size()

    def __len__(self) -> int:
        return len(self._entries)

    def free_slot_count(self) -> int:
        return len(self._free_slots)

    def free_page_count(self) -> int:
        return len(self._free_pages)

    def lookup(self, adapter_index: int, text: str) -> int | None:
        """The slot holding this prefix, now the most recently used, or None."""
        key = (adapter_index, text)
        slot = self._entries.get(key)
        if slot is not None:
            self._entries.move_to_end(key)
        return slot

    def reserve(self, adapter_index: int, text: str, tokens: int) -> int:
        """A slot and its pages for a new prefix of `tokens` tokens, the most recently used; the caller writes its
        state (write). Evicts the least recently used prefixes that are not pinned, as many as it takes, or none at
        all when that would not make room (EngineError)."""
        key = (adapter_index, text)
        if key in self._entries:
            raise ValueError(f"Prefix of adapter row {adapter_index} is already in the pool "
                             f"(slot {self._entries[key]})")
        if tokens > LONGEST_PREFIX_TOKENS:
            raise ValueError(f"A prefix of {tokens} tokens is longer than the longest prefix ({LONGEST_PREFIX_TOKENS})")
        wanted = self.shapes.pages_per_prefix(tokens)
        if wanted > self.pages - RESERVED_PAGES:
            raise EngineError(f"A prefix of {tokens} tokens needs {wanted} pages; the prefix pool has "
                              f"{self.pages - RESERVED_PAGES} in all (raise --prefix-pool-mb)")
        evictable = [slot for slot in self._entries.values() if slot not in self._pinned]
        if (len(self._free_slots) + len(evictable) < 1
                or len(self._free_pages) + sum(len(self._slot[slot].pages) for slot in evictable) < wanted):
            raise EngineError(f"No room in the prefix pool for a prefix of {tokens} tokens ({wanted} pages): "
                              f"{len(self._pinned)} prefixes are pinned by the pass being formed, and the others free "
                              f"too few pages. Lower --max-batch-rows or raise --prefix-pool-mb.")
        for slot in evictable:  # oldest first
            if self._free_slots and len(self._free_pages) >= wanted:
                break
            self._free(slot)
        slot = heapq.heappop(self._free_slots)
        pages = [self._free_pages.pop() for _ in range(wanted)]
        self.tables[slot, :wanted] = torch.tensor(pages, dtype=torch.long)
        self._slot[slot] = _Entry(key, tokens, pages)
        self._entries[key] = slot
        return slot

    def release(self, slot: int) -> None:
        """Give a prefix's slot and pages back (its pass failed, so its state is not what its key says)."""
        if slot in self._pinned:
            raise ValueError(f"Prefix slot {slot} is pinned")
        self._in_use(slot)
        self._free(slot)

    def block_table(self, slot: int) -> list[int]:
        """The slot's pages: state pages, convolution pages, then key/value pages in token order (slot 0: the zero
        page for the state and convolution pages, no key/value pages). Also in tables[slot, :count]."""
        return [ZERO_PAGE] * self.fixed_pages if slot == 0 else list(self._in_use(slot).pages)

    def key(self, slot: int) -> tuple[int, str]:
        """The (adapter index, prefix text) whose prefix the slot holds (ValueError for slot 0 or a free slot)."""
        return self._in_use(slot).key

    def length(self, slot: int) -> int:
        """The slot's prefix length in tokens (0 for slot 0)."""
        return 0 if slot == 0 else self._in_use(slot).length

    def pin(self, slots: Iterable[int]) -> None:
        """Keep these slots from eviction until unpinned (counted: pin a slot twice, unpin it twice)."""
        slots = list(slots)
        for slot in slots:
            self._in_use(slot)
        for slot in slots:
            self._pinned[slot] = self._pinned.get(slot, 0) + 1

    def unpin(self, slots: Iterable[int]) -> None:
        for slot in slots:
            count = self._pinned.get(slot)
            if count is None:
                raise ValueError(f"Prefix slot {slot} is not pinned")
            if count == 1:
                del self._pinned[slot]
            else:
                self._pinned[slot] = count - 1

    def forget_adapter(self, adapter_index: int) -> None:
        """Free every prefix of this adapter row (its adapter was added, replaced or removed)."""
        slots = [slot for (index, _), slot in self._entries.items() if index == adapter_index]
        pinned = [slot for slot in slots if slot in self._pinned]
        if pinned:
            raise ValueError(f"Adapter row {adapter_index} has pinned prefix slots {pinned}: an adapter changes only "
                             "between passes")
        for slot in slots:
            self._free(slot)

    # ------------------------------------------------------------ the state in the pages

    def write(self, pages: torch.Tensor, conv: torch.Tensor, state: torch.Tensor, kv: torch.Tensor) -> None:
        """Write one prefix's state into the pages a block table lists (`pages`, on the pool's device: the state
        pages, the convolution pages, then one key/value page per 32 tokens of `kv`). conv (layers, 3, channels),
        state (layers, heads, key dim, value dim), kv (attention layers, 2, tokens, heads, dim) with a whole number of
        pages of tokens. Only tensor operations reading `pages` from device memory, so a CUDA graph can record it.

        Entries may repeat page 0 (the prefix pass lists it for every page its padding tokens fill, and recording
        lists it everywhere): index_copy_ then writes page 0 several times in no set order, which is harmless, since
        nothing ever reads page 0."""
        shapes = self.shapes
        s, c = shapes.state_pages, shapes.conv_pages
        self.state.index_copy_(0, pages[:s], state.reshape(s, *self.state.shape[1:]))
        self.conv.index_copy_(0, pages[s:s + c], conv.reshape(c, *self.conv.shape[1:]))
        n = kv.shape[2] // self.page_tokens
        self.kv.index_copy_(0, pages[s + c:s + c + n],
                            kv.view(shapes.attn_layers, 2, n, self.page_tokens, *shapes.kv).permute(2, 0, 1, 3, 4, 5))

    def gather(self, slot: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """A prefix's state, copied out of its pages: conv (layers, 3, channels), state (layers, heads, key dim, value
        dim), kv (attention layers, 2, prefix length, heads, dim)."""
        shapes = self.shapes
        s, c = shapes.state_pages, shapes.conv_pages
        table = self.tables[slot]
        state = self.state.index_select(0, table[:s]).reshape(shapes.gdn_layers, *shapes.state)
        conv = self.conv.index_select(0, table[s:s + c]).reshape(shapes.gdn_layers, *shapes.conv)
        p = self.length(slot)
        n = _cdiv(p, self.page_tokens)
        kv = self.kv.index_select(0, table[s + c:s + c + n]).permute(1, 2, 0, 3, 4, 5)
        return conv, state, kv.reshape(shapes.attn_layers, 2, n * self.page_tokens, *shapes.kv)[:, :, :p]

    def _in_use(self, slot: int) -> _Entry:
        entry = self._slot.get(slot)
        if entry is None:
            raise ValueError(f"Prefix slot {slot} holds no prefix")
        return entry

    def _free(self, slot: int) -> None:
        entry = self._slot.pop(slot)
        del self._entries[entry.key]
        self._free_pages.extend(reversed(entry.pages))
        heapq.heappush(self._free_slots, slot)
