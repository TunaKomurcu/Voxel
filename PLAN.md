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

**Status:** done. `JUDGE_MODEL` in `judge.py` is `qwen3.5-4b-32k-fast` —
confirmed on the dashboard that every Claude model on the LLM Gateway needs
a paid upgrade this account's free tier doesn't have. This is the permanent
choice, not a placeholder; the retry + sanitize safety net in `call_judge()`
(Phase 2.5/4a) is what makes it reliable enough to ship.

## Phase 2.5 — Enrich judge output (before Phase 3)

The UI in Phase 3 is built against this schema, so the schema has to be
right first.

- [x] Expand the judge schema from a single score to three categories:
      `content_substance`, `composure_under_pressure`,
      `audience_responsiveness` (each `{score: 0-100, note}`).
- [x] Add `trigger` and `recovery_pattern` to each interruption, both
      validated against a fixed enum (not free text):
      `trigger`: `vague_claim | unsupported_number | hesitation | ignored_question`
      `recovery_pattern`: `answered_directly | deflected | repeated_claim | asked_clarifying_question`
- [x] Add `better_response_example` per interruption — one sentence, must
      reference something specific from that actual conversation (a claim,
      a number). The prompt explicitly bans generic advice
      ("be more specific" and the like).
- [x] Compute timing signals per interruption from the timeline's existing
      turn-level timestamps (no word-level timestamps exist in the
      timeline) — `response_latency_ms` (gap between the interruption
      ending and the user speaking again) and `words_per_second` before vs.
      after the interruption — and hand them to the judge as raw context;
      the judge interprets them, we don't.
- [x] Run AssemblyAI's sentiment analysis (`sentiment_analysis: true` on
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
`qwen3.5-4b-32k-fast` (the permanent JUDGE_MODEL, see Phase 2) would not
reliably follow the "don't invent numbers" instruction on its own, even
after strengthening the prompt with contrastive examples, and occasionally
returned malformed JSON. A judge output safety net was added so this holds
regardless of which model is behind it: `call_judge()` now retries up to 3
times on invalid JSON/schema, and separately retries once (then falls back
to a regex-based `sanitize_response_example()`) if `better_response_example`
cites a number that never appeared in the transcript.

## Phase 3 — UI (Days 10–13)
- [ ] Extend the starter's browser client with a post-call results view:
      score, per-interruption breakdown, suggestions.
- [ ] Keep styling minimal — clarity over polish at this stage.

## Phase 4 — Hardening (Days 14–17)
- [ ] Run the objectivity checks in `TESTING.md` across a small transcript
      set; fix judge-prompt drift or inconsistency.
- [ ] Handle edge cases:
  - [x] Very short calls — `tests/fixtures/near_empty_session.json` (~16s).
  - [x] No interruptions triggered — `tests/fixtures/no_interruption_session.json`;
        `run_judge_pass` doesn't crash on an empty `interruptions` list, and
        the UI shows a dedicated (non-generic, score-agnostic) message
        instead of an empty-looking panel.
  - [x] User goes silent mid-sentence — `tests/fixtures/hesitation_session.json`.
        Found the timing-based barge-in signal misses these entirely (the
        agent starts *after* `user_speech_ended_at_ms` on a silence-timeout
        cutoff, same range as an un-interrupted reply); added a text-based
        `hesitation_cutoff` signal alongside the timing-based `barge_in`
        one, and a deterministic type-to-trigger mapping in the judge
        prompt so `hesitation_cutoff` always maps to `trigger: "hesitation"`
        instead of the judge guessing from content.
  - [x] Connection drop mid-call — `server.py`'s `_send()` and the new
        `Server.handle_error()` log a clean line instead of a traceback
        when a client (browser tab) disconnects mid-request, and the
        server keeps serving other requests. Verified with a controlled
        test (`judge.run_judge_pass` stubbed with a delay, request aborted
        mid-flight), not a live tab close — see the session notes.
- [ ] Re-test the full flow start to finish, cold (as a first-time user
      would experience it).
- [ ] Known limitation (logged in judge_eval_log.md): judge occasionally
      under-reports a real, marked interruption (asymmetric to the fixed
      over-reporting case). Not tied to a model swap — `qwen3.5-4b-32k-fast`
      is the permanent JUDGE_MODEL now (see Phase 2 status). A separate
      improvement opportunity to pick up if it matters: tighten the prompt
      further, or add a symmetric "missing interruption" safety net (the
      same retry-then-fix pattern as the over-reporting fix, mirrored).

## Phase 4b — Multi-persona support (stretch goal)

Four counterparts to practice against instead of one: Marcus (Investor,
existing) plus three new ones, each its own `agents/*.jsonc` with its own
persona and turn-detection tuning.

