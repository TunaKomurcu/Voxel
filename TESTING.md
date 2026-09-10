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
  response.

- `test_judge_output_schema.py`
  Given a fixed transcript fixture and a **mocked** LLM Gateway response
  (not a live call), assert that our code validates/rejects malformed judge
  output correctly (missing score, score out of range, missing suggestions
  field, etc). This tests our validation logic, not the LLM's judgment.

- `test_agent_config.py`
  Load `agents/voxel-investor.jsonc` and assert required fields are present
  and in valid ranges (`vad_threshold` 0.0–1.0, `min_silence`/`max_silence`
  are ints, `interrupt_response` is a bool, etc). Cheap insurance against a
  typo silently breaking the interruption mechanic.

Run with: `pytest tests/`

## Layer 2 — Judge consistency check (semi-automated)

The judge LLM call is non-deterministic, so "does it work" isn't a pass/fail
unit test — it's a consistency and sanity check:

1. Keep a small fixed set of **saved transcript fixtures** (5–8 example
   sessions, written by hand or from real test calls): a mix of
   good-recovery, bad-recovery, no-interruption, and edge-case transcripts.
2. Run each fixture through the judge prompt **3 times**.
3. Check: does the score vary wildly between runs (bad — prompt is too
   loosely specified)? Does a transcript with an obviously bad recovery ever
   score higher than an obviously good one (bad — judge isn't tracking the
   right signal)?
4. Log results in `tests/judge_eval_log.md` with date + prompt version, so
   you can see whether a prompt change made things better or worse.

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
