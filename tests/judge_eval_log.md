# Judge Eval Log

## 2026-09-11 — near_empty_session.json

Model: `qwen3.5-4b-32k-fast`

1/3 run(s) failed — see details below

- run 1: overall_score=15, interruptions=0
- run 2: overall_score=10, interruptions=0
- run 3: FAILED — POST /chat/completions failed (429): {"request_id":"05033ffb-b4d1-445b-97d5-a412b89c8194","message":"too many requests for this action","code":429}

## 2026-09-11 — sample_session_timeline.json

Model: `qwen3.5-4b-32k-fast`

Gateway rate limits (429) blocked a full automated 3x run tonight — every
`judge_eval.py` attempt after the first got shut out entirely (see the
repeated 429s that were here and got cleaned out of this log). These two
runs were captured by hand, spaced further apart:

- run 1: overall_score=15, interruptions=0 — the judge omitted the one
  real, marked interruption entirely (not a hallucinated extra one; a
  dropped real one — the opposite failure from what the interruption-count
  safety net catches).
- run 2: overall_score=15, interruptions=1 — correctly included the marked
  interruption, with a reasonable trigger/recovery_pattern/note.

Takeaway: the near-empty fixture's fixes (score calibration, zero phantom
interruptions) held up across every run tonight. But this surfaces a new,
separate reliability gap — the judge can silently drop a real, marked
interruption from its output, and nothing currently catches that (the
safety net in `call_judge` only catches *too many* interruptions, not too
few). Worth a follow-up: a symmetric check that retries when
`len(interruptions) < marked_count`. Not built yet — flagging it rather
than guessing at a fix under rate-limit pressure.

**Next step:** re-run `python tests/judge_eval.py` later once the rate
limit window has reset, for a clean 3x/3x baseline on both fixtures.
