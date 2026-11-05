"""Factory settings, read once from the environment when the server or the factory worker starts.

JEFF_FACTORY        off or owner; required as soon as any other variable below is set, and unset means off. Off: every
                    build endpoint answers 403 FactoryOff; recorded replays are served, and description checks and
                    previews (interpretation only, run in the API process) too when the three teacher settings are
                    given, with JEFF_FACTORY_DIR.
JEFF_FACTORY_DIR    folder for runs/, previews/, replays/, description-checks/, the job queue and the worker's heartbeat
                    (required in owner mode; in off mode, for replays, and required with the teacher settings).
JEFF_TEACHER_URL    OpenAI-compatible base URL of the teacher model that writes the training data (owner mode).
JEFF_TEACHER_KEY    its API key (owner mode).
JEFF_TEACHER_MODEL  its model name (owner mode).
JEFF_TRAINER        folder of the pinned trainer install: .venv/bin/jeff-train and TRAINER_COMMIT (owner mode only).
JEFF_FACTORY_MIN_FREE_GB  GPU memory, in GB, that must be free before training starts (owner mode only, required).

Build endpoints need the server owner's credentials (jeff_server.operations.owner: the API key or a console session),
not a key of their own. As every server setting, a variable that is set but empty is refused, never read as unset."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jeff.errors import InvalidRequest

# Every variable the factory reads. A public server refuses all of them except JEFF_FACTORY=off
# (jeff_server.settings): the factory spends teacher-model money and trains on the GPU, so it belongs on a private
# server.
VARIABLES = ("JEFF_FACTORY", "JEFF_FACTORY_DIR", "JEFF_TEACHER_URL", "JEFF_TEACHER_KEY", "JEFF_TEACHER_MODEL",
             "JEFF_TRAINER", "JEFF_FACTORY_MIN_FREE_GB")
TEACHER_VARIABLES = ("JEFF_TEACHER_URL", "JEFF_TEACHER_KEY", "JEFF_TEACHER_MODEL")
OWNER_ONLY_VARIABLES = ("JEFF_TRAINER", "JEFF_FACTORY_MIN_FREE_GB")


@dataclass(frozen=True)
class FactoryConfig:
    mode: Literal["off", "owner"]
    root: Path | None
    teacher_url: str | None = None
    teacher_key: str | None = None
    teacher_model: str | None = None
    trainer: Path | None = None
    min_free_gb: float | None = None

    @classmethod
    def off(cls) -> "FactoryConfig":
        """The factory as a server with no factory variable at all has it: off, with no folder."""
        return cls(mode="off", root=None)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "FactoryConfig":
        values = {name: _value(env, name) for name in VARIABLES}
        mode = values["JEFF_FACTORY"]
        if mode is None:
            given = [name for name in VARIABLES if values[name] is not None]
            if given:
                raise InvalidRequest(f"{', '.join(given)} set without JEFF_FACTORY; set JEFF_FACTORY=owner to build "
                                     "adapters on this server, or JEFF_FACTORY=off")
            return cls.off()
        root = Path(values["JEFF_FACTORY_DIR"]) if values["JEFF_FACTORY_DIR"] is not None else None
        teacher = [values[name] for name in TEACHER_VARIABLES]
        if mode == "off":
            # The teacher settings are optional here: when all three are set, description checks are offered.
            if any(teacher) and not all(teacher):
                raise InvalidRequest("Set all of JEFF_TEACHER_URL, JEFF_TEACHER_KEY and JEFF_TEACHER_MODEL, or none")
            if all(teacher) and root is None:
                raise InvalidRequest("The teacher settings offer description checks and previews, which are kept in "
                                     "JEFF_FACTORY_DIR; set JEFF_FACTORY_DIR, or unset the teacher settings")
            unused = [name for name in OWNER_ONLY_VARIABLES if values[name] is not None]
            if unused:
                raise InvalidRequest(f"{', '.join(unused)} only apply with JEFF_FACTORY=owner; unset them, or set "
                                     "JEFF_FACTORY=owner")
            return cls(mode="off", root=root, teacher_url=teacher[0].rstrip("/") if teacher[0] else None,
                       teacher_key=teacher[1], teacher_model=teacher[2])
        if mode != "owner":
            raise InvalidRequest(f"JEFF_FACTORY={mode!r}; use off or owner")
        for name in ("JEFF_FACTORY_DIR", *TEACHER_VARIABLES, *OWNER_ONLY_VARIABLES):
            if values[name] is None:
                raise InvalidRequest(f"JEFF_FACTORY=owner needs {name}")
        trainer = Path(str(values["JEFF_TRAINER"]))
        for path in (trainer / ".venv" / "bin" / "jeff-train", trainer / "TRAINER_COMMIT"):
            if not path.exists():
                raise InvalidRequest(f"JEFF_TRAINER={trainer}: {path} is missing")
        return cls(mode="owner", root=root, teacher_url=str(teacher[0]).rstrip("/"), teacher_key=teacher[1],
                   teacher_model=teacher[2], trainer=trainer,
                   min_free_gb=_gigabytes(str(values["JEFF_FACTORY_MIN_FREE_GB"])))

    @property
    def can_check(self) -> bool:
        """Description checks need only the teacher settings, so they also work with building switched off."""
        return bool(self.teacher_url and self.teacher_key and self.teacher_model and self.root)

    @property
    def runs(self) -> Path:
        return self.folder("runs")

    @property
    def replays(self) -> Path:
        return self.folder("replays")

    @property
    def required_root(self) -> Path:
        if self.root is None:
            raise InvalidRequest("JEFF_FACTORY_DIR is not set")
        return self.root

    def folder(self, name: str) -> Path:
        path = self.required_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def trainer_commit(self) -> str:
        path = self.required_trainer / "TRAINER_COMMIT"
        return path.read_text().strip()

    @property
    def required_trainer(self) -> Path:
        if self.trainer is None:
            raise InvalidRequest("JEFF_TRAINER is not set")
        return self.trainer

    @property
    def required_min_free_gb(self) -> float:
        if self.min_free_gb is None:
            raise InvalidRequest("JEFF_FACTORY_MIN_FREE_GB is not set")
        return self.min_free_gb


def given(env: Mapping[str, str]) -> list[str]:
    """The factory variables set in `env` that switch the factory on or configure it: every one of VARIABLES that
    is present, except JEFF_FACTORY=off. A public server refuses these."""
    return [name for name in VARIABLES if name in env and not (name == "JEFF_FACTORY" and env[name] == "off")]


def _value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value == "":
        raise InvalidRequest(f"{name} is set but empty; set a value or unset it")
    return value


def _gigabytes(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise InvalidRequest(f"JEFF_FACTORY_MIN_FREE_GB={value!r}; use a number of GB, for example 16") from error
    if not math.isfinite(number) or number < 0:
        raise InvalidRequest(f"JEFF_FACTORY_MIN_FREE_GB={value!r}; use a number of GB, 0 or more")
    return number
