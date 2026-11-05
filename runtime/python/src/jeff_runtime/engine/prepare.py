"""The fast path's preparation of a request, on the CPU only: each row's prompt in its adapter's own layout, split into
the reusable prefix text and the new text, and the new text tokenised (see fast_path.py's module docstring for where
the split falls and why it is exact). Nothing here touches the GPU or any shared mutable state: the caller passes the
processor (chat template and tokenizer) to use, so each preparation thread of the engine uses its own copy, and the
prompt layout, read from the engine's table under its lock.

Kept apart from fast_path.py (which needs Triton and a GPU to import) so the engine can prepare rows on arrival on
any thread, and tests can run it on any machine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jeff_runtime.engine.model import decision_messages, options

if TYPE_CHECKING:
    from jeff_runtime.engine.types import DecisionInput

LATEST = "\n\nLatest:\n"
USER_TURN = "<|im_start|>user\n"
NEW_BUCKETS = (32, 64, 128, 256, 512, 1024)  # the new-token buckets of the fast path's graphs


@dataclass
class Row:
    """One question ready for the fast path."""
    prefix_text: str
    new_ids: list[int]
    count: int  # answer options


def split(processor: Any, codes: Sequence[str], row: DecisionInput, layout: str) -> tuple[str, str]:
    """The prompt of one question in the given prompt layout, rendered with `processor`'s chat template and split
    into the reusable prefix text and the new text."""
    text = cast(str, processor.apply_chat_template(decision_messages(row, codes, layout), tokenize=False,
                                                   add_generation_prompt=True, enable_thinking=False))
    if layout == "live-last" and isinstance(row["state"], dict):
        cut = text.rindex(LATEST) + len(LATEST)
    else:
        cut = text.index(USER_TURN) + len(USER_TURN)
    return text[:cut], text[cut:]


def prepare_rows(processor: Any, tokenizer: Any, codes: Sequence[str], layout: str,
                 rows: Sequence[DecisionInput]) -> list[Row] | str:
    """The rows ready for the fast path under an adapter whose prompt layout is `layout`, or why the request belongs
    on the standard path (the routing rule, decided here before any work: a row with images, or with more new tokens
    than the largest bucket, sends the whole request there)."""
    if any(row.get("images") for row in rows):
        return "images"
    prepared = []
    for row in rows:
        prefix_text, new_text = split(processor, codes, row, layout)
        new_ids = cast(list[int], tokenizer(new_text, add_special_tokens=False)["input_ids"])
        if len(new_ids) > max(NEW_BUCKETS):
            return f"more than {max(NEW_BUCKETS)} new tokens"
        prepared.append(Row(prefix_text, new_ids, len(options(row["question"])[0])))
    return prepared
