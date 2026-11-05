"""Test set-up shared by the runtime's tests.

One log message is silenced, narrowly: transformers' warning that `causal_conv1d_fn` / `causal_conv1d_update` fall back
to their reference PyTorch implementation because the `causal-conv1d` package is not installed. It is a log record
(transformers' logger.warning_once in transformers.integrations.hub_kernels), not a Python warning, so pytest's
filterwarnings cannot reach it; a logging filter on that one logger does.

Why it is harmless here (checked on datigator, 2026-10-24, transformers' Qwen3.5 modelling code):
- the fast path never calls transformers' convolution: its own kernels do the Gated DeltaNet convolution
  (batched_kernels.gdn_prep_rows and the prefix pass);
- the standard path (images, more than 1,024 new tokens, and the reference of the agreement tests) does, through
  Qwen3_5GatedDeltaNet, and the fallback there is F.conv1d: the same computation, slower, and exactly what the
  agreement tests' reference has always used. Installing causal-conv1d would change the standard path's rounding
  and is not in the lock.
- the Gated DeltaNet recurrence itself is fla's on both paths (installed), so its fallback warning, which would mean a
  much slower standard path, is not silenced."""

from __future__ import annotations

import logging

SILENCED = ("`causal_conv1d_fn` is falling back", "`causal_conv1d_update` is falling back")


class _CausalConv1dFallback(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(SILENCED)


logging.getLogger("transformers.integrations.hub_kernels").addFilter(_CausalConv1dFallback())