- [x] `agents/technical-cofounder.jsonc` — Priya, a skeptical technical
      co-founder candidate. Probes mechanism-level depth ("what breaks at
      10x", "is that actually automatic"), not domain trivia. Less
      aggressive turn-detection than Marcus (`min_silence: 400` vs `300`)
      — this persona is testing depth, not speed.
- [x] `agents/non-technical-buyer.jsonc` — Grace, a non-technical enterprise
      buyer who gets lost in jargon. Interrupts rarely, only out of real
      confusion ("I still don't understand that"). Highest `min_silence`
      of the four (`550`) — patience is the point.
- [x] `agents/impatient-buyer.jsonc` — Derek, an impatient enterprise buyer
      (a customer, not an investor) focused on concrete ROI/cost/timeline.
      About as aggressive as Marcus (`min_silence: 320`).
- [x] All three published; `.env` has `AGENT_ID_TECHNICAL_COFOUNDER`,
      `AGENT_ID_NON_TECHNICAL_BUYER`, `AGENT_ID_IMPATIENT_BUYER`.
- [x] Persona selection moved client-side: `server.py` resolves all four at
      startup into a `PERSONAS` registry and embeds them as
      `window.PERSONAS`; the browser picks which `agent_id` to send in the
      websocket's `session.update` message. (The `/token` endpoint needed
      no change — it was already agent-agnostic; agent selection has
      always happened over the websocket, not at token-mint time. The
      starter's single-agent `AGENT=<name>` env var still works as a
      legacy override for testing any other `agents/*.jsonc` file.)
      `index.html` gets a persona `<select>` before "Start call",
      defaulting to Marcus; disabled during an active call like the mic
      picker. The sidebar's read-only "Agent" tab now takes `/agent?key=`
      and refetches on persona change.
- [x] Judge prompt generalized for multi-persona: `run_judge_pass` extracts
      a `counterpart_description` from the session's own
      `config.system_prompt` (its first sentence — every persona's prompt
      opens with "You are `<Name>`, a `<role>`...", so no separate registry
      to keep in sync) and passes it to the judge as context.
      `JUDGE_SYSTEM_PROMPT`'s "investor"-specific language is now
      "counterpart," and `audience_responsiveness` is explicitly judged
      against what *that* counterpart cares about (mechanism depth for
      Priya, plain language for Grace, ROI/cost/timeline for Derek) instead
      of investor-pitch assumptions. Trigger enum unchanged — already
      general enough.
- [x] Manually test at least one call with each of the three new personas;
      confirm the interruption tuning feels intentional (see PLAN.md's
      "quick manual test after every agents/*.jsonc change" rule) and that
      judge feedback tracks the right thing for that counterpart.
      Priya: done, twice (`priya_test_session.json`,
      `priya_test_session_2.json`). Grace: done
      (`grace_test_session.json`) — escalating jargon-confusion turns,
      judge notes correctly framed around plain language. Derek: done
      (`derek_test_session.json`) — escalating impatience over a missing
      cost/time number, judge notes correctly framed around ROI/cost. Both
      also confirmed the `composure_under_pressure` fix (see below) holds
      outside Priya's technical-cofounder persona: zero interruptions in
      both, yet all three category scores came out low and consistent
      (Grace 5/10/5, Derek 10/20/10) instead of the old high-composure
      false positive.
- [x] Known limitation, now closed: `is_interruption`/`interruption_type`
      only catches `barge_in` (talks over the user) and `hesitation_cutoff`
      (user trails off). A third real pattern showed up testing Priya's
      "interrupt on the first unsupported claim" fix: the agent replying
      promptly (~1-1.6s, ample processing latency, not overlap) and
      pointedly to a claim the user *just finished* a complete sentence on.
      That's not a barge-in or a hesitation cutoff by our definitions, so
      it's invisible to `parse_timeline` — `interruptions: []` even when the
      persona is doing exactly what its prompt asks. Confirmed by comparing
      `priya_test_session.json` (4 unsupported claims piled up before one
      reply) against `priya_test_session_2.json` (reply after each single
      claim, same low reply latency in both). Rather than adding a fourth
      `interruption_type` to detect this at the timeline layer, closed it
      one level up: `JUDGE_SYSTEM_PROMPT` now tells the judge that an empty
      `interruptions` list doesn't mean the founder faced no pressure —
      `composure_under_pressure` should instead track how sharp/specific
      the counterpart's questions were and how concrete vs. superficial the
      founder's answers were, interruption or not. Verified on
      `priya_test_session_2.json`: `composure_under_pressure` went from an
      inconsistent 80 (alongside `content_substance: 10`,
      `audience_responsiveness: 20`) to 30, now consistent with the other
      two categories (20/15 on re-run). No new `interruption_type` needed.

## Backlog (not blocking Phase 5)
- Call-closing behavior: none of the four `agents/*.jsonc` prompts say
  anything about how the persona should behave when time/the call is
  ending (e.g. wrapping up, a closing line). Identified during the
  system-prompt audit (2026-09-16) alongside the claim-piling and
  pushback gaps (both since fixed) — deprioritized because a demo/judge
  call is short and scripted enough that this edge is unlikely to come
  up in practice.

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
