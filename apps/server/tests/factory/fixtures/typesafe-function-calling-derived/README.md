# Derived from the TypeSafe "Function calling" cookbook (for the factory's import mode)

Made from the unchanged files in `../typesafe-function-calling/` (MIT, see its SOURCE.md); nothing here edits them.

- `questions.json`: the 54 questions exactly as the cookbook's `dispatch.all_questions(spec, trader.TOOLS)` builds
  them (built with stand-ins for the plotting, data and API libraries, which questions do not use).
- `signatures.json`: each function's closed-set arguments from `dispatch.closed_sets` and `trader.py`'s signatures:
  shape, allowed values, default, whether it is required, whether `spec.json` gives it a "stated" question.
- `conditions.json`: when each question is read, from `dispatch.read`: a function's questions only when `__tool__`
  names it; an argument with a "stated" question only when that question is answered yes.
- `description.md`: the short task description for the teacher (who writes the requests and the hard patterns).
  It never defines questions; the questions are imported verbatim.
