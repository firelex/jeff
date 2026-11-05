"""Task 3 reference: the prefix state today's fast path keeps (fast_path.Prefix: per Gated DeltaNet layer the last
three convolution inputs and the recurrent state, per attention layer the keys and values), recorded at the commit
Task 3 starts from (23fb008), before the slot pool replaces the cache. Stored as SHA-256 digests of each tensor's
bytes (with its shape, dtype, sum and largest magnitude to show the size of any difference), so the CUDA test can
check the pool's prefix pass writes exactly the same bits.

Prefixes: three lengths of real triage test-set text (in three prefix buckets, none a multiple of 32) under the
triage adapter, and the middle one under the base too.

Usage (on datigator, from ~/jeff-dev-next): .venv/bin/python results/step2b1/task3/record_prefixes.py OUTPUT.json COMMIT"""
import glob
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import torch

from jeff_runtime import LocalEngine

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
TRIAGE = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-triage-*/final"
TEXT = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets/triage/test.jsonl")
LENGTHS = (77, 1001, 2601)  # tokens; prefix buckets 128, 1024 and 3072


def digest(tensor: torch.Tensor) -> dict[str, Any]:
    data = tensor.detach().contiguous().cpu()
    raw = data.view(torch.int16) if data.dtype == torch.bfloat16 else data
    return {"shape": list(data.shape), "dtype": str(data.dtype),
            "sha256": hashlib.sha256(raw.numpy().tobytes()).hexdigest(),
            "sum": float(data.double().sum()), "absmax": float(data.abs().max())}


def text_of(tokenizer: Any, lines: list[str], tokens: int) -> str:
    """Real text of exactly `tokens` tokens (checked)."""
    text = "\n".join(str(json.loads(line)["state"]) for line in lines)
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    if len(ids) < tokens + 50:
        raise ValueError(f"not enough text for {tokens} tokens")
    for cut in range(tokens, tokens + 50):
        candidate = str(tokenizer.decode(ids[:cut]))
        if len(tokenizer(candidate, add_special_tokens=False)["input_ids"]) == tokens:
            return candidate
    raise ValueError(f"no cut gives exactly {tokens} tokens")


def main() -> None:
    output = Path(sys.argv[1])
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    commit = sys.argv[2]
    found = glob.glob(TRIAGE)
    if len(found) != 1:
        raise FileNotFoundError(f"expected one triage checkpoint, found {found}")
    engine = LocalEngine(BASE, {"triage": Path(found[0])}, backend="fast", fast_cache_mb=2048, max_adapters=1)
    fast = engine.fast
    lines = TEXT.read_text().splitlines()[:400]
    cases = [("triage", n) for n in LENGTHS] + [(None, LENGTHS[1])]
    out = []
    with torch.inference_mode():
        for adapter, tokens in cases:
            text = text_of(fast.tokenizer, lines, tokens)
            timing = {"model_ms": 0.0, "prefix_ms": 0.0, "prefix_hits": 0, "prefix_misses": 0, "new_tokens": 0}
            entry = fast.prefix(adapter, fast.index(adapter), text, 8192, timing)
            assert len(entry.ids) == tokens, (len(entry.ids), tokens)
            out.append({"adapter": adapter, "tokens": tokens, "text": text, "conv": digest(entry.conv),
                        "state": digest(entry.state), "kv": digest(entry.kv)})
            print(adapter, tokens, out[-1]["kv"]["sha256"][:12], flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"base": str(BASE), "triage": found[0], "commit": commit, "prefixes": out},
                                 indent=1) + "\n")
    print("wrote", output)


if __name__ == "__main__":
    main()
