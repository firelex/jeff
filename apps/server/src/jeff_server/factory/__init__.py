"""The adapter factory: turn a plain-English task description into a tested, served LoRA adapter.

Steps (each timed with server-clock events in the run's events.jsonl): interpret the description into a task spec,
the owner reviews and approves it, generate training data with the teacher model, check the data, train, calibrate,
evaluate, publish into the server's adapter folder. Spec: docs/proposals/2026-10-17-adapter-from-a-prompt.md (in
jeff-dev).

The API (routes.py) queues the steps (queue.py); the factory worker (worker.py, `jeff-server factory worker`), a
process of its own, runs them one at a time.
"""
