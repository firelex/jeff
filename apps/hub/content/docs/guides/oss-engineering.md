---
title: Engineering
---
# Engineering

The engineering adapters help a software project keep up with its own automation: why a build-and-test run failed,
which part of the project owns a new issue or pull request, and what type it is. They read what a maintainer
would read (the log, the change, the run history, the issue text) and give an answer a bot can act on, such as
re-running a flaky test or labelling and assigning an issue.

Each example below uses an input written for this page and shows the answer returned by the live API. To run one yourself, set `JEFF_API_KEY` as in the [Quick start](/docs/guides/quickstart).

## Build failure triage

[Build failure triage](/catalogue/jeff.engineering.ci-failure) reads a failed automated build-and-test run (its log,
the code change and the run history) and says what kind of failure it is: a real regression, a flaky test,
infrastructure, a dependency change, a test that needs updating, and so on. Here a change meant to speed up a CSV
reader fails a test that passes on the main branch:

<!-- example: oss-ci-failure -->

`real_regression` means the change itself broke the code: the test fails the same way twice on this commit, passes
on the main branch, and nothing else changed. Send it back to the author rather than re-running it.

## Issue and pull request classification

[Issue and pull request classification](/catalogue/jeff.engineering.issue-route) chooses the component responsible for a new issue or pull request
from a menu, and identifies its type: bug, feature, question, docs or
duplicate. Those are two functions. The first picks the owner, from the components you pass as labels:

<!-- example: oss-issue-route-owner -->

`readers` is the owning component: the crash shows in a chart command, but the error is raised in the NetCDF
reader.

The second identifies the type of a different issue: a request to silence progress output:

<!-- example: oss-issue-route-kind -->

`feature` is right (the issue asks for a new option), but its confidence is only 0.564. Low confidence means the
function is uncertain: across many answers assigned a confidence of about 0.56, more than four in ten are wrong. Here the request sits
close to an open issue asking for a `--quiet` flag, so it could also be read as a duplicate. Do not label the issue
on such an answer alone: check `abstained` (it is `true` when the confidence is below the threshold the function was
calibrated with), and leave uncertain cases for a maintainer, or let Jeff pass them on with an `on_low_confidence`
policy. See [Calibrated confidence](/docs/guides/how-it-works#section-calibrated-confidence) and [Escalation](/docs/guides/escalation).
