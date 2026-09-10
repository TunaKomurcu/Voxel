# Voxel — Build Plan

Timeframe: Sep 10 – Sep 30, 2026 (20 days, solo).
Submission target: lablab.ai, AssemblyAI Voice Agent Hackathon.

> **After touching `JUDGE_SYSTEM_PROMPT` in `judge.py`, run
> `python tests/judge_eval.py` before moving on.** It's the only thing that
> catches a scoring/labeling regression across saved fixtures instead of
> just the one live call you happened to try — see `TESTING.md` Layer 2.

## Phase 0 — Setup (Day 1)
- [x] Clone `voice-agent-starter-python`, add `.env` with `ASSEMBLYAI_API_KEY`.
- [x] `python publish.py` with the default `minimal` agent, confirm browser
      call works end-to-end (this just proves the pipeline, not the product).
- [x] Commit `CLAUDE.md`, `PLAN.md`, `TESTING.md` to the repo before writing
      any product code.

## Phase 1 — Persona & interruption mechanic (Days 2–4)
- [x] Copy `agents/turn-taking.jsonc` → `agents/voxel-investor.jsonc`.
- [x] Write the system prompt: skeptical/impatient investor persona,
      explicit instruction to interrupt on vague claims, hesitation, or
      unsupported numbers.
- [x] Tune `turn_detection` (`vad_threshold`, `min_silence`, `max_silence`,
      `interrupt_response`) until interruptions feel intentional, not random
      or glitchy.
- [x] Manually test with at least 3 different pitch styles (confident,
      rambling, numbers-heavy) and note what breaks.

**Exit criteria:** a human (you) can have a 2-minute pitch conversation with
the agent and it interrupts at least once in a way that feels deliberate.

## Phase 2 — Session retrieval + judge pass (Days 5–9)
- [x] After a session ends, fetch `GET /v1/sessions/{id}` and parse the
      `timeline` artifact (`user_transcript` / `agent_text` pairs).
- [x] Design the judge prompt: given the timeline, identify each
      interruption point, whether the user recovered well, and output
      structured JSON (score 0–100, 2–3 concrete suggestions).
- [x] Call the judge prompt via AssemblyAI's LLM Gateway.
- [x] Validate the JSON output against a fixed schema before displaying it
      (see `TESTING.md` — this is the first "objective" checkpoint).

**Exit criteria:** feeding a real session timeline through the judge
reliably produces valid, parseable JSON with a score and specific feedback
(not generic praise).

**Status:** done, but `JUDGE_MODEL` in `judge.py` is temporarily
`qwen3.5-4b-32k-fast` — this account doesn't have LLM Gateway access to any
Claude model yet. Switch back to `claude-sonnet-5` once that's enabled on
the AssemblyAI dashboard.

## Phase 2.5 — Enrich judge output (before Phase 3)

The UI in Phase 3 is built against this schema, so the schema has to be
right first.

- [ ] Expand the judge schema from a single score to three categories:
      `content_substance`, `composure_under_pressure`,
      `audience_responsiveness` (each `{score: 0-100, note}`).
- [ ] Add `trigger` and `recovery_pattern` to each interruption, both
      validated against a fixed enum (not free text):
      `trigger`: `vague_claim | unsupported_number | hesitation | ignored_question`
      `recovery_pattern`: `answered_directly | deflected | repeated_claim | asked_clarifying_question`
- [ ] Add `better_response_example` per interruption — one sentence, must
      reference something specific from that actual conversation (a claim,
      a number). The prompt explicitly bans generic advice
      ("be more specific" and the like).
- [ ] Compute timing signals per interruption from the timeline's existing
      turn-level timestamps (no word-level timestamps exist in the
      timeline) — `response_latency_ms` (gap between the interruption
      ending and the user speaking again) and `words_per_second` before vs.
      after the interruption — and hand them to the judge as raw context;
      the judge interprets them, we don't.
- [ ] Run AssemblyAI's sentiment analysis (`sentiment_analysis: true` on
      the classic pre-recorded transcription API, not the Voice Agent API)
      against the session's recording, and pass the sentence-level results
      to the judge as context. Timestamps don't line up across the two
      APIs (recording-relative vs. absolute unix-ms), so results are
      passed as text + label, not time-aligned — the judge matches them to
      turns by content. If sentiment analysis fails (timeout, account
      access), the judge pass still completes without it.

**Exit criteria:** a real conversation's judge output fully matches the new
schema, and its `better_response_example` fields are specific to that
conversation, not generic.

**Status:** done. Live-verified against the real session — worth noting:
`qwen3.5-4b-32k-fast` (the temporary model, see Phase 2) would not
reliably follow the "don't invent numbers" instruction on its own, even
after strengthening the prompt with contrastive examples, and occasionally
returned malformed JSON. Judge output güvenlik ağı eklendi — hangi model
kullanılırsa kullanılsın geçerli JSON + halüsinasyon olmayan sayılar garanti
ediliyor: `call_judge()` now retries up to 3 times on invalid JSON/schema,
and separately retries once (then falls back to a regex-based
`sanitize_response_example()`) if `better_response_example` cites a number
that never appeared in the transcript. Worth re-testing once Claude access
is enabled, since a stronger model may need this safety net less often —
but it stays regardless, since it's model-independent insurance.

## Phase 3 — UI (Days 10–13)
- [ ] Extend the starter's browser client with a post-call results view:
      score, per-interruption breakdown, suggestions.
- [ ] Keep styling minimal — clarity over polish at this stage.

## Phase 4 — Hardening (Days 14–17)
- [ ] Run the objectivity checks in `TESTING.md` across a small transcript
      set; fix judge-prompt drift or inconsistency.
- [ ] Handle edge cases: very short calls, no interruptions triggered, user
      goes silent, connection drop mid-call.
- [ ] Re-test the full flow start to finish, cold (as a first-time user
      would experience it).
- [ ] Known limitation (logged in judge_eval_log.md): judge occasionally
      under-reports a real, marked interruption (asymmetric to the fixed
      over-reporting case). Re-evaluate after switching JUDGE_MODEL to
      claude-sonnet-5 — may resolve with a stronger model; if not, add a
      symmetric "missing interruption" safety net.

## Phase 5 — Demo & submission (Days 18–20)
- [ ] Record a 2–3 min demo video: show an interruption happening live, then
      the resulting feedback screen. Call out explicitly that the
      turn-detection config was hand-tuned as a feature, and that feedback
      comes from a second, independent judge pass over the transcript (not
      just "an LLM wrote something nice").
- [ ] Write the submission description (what it does, how it uses
      AssemblyAI, what's novel about it).
- [ ] Submit on lablab.ai with time to spare before the deadline.

## Explicitly out of scope for the hackathon version
- Multi-user accounts, persistence beyond a single session.
- Telephony / Twilio integration — browser-only is enough for the demo.
- Multiple personas — one well-tuned investor persona beats three shallow ones.
