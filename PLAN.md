# Voxel — Build Plan

Timeframe: Sep 10 – Sep 30, 2026 (20 days, solo).
Submission target: lablab.ai, AssemblyAI Voice Agent Hackathon.

## Phase 0 — Setup (Day 1)
- [ ] Clone `voice-agent-starter-python`, add `.env` with `ASSEMBLYAI_API_KEY`.
- [ ] `python publish.py` with the default `minimal` agent, confirm browser
      call works end-to-end (this just proves the pipeline, not the product).
- [ ] Commit `CLAUDE.md`, `PLAN.md`, `TESTING.md` to the repo before writing
      any product code.

## Phase 1 — Persona & interruption mechanic (Days 2–4)
- [ ] Copy `agents/turn-taking.jsonc` → `agents/voxel-investor.jsonc`.
- [ ] Write the system prompt: skeptical/impatient investor persona,
      explicit instruction to interrupt on vague claims, hesitation, or
      unsupported numbers.
- [ ] Tune `turn_detection` (`vad_threshold`, `min_silence`, `max_silence`,
      `interrupt_response`) until interruptions feel intentional, not random
      or glitchy.
- [ ] Manually test with at least 3 different pitch styles (confident,
      rambling, numbers-heavy) and note what breaks.

**Exit criteria:** a human (you) can have a 2-minute pitch conversation with
the agent and it interrupts at least once in a way that feels deliberate.

## Phase 2 — Session retrieval + judge pass (Days 5–9)
- [ ] After a session ends, fetch `GET /v1/sessions/{id}` and parse the
      `timeline` artifact (`user_transcript` / `agent_text` pairs).
- [ ] Design the judge prompt: given the timeline, identify each
      interruption point, whether the user recovered well, and output
      structured JSON (score 0–100, 2–3 concrete suggestions).
- [ ] Call the judge prompt via AssemblyAI's LLM Gateway.
- [ ] Validate the JSON output against a fixed schema before displaying it
      (see `TESTING.md` — this is the first "objective" checkpoint).

**Exit criteria:** feeding a real session timeline through the judge
reliably produces valid, parseable JSON with a score and specific feedback
(not generic praise).

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
