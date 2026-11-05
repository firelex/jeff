import os

import jeff


def current_token() -> str:
    # Called before every request, so a token refreshed here (read again from the environment, or fetched from your
    # secret store) is used from the next request on.
    return os.environ["JEFF_API_KEY"]


with jeff.Jeff.remote("{{origin}}", token=current_token, timeout_s=30) as client:

    guard = client.function("guard.attempt", context={
        "application": "A support chat assistant for an online shop.",
        "source": "user message",
    })

    print(guard("What are your opening hours?").value)
