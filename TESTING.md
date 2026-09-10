# Voxel — Testing Strategy

Voice agents are hard to unit-test end-to-end (the interesting behavior is
audio + timing + an LLM's judgment). So we split testing into layers: the
parts that CAN be deterministic get real automated tests; the parts that
can't get a small, repeatable manual protocol instead of vibes.

## Layer 1 — Deterministic unit tests (pytest)

These don't touch the network or the AssemblyAI API. Fast, run on every change.

- `test_timeline_parsing.py`
  Given a fixed, saved example `timeline` JSON (a fixture file, not a live
  call), assert that our parser correctly extracts turns, flags which turns
  were interruptions, and pairs each interruption with the user's following
  response. Since Phase 2.5, also covers `compute_timing_signals()`:
  `response_latency_ms` and pre/post `words_per_second` for a normal case,
  and the edge case where an interruption is the last turn (no recovery
  turn to measure against, so `response_latency_ms` is `None`).

- `test_judge_output_schema.py`
  Given a fixed transcript fixture and a **mocked** LLM Gateway response
  (not a live call), assert that our code validates/rejects malformed judge
  output correctly (missing score, score out of range, missing suggestions
  field, etc). This tests our validation logic, not the LLM's judgment.
  Since Phase 2.5, this also covers the enriched schema: the three
  `categories` (missing category, out-of-range category score), and each
  interruption's `trigger` / `recovery_pattern` fields against their fixed
  enums (free text outside the enum is rejected) and `better_response_example`
  being present. Timing signals (`response_latency_ms`, `words_per_second`)
  and sentiment results are judge *input*, not output, so they're covered
  under timeline parsing below, not here.

- `test_agent_config.py`
  Load `agents/voxel-investor.jsonc` and assert required fields are present
  and in valid ranges (`vad_threshold` 0.0–1.0, `min_silence`/`max_silence`
  are ints, `interrupt_response` is a bool, etc). Cheap insurance against a
  typo silently breaking the interruption mechanic.

Run with: `pytest tests/`

## Layer 2 — Judge consistency check (semi-automated)

The judge LLM call is non-deterministic, so "does it work" isn't a pass/fail
unit test — it's a consistency and sanity check, run by hand with:

    python tests/judge_eval.py

This runs every fixture in `tests/fixtures/` through `judge.call_judge()`
**3 times** and appends a dated block to `tests/judge_eval_log.md` with each
run's score and interruption count. It is a real script, not just a
description — it costs real LLM Gateway calls, which is why it's a
standalone script rather than a pytest test.

**Run this after any change to `JUDGE_SYSTEM_PROMPT`.** It's easy to forget
the script exists and go back to testing blind by eyeballing one live call —
don't; that's exactly how the near-empty-session scoring bug (see the log,
2026-09-11) slipped through Phase 2.5.

Current fixture set (grows as new edge cases turn up in manual testing):

- `sample_session_timeline.json` — a real session with one genuine,
  timing-confirmed interruption and no recovery turn (the call ends right
  after). Checks that a real interruption gets included, and with a
  grounded trigger/recovery_pattern.
- `near_empty_session.json` — a real, near-content-free call ("Alright."
  and nothing else). Added after a cold-test run scored this 85 with a
  hallucinated `vague_claim` interruption on a turn that was never actually
  marked as one. Checks that low content produces a low score (no
  composure/tone compensation) and that nothing gets marked as an
  interruption when nothing is.
- `no_interruption_session.json` — a real call with strong opening numbers
  (249 customers, 10% MoM, $112 LTV, 8:1 LTV:CAC, 45% margin) that never
  triggers the agent's interrupt mechanic, though the founder later dodges
  a direct CAC follow-up. Scored 42/100 — a reminder that "no interruptions"
  isn't the same as "flawless pitch," and the judge should keep tracking
  actual content quality either way. Also carries a turn where the AGENT's
  own reply got cut short by the user (`status: "interrupted"`,
  `agent_text: "If"`) — confirmed this doesn't get flagged as our kind of
  interruption and doesn't feed into any timing signal (see the comment
  above `compute_timing_signals` in `judge.py`).

What to check when reading the log:
- Does the score vary wildly between runs on the same fixture (bad — prompt
  is too loosely specified)?
- Does a transcript with an obviously bad recovery ever score higher than
  an obviously good one (bad — judge isn't tracking the right signal)? This
  one still needs a human to read the log; the script doesn't compare
  across fixtures for you.

This is intentionally lightweight — the goal is "catch obvious judge
brokenness," not build a full eval harness. AssemblyAI's Bluejay simulation
tooling (mentioned in their docs) does this at much larger scale for
production agents, but it's overkill for a 20-day solo hackathon build —
worth knowing it exists, not worth integrating here.

## Layer 3 — Manual call protocol (before each milestone)

Before moving to the next build phase (see `PLAN.md`), do at least one live
call covering each of:

- [ ] A confident, well-structured pitch (should get interrupted rarely, if
      at all, and score well).
- [ ] A rambling, vague pitch (should get interrupted more, and the
      feedback should point at specific vague moments).
- [ ] A pitch where the user goes silent for a few seconds mid-sentence
      (should not falsely trigger an interruption from silence alone).
- [ ] A very short call (a few seconds) — confirm the app doesn't crash on
      a near-empty timeline.

Keep a running note of failures in `tests/manual_log.md` — one line per
issue found, so nothing gets fixed once and silently regresses later.

## What "objective" means here, concretely

The point of this file isn't to pretend a solo 20-day hackathon project has
production-grade QA. It's to make sure that when you say "the feedback is
useful," that claim rests on:
- a validated output schema (Layer 1),
- a repeatability check on the judge (Layer 2),
- and a fixed manual checklist instead of ad-hoc "seems fine" testing
  (Layer 3) —

— rather than just your own impression from the last call you happened to
make.
