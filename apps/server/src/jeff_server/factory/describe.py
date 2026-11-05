"""The description check, before a run starts: the teacher reads the pasted description, lists what is missing,
ambiguous or contradictory, asks the questions a careful builder would ask, and writes an improved description that
keeps everything the person said and states each added assumption plainly. The person then keeps their own text,
takes the improved one, or edits it; only the confirmed text starts the run.

Checks are saved in checks/<id>.json with their server-clock start and end, so the run's timeline can show the check
(teacher time) and the confirmation (the person's time) before the run's own start."""

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.store import now, read_json, write_json
from jeff_server.factory.teacher import Teacher

PLACEHOLDER = re.compile(r"[.…\s]*|etc\.?|n/?a|tbd", re.IGNORECASE)
text = {"type": "string"}
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["ready", "summary", "problems", "questions", "improved_description", "changes"],
    "properties": {
        "ready": {"type": "boolean"},
        "summary": text,
        "problems": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["severity", "text"],
            "properties": {"severity": {"type": "string", "enum": ["blocking", "important", "minor"]}, "text": text}}},
        "questions": {"type": "array", "items": text},
        "improved_description": text,
        "changes": {"type": "array", "items": text},
    },
}

PROMPT = """A person wants to build a small classifier and has described, in plain English, the decision it should make. \
Before anything is built, check the description and improve it.

How the classifier works, so you know what a complete description needs: it reads a "state" (a small JSON object: a \
few fields that stay the same for one deployment, such as a desk name, and one field that changes on every request, \
such as a typed message) and answers one or more fixed questions. Each question has a closed list of options (pick \
one), or is a yes/no question, or is a point on a described scale. Training examples will be invented by a language \
model from the description alone, so anything the description leaves open will be guessed.

A complete description says:
- what is decided, and for whom;
- every option of every question, with what each one means, and when two options could both seem to fit, which wins;
- what to answer when nothing fits (a "none" or "other" option, or a rule);
- what the state contains: the fixed fields and the changing field;
- defaults for anything a request may leave out;
- which questions are asked of every request and which only in some cases;
- the hard or tricky cases to include.

Do this:
1. "ready": true if a careful builder could build the classifier from the description without guessing anything \
important; otherwise false.
2. "summary": one sentence saying what the classifier decides.
3. "problems": each gap, ambiguity, contradiction or undefined term, with a "severity": "blocking" (cannot be built \
sensibly without an answer), "important" (would be guessed and could easily be guessed wrong) or "minor". Say \
concretely what is missing. An empty list if there are none.
4. "questions": the questions you would ask the person, most important first. An empty list if there are none.
5. "improved_description": the description rewritten so it is complete and unambiguous. Keep everything the person \
wrote: every option, name, value and rule, word for word where possible, and never drop or change any of them. Where \
something is missing, add the most reasonable choice and mark it in the text with "(assumed)" so the person can see \
and change it. Do not add options, functions or values the person did not ask for, except a "none" option when the \
person clearly needs one and gave none (mark it "(assumed)"). Keep the person's structure and headings.
6. "changes": one short line per change you made in the improved description.

Write every field out in full sentences. The improved description must be the complete text, at least as long as \
the original.

The description:
<<<
{description}
>>>
{examples}"""


def check(teacher: Teacher, folder: Path, description: str, examples: str | None) -> dict[str, Any]:
    if not description.strip():
        raise ValueError("Describe the decision first")
    started = now()
    extra = f"\nThe person also gave these example cases with their answers:\n<<<\n{examples}\n>>>\n" if examples else ""
    def complete(value: Any) -> None:
        """Reject placeholder or truncated replies (they are retried); the shape is already checked against SCHEMA."""
        for field in ("summary", "improved_description"):
            if PLACEHOLDER.fullmatch(value[field].strip()):
                raise ValueError(f"{field} is a placeholder: {value[field]!r}")
        if len(value["improved_description"]) < 0.8 * len(description):
            raise ValueError(f"the improved description has {len(value['improved_description'])} characters, the "
                             f"original {len(description)}: it must keep everything")
        if any(PLACEHOLDER.fullmatch(item.strip()) for item in value["changes"] + value["questions"]):
            raise ValueError("a change or question is a placeholder")
    result, model = teacher.ask(PROMPT.format(description=description, examples=extra), SCHEMA, "description_check",
                                check=complete)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    record = {"id": f"check-{stamp}-{secrets.token_hex(3)}", "started": started, "ended": now(),
              "description": description, "examples": examples, "teacher_model": model,
              "teacher": teacher.usage.snapshot(), "result": result, "used_by": None}
    write_json(folder / f"{record['id']}.json", record)
    return record


def load(folder: Path, check_id: str) -> dict[str, Any]:
    path = folder / f"{check_id}.json"
    if "/" in check_id or check_id.startswith(".") or not path.exists():
        raise KeyError(check_id)
    return read_json(path)


def run_check(config: FactoryConfig, description: str, examples: str | None) -> dict[str, Any]:
    """A check with a fresh teacher cache (a repeated description is checked again, not answered from a cache)."""
    if not config.can_check:
        raise PermissionError("Description checks are not set up on this server (no teacher settings)")
    folder = config.folder("description-checks")
    assert config.teacher_url and config.teacher_key and config.teacher_model
    teacher = Teacher(config.teacher_url, config.teacher_key, config.teacher_model, folder / "cache" / secrets.token_hex(8))
    return check(teacher, folder, description, examples)
