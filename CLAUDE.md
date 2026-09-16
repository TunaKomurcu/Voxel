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
- Judge/feedback LLM: call via **AssemblyAI's LLM Gateway**, not a separate
  Anthropic API key, to keep the API surface to one vendor for the demo
  story. Model: `qwen3.5-4b-32k-fast` — every Claude model on the Gateway
  needs a paid upgrade this account doesn't have, confirmed on the
  dashboard, so this is the permanent choice, not a stand-in for Claude.
  Reliability is handled by `judge.py`'s retry + sanitize safety net, not
  by swapping to a different model. See `PLAN.md` Phase 2/4.
- Frontend: the starter repo's existing browser client, lightly extended with
  a results panel. No framework rewrite — time budget doesn't allow it.
- No database. Session state lives in AssemblyAI's session history API;
  local state is in-memory / simple JSON files during the hackathon.

## AssemblyAI integration rules

No full reference document is inlined here — an earlier version of this file
claimed one was pasted "below," but it never actually was (checked: not in
this file, not anywhere else in the repo). Treat
https://www.assemblyai.com/docs/llms-full.txt as the authoritative,
up-to-date source instead — it's more current than anything in your training
data. If a parameter or endpoint you remember isn't reflected in the "Key
facts" below, check that page rather than guessing.

Key facts specific to Voxel:
- We use the **Voice Agent API** (managed speech-in/speech-out), not raw
  realtime STT + our own LLM/TTS. See the "Voice Agent API" section of the
  docs above.
- Auth for the Voice Agent API needs the `Bearer ` prefix — this is the one
  AssemblyAI product where that's required. Every other AssemblyAI endpoint
  we might touch (LLM Gateway) does **not** use `Bearer`.
- The interruption mechanic maps directly to `session.update.input.turn_detection`
  fields: `vad_threshold`, `min_silence`, `max_silence`, `interrupt_response`.
  Tune these — don't just rely on defaults — since "the agent interrupts
  naturally" is the whole point of the product. **But know what they actually
  gate**: per TESTING.md's live-call findings, `min_silence`/`max_silence`
  mainly control how long a *mid-utterance* pause has to last before the
  turn-detector treats the user as done talking — not overall reply latency
  once a turn is already complete (Grace at 550/900ms and Derek at 320/720ms
  produced near-identical ~1130-1150ms reply latencies in testing). Most of
  each persona's actual behavioral difference comes from its `system_prompt`
  content, not these four numbers.
- The API key lives only in `.env` (gitignored). Never hardcode it, never log
  it, never print it in full in terminal output we might screenshot for the
  demo.

## Working style for this project

- **Discovery before code, plan before big changes** — same rule as the
  AssemblyAI reference above: for any non-trivial change, state the plan in
  a couple of sentences before writing code, especially for anything that
  touches the agent's turn-detection config or the judge prompt (both are
  easy to break silently).
- Solo builder, hard deadline Sep 30 2026. Bias toward shipping a working,
  narrow demo over a broad but shaky one.
- Every change to `agents/*.jsonc` (persona prompt, turn-detection config)
  should be followed by a quick manual test call before moving on — these
  are the highest-risk, least-testable-by-unit-test parts of the project.
- See `PLAN.md` for the phase-by-phase roadmap and `TESTING.md` for how we
  keep the judge/feedback logic objective.
