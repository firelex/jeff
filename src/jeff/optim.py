"""AdamW with exact FP32 master weights and moments kept in host memory."""

from collections.abc import Iterable, Mapping
from typing import TypedDict, cast

import torch


class ParameterGroup(TypedDict):
    params: list[torch.Tensor]
    lr: float
    peak_lr: float  # the scheduled rate is peak_lr times the warmup/cosine factor


class OffloadState(TypedDict):
    format_version: int
    parameter_names: list[str]
    masters: list[torch.Tensor]
    optimizer: dict[str, object]


class CPUOffloadAdamW:
    def __init__(
        self, named_parameters: Iterable[tuple[str, torch.nn.Parameter]], lr: float,
        weight_decay: float = 0.01, learning_rates: Mapping[str, float] | None = None,
    ) -> None:
        """`learning_rates`: peak rates for some parameters by name (for example the readout of a LoRA run); the rest use
        `lr`. Each distinct rate is one parameter group."""
        parameters = [(name, parameter) for name, parameter in named_parameters if parameter.requires_grad]
        if not parameters:
            raise ValueError("The optimizer needs trainable parameters.")
        self.names = [name for name, _ in parameters]
        if len(set(self.names)) != len(self.names):
            raise ValueError("Parameter names must be unique.")
        self.parameters = [parameter for _, parameter in parameters]
        self.masters = [torch.nn.Parameter(parameter.detach().to(device="cpu", dtype=torch.float32, copy=True))
                        for parameter in self.parameters]
        self.gradients = [torch.empty_like(master) for master in self.masters]
        self.buffers = {
            dtype: torch.empty(max(parameter.numel() for parameter in self.parameters if parameter.dtype == dtype),
                               dtype=dtype, device="cpu", pin_memory=any(parameter.is_cuda for parameter in self.parameters))
            for dtype in {parameter.dtype for parameter in self.parameters}
        }
        rates = [(learning_rates or {}).get(name, lr) for name in self.names]
        groups = [{"params": [master for master, own in zip(self.masters, rates) if own == rate], "lr": rate, "peak_lr": rate}
                  for rate in dict.fromkeys(rates)]
        self.optimizer = torch.optim.AdamW(groups, lr=lr, weight_decay=weight_decay, fused=True)

    @property
    def param_groups(self) -> list[ParameterGroup]:
        return cast(list[ParameterGroup], self.optimizer.param_groups)

    def zero_grad(self, set_to_none: bool = True) -> None:
        self.optimizer.zero_grad(set_to_none=set_to_none)
        for parameter in self.parameters:
            if set_to_none:
                parameter.grad = None
            elif parameter.grad is not None:
                parameter.grad.zero_()

    @torch.no_grad()
    def step(self) -> None:
        for parameter, master, gradient in zip(self.parameters, self.masters, self.gradients):
            if parameter.grad is None:
                master.grad = None
                continue
            buffer = self.buffers[parameter.dtype][:parameter.numel()].view_as(parameter)
            buffer.copy_(parameter.grad, non_blocking=True)
            if parameter.is_cuda:
                torch.cuda.synchronize(parameter.device)
            gradient.copy_(buffer)
            master.grad = gradient
            parameter.grad = None
        self.optimizer.step()
        for parameter, master in zip(self.parameters, self.masters):
            if master.grad is not None:
                buffer = self.buffers[parameter.dtype][:parameter.numel()].view_as(parameter)
                buffer.copy_(master)
                parameter.copy_(buffer, non_blocking=True)
                if parameter.is_cuda:
                    torch.cuda.synchronize(parameter.device)
                master.grad = None

    def state_dict(self) -> OffloadState:
        """Return CPU tensor references for torch.save; never clone the large states."""
        return {"format_version": 1, "parameter_names": list(self.names),
                "masters": [master.detach() for master in self.masters],
                "optimizer": cast(dict[str, object], self.optimizer.state_dict())}

    @torch.no_grad()
    def load_state_dict(self, state: OffloadState) -> None:
        if state["format_version"] != 1 or state["parameter_names"] != self.names:
            raise ValueError("Optimizer checkpoint does not match model parameter names and order.")
        saved_masters = state["masters"]
        if len(saved_masters) != len(self.masters):
            raise ValueError("Optimizer checkpoint has the wrong number of master tensors.")
        for master, saved in zip(self.masters, saved_masters):
            if saved.device.type != "cpu" or saved.dtype != torch.float32 or saved.shape != master.shape:
                raise ValueError("Master tensors must be CPU FP32 with matching parameter shapes.")
        self.optimizer.load_state_dict(state["optimizer"])
        for parameter, master, saved in zip(self.parameters, self.masters, saved_masters):
            master.copy_(saved)
            parameter.copy_(master)
        for parameter in self.parameters:
            if parameter.is_cuda:
                torch.cuda.synchronize(parameter.device)
                break
        self.zero_grad()
