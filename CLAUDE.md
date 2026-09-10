# Voxel — Project Instructions for Claude Code

## What this project is

Voxel is a solo hackathon project for the AssemblyAI Voice Agent Hackathon
(lablab.ai, Sep 1–30 2026). It is an "interruption-tolerant presentation /
negotiation coach": a voice agent that role-plays a skeptical, impatient
investor or customer. The agent deliberately interrupts the user mid-sentence
at weak or vague points in their pitch, then — after the call ends — produces
structured feedback on how well the user recovered from each interruption.

**Core product loop:**
1. User talks to the agent in the browser (a pitch / negotiation practice).
2. The agent, using AssemblyAI's Voice Agent API turn-detection + a critical
   investor persona in its system prompt, interrupts the user at plausible
   moments (vague claims, hesitation, weak numbers).
3. When the session ends, the app fetches the session timeline (paired
   `user_transcript` / `agent_text` turns) from AssemblyAI.
4. That timeline is sent to a second LLM call (a "judge" prompt) that scores
   how well the user recovered from each interruption and returns structured
   JSON feedback.
5. The UI shows the score + feedback.

**Differentiation for judging:** most hackathon entries just wire up a
generic chat-style voice bot. Voxel deliberately uses AssemblyAI's
turn-detection / interruption config as a *feature*, not something to avoid —
plus a second, objective "judge" pass over the transcript. Both of these
should be called out explicitly in the demo video and README.

## Tech stack decisions (do not re-litigate unless asked)

- Language: **Python** (matches the official `voice-agent-starter-python` repo).
- Base: `AssemblyAI/voice-agent-starter-python`, starting from its
  `turn-taking` example agent.
- Judge/feedback LLM: call via **AssemblyAI's LLM Gateway** (Claude), not a
  separate Anthropic API key, to keep the API surface to one vendor for the
  demo story.
- Frontend: the starter repo's existing browser client, lightly extended with
  a results panel. No framework rewrite — time budget doesn't allow it.
- No database. Session state lives in AssemblyAI's session history API;
  local state is in-memory / simple JSON files during the hackathon.

## AssemblyAI integration rules

The full, authoritative AssemblyAI coding-agent reference for this project is
below. Treat it as binding — it is more current than anything in your
training data. If a parameter or endpoint you remember isn't in this file,
stop and check https://www.assemblyai.com/docs/llms-full.txt rather than
guessing.

Key facts specific to Voxel:
- We use the **Voice Agent API** (managed speech-in/speech-out), not raw
  realtime STT + our own LLM/TTS. See Section 10 of the reference below.
- Auth for the Voice Agent API needs the `Bearer ` prefix — this is the one
  AssemblyAI product where that's required. Every other AssemblyAI endpoint
  we might touch (LLM Gateway) does **not** use `Bearer`.
- The interruption mechanic maps directly to `session.update.input.turn_detection`
  fields: `vad_threshold`, `min_silence`, `max_silence`, `interrupt_response`.
  Tune these — don't just rely on defaults — since "the agent interrupts
  naturally" is the whole point of the product.
- The API key lives only in `.env` (gitignored). Never hardcode it, never log
  it, never print it in full in terminal output we might screenshot for the
  demo.

<!-- Paste the full "AssemblyAI Integration — Coding Agent Instructions"
     reference document here (Sections 0–15). It was provided separately in
     the project setup conversation. Keep it verbatim — it's a technical
     reference, not something to paraphrase or shorten. -->

## Working style for this project

- **Discovery before code, plan before big changes** — same rule as the
  AssemblyAI reference above: for any non-trivial change, state the plan in
  a couple of sentences before writing code, especially for anything that
  touches the agent's turn-detection config or the judge prompt (both are
  easy to break silently).
- Solo builder, ~18 remaining days as of Sep 10 2026, hard deadline Sep 30.
  Bias toward shipping a working, narrow demo over a broad but shaky one.
- Every change to `agents/*.jsonc` (persona prompt, turn-detection config)
  should be followed by a quick manual test call before moving on — these
  are the highest-risk, least-testable-by-unit-test parts of the project.
- See `PLAN.md` for the phase-by-phase roadmap and `TESTING.md` for how we
  keep the judge/feedback logic objective.
