---
title: CI failures routed to the right person
---
# CI failures routed to the right person

## The problem

An open-source library runs its tests on every pull request. When a run fails, someone has to read the log and
decide: is this a real bug in the change, a flaky test, a broken dependency or a problem with the CI machines? Then
the right team has to hear about it. A flaky test that gets a bug report wastes a maintainer's morning, and a real
regression that gets a rerun ships.

## How Jeff helps

Two engineering functions do the sorting, each in one fast call:

1. [CI failure triage](/catalogue/jeff.engineering.ci-failure) reads the failed run's log, the change and the facts
   the CI system records, and names the kind of failure.
2. [Issue routing](/catalogue/jeff.engineering.issue-route) reads the issue filed for a real failure against the
   project's components and open issues, and says what kind of issue it is and which component owns it.

Your CI bot then reruns, files an issue or pings the owning team.

## The solution

Each step below is a live call; the answer under it is the one the API returned. Choose your language and whether to
run it on your machine (free) or through the API.

### A test fails after a code change

A pull request rewrites the table layout, and a table test fails on both attempts, while it passed on every recent
run of the main branch:

<!-- example: worked-ci-regression -->

`real_regression`: the change broke the test. The bot files an issue instead of rerunning.

### A test fails once and passes on rerun

A documentation-only pull request fails a font cache test that also fails now and then on the main branch:

<!-- example: worked-ci-flaky -->

`flaky_test`: the bot reruns it and adds the failure to the flaky test's existing issue, without bothering the
author.

### Route the regression's issue

The issue filed for the regression:

<!-- example: worked-ci-issue-kind -->

`bug`: a fault in the code, not a question or a feature request.

<!-- example: worked-ci-issue-owner -->

`layout`: tables belong to the layout component, so the bot assigns the issue to its team.

## What you get

- Every failed run sorted by cause and every issue routed to a team in two calls, with a confidence your bot can
  check before it acts. All four answers above were above their functions' thresholds.
- How the functions measure, from each adapter's page: CI failure triage 99.7% and issue routing 99.0% on their
  in-house tests (2,000 rows each, from the same generator as the training data). Their rows were kept only where a
  blind LLM check agreed with the label, so these scores are probably inflated, and neither has a public-benchmark
  figure. Check them on your own project's failures before letting the bot act alone.
