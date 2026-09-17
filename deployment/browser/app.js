// The page's client. Identical to the one in the JS starter: the
// audio worklets, the websocket session, and the transcript and event panes.
const $ = (id) => document.getElementById(id)
// Restarts a CSS `animation` on an element that already has the class —
// a plain classList.add() is a no-op if the class (and so the animation)
// is already applied, so the class has to actually come off first.
function replayAnimation(el, cls) {
  el.classList.remove(cls)
  void el.offsetWidth
  el.classList.add(cls)
}
// The rate the API speaks. Both worklets resample, since a browser may
// ignore the rate an AudioContext asks for.
const WIRE_RATE = 24_000
const PERSONAS = window.PERSONAS
let selectedPersona = PERSONAS[0]

// Short "what to expect" hints, shown as tooltips on the persona picker.
// Keyed by server.py's PERSONAS[].key.
const PERSONA_HINTS = {
  investor: 'Fast, aggressive — demands hard numbers.',
  technical: 'Probes technical depth and mechanism.',
  buyer: 'No jargon — wants plain-English clarity.',
  enterprise: 'Impatient — wants concrete ROI, not vision.',
  surprise: 'Random persona — revealed once the call starts.',
}

// Scratch buffers are reused: allocating on the audio thread causes glitches.
const CAPTURE_WORKLET = `
  class CaptureProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this._ratio = sampleRate / ${WIRE_RATE};
      this._pos = 0;
      this._prev = 0;
      this._src = null;
      this._out = null;
    }
    _toPcm(samples, len) {
      const pcm = new Int16Array(len);
      for (let i = 0; i < len; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      return pcm;
    }
    process(inputs) {
      const ch = inputs[0]?.[0];
      if (!ch) return true;
      if (this._ratio === 1) {
        const pcm = this._toPcm(ch, ch.length);
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        return true;
      }
      const n = ch.length;
      if (!this._src || this._src.length < n + 1) {
        this._src = new Float32Array(n + 1);
        this._out = new Float32Array(Math.ceil((n + 1) / this._ratio) + 2);
      }
      const src = this._src;
      const out = this._out;
      src[0] = this._prev;
      src.set(ch, 1);
      let outLen = 0;
      let pos = this._pos;
      while (pos < n) {
        const i = Math.floor(pos);
        const frac = pos - i;
        out[outLen++] = src[i] + (src[i + 1] - src[i]) * frac;
        pos += this._ratio;
      }
      this._pos = pos - n;
      this._prev = ch[n - 1];
      if (outLen) {
        const pcm = this._toPcm(out, outLen);
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
      }
      return true;
    }
  }
  registerProcessor('capture', CaptureProcessor);
`

// A ring buffer rather than one AudioBufferSource per chunk, which drifts and
// clicks under jitter. Posting 'stop' empties it for barge-in.
const PLAYBACK_WORKLET = `
  class PlaybackProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this._ring = new Float32Array(sampleRate * 30);
      this._writePos = 0;
      this._readPos = 0;
      this._available = 0;
      this._step = ${WIRE_RATE} / sampleRate;
      this._rsPos = 0;
      this._rsPrev = 0;
      // After a gap the speaker sits at zero, so interpolating from the
      // pre-gap _rsPrev would click. Reset it instead.
      this._drained = false;
      this.port.onmessage = (e) => {
        if (e.data === 'stop') {
          this._writePos = this._readPos = this._available = 0;
          this._rsPos = this._rsPrev = 0;
          return;
        }
        const int16 = new Int16Array(e.data);
        // int16[-1] would make _rsPrev NaN, silencing the ring for good.
        if (!int16.length) return;
        if (this._drained) {
          this._rsPrev = 0;
          this._rsPos = 0;
          this._drained = false;
        }
        if (this._step === 1) {
          for (let i = 0; i < int16.length; i++) this._push(int16[i] / 32768);
          return;
        }
        const n = int16.length;
        let pos = this._rsPos;
        while (pos < n) {
          const i = Math.floor(pos);
          const frac = pos - i;
          const a = i === 0 ? this._rsPrev : int16[i - 1] / 32768;
          const b = int16[i] / 32768;
          this._push(a + (b - a) * frac);
          pos += this._step;
        }
        this._rsPos = pos - n;
        this._rsPrev = int16[n - 1] / 32768;
      };
    }
    _push(v) {
      if (this._available < this._ring.length) {
        this._ring[this._writePos] = v;
        this._writePos = (this._writePos + 1) % this._ring.length;
        this._available++;
      }
    }
    process(inputs, outputs) {
      const output = outputs[0];
      const out = output[0];
      const cap = this._ring.length;
      for (let i = 0; i < out.length; i++) {
        if (this._available > 0) {
          out[i] = this._ring[this._readPos];
          this._readPos = (this._readPos + 1) % cap;
          this._available--;
        } else {
          out[i] = 0;
          this._drained = true;
        }
      }
      // Mono source, stereo sink.
      for (let ch = 1; ch < output.length; ch++) output[ch].set(out);
      return true;
    }
  }
  registerProcessor('playback', PlaybackProcessor);
`

const blobUrl = (code) =>
  URL.createObjectURL(new Blob([code], { type: 'application/javascript' }))

let ws, captureCtx, playbackCtx, playback, mic, callStart, timer, sessionId

// --- microphones ---
// Labels stay empty until mic permission is granted, so this runs again after
// getUserMedia.
async function listMics() {
  if (!navigator.mediaDevices?.enumerateDevices) return
  const devices = await navigator.mediaDevices.enumerateDevices()
  const inputs = devices
    .filter((device) => device.kind === 'audioinput')
    // Chrome's synthetic entries alias a real device and duplicate it.
    .filter((device) => device.deviceId !== 'default' && device.deviceId !== 'communications')
  const select = $('mic')
  const chosen = select.value
  select.replaceChildren()
  const auto = document.createElement('option')
  auto.value = ''
  auto.textContent = 'Default microphone'
  select.append(auto)
  inputs.forEach((device, i) => {
    const option = document.createElement('option')
    option.value = device.deviceId
    option.textContent = device.label || `Microphone ${i + 1}`
    select.append(option)
  })
  if (chosen && inputs.some((device) => device.deviceId === chosen)) select.value = chosen
}
listMics()
navigator.mediaDevices?.addEventListener?.('devicechange', listMics)

// --- persona selection ---
function listPersonas() {
  const select = $('persona')
  select.replaceChildren()
  PERSONAS.forEach((p) => {
    const option = document.createElement('option')
    option.value = p.key
    option.textContent = p.label || p.name
    select.append(option)
  })
  const surprise = document.createElement('option')
  surprise.value = 'surprise'
  surprise.textContent = '\u{1F3B2} Surprise me'
  select.append(surprise)
  select.value = selectedPersona.key
  // Always-visible, not a hover tooltip — mirrors whatever value is
  // actually showing (select.value), never the resolved persona, so a
  // "Surprise me" pick doesn't leak through a description the dropdown
  // itself doesn't reveal.
  $('persona-hint').textContent = PERSONA_HINTS[select.value] || ''
}
listPersonas()

function pickSurprisePersona() {
  // Excludes the current pick so back-to-back rolls don't (as often) land
  // on the same persona — with only one real persona configured (legacy
  // AGENT=<name> mode) there's nothing to exclude, so fall back to the
  // full list.
  const candidates = PERSONAS.filter((p) => p.key !== selectedPersona?.key)
  const pool = candidates.length ? candidates : PERSONAS
  return pool[Math.floor(Math.random() * pool.length)]
}

$('persona').onchange = () => {
  const value = $('persona').value
  if (value === 'surprise') {
    // Deliberately does NOT snap the dropdown to the chosen name — it
    // stays on "Surprise me" so the identity isn't revealed there. The
    // only reveal is the transcript label once the agent actually speaks.
    // Each call gets its own fresh roll — see start().
    selectedPersona = pickSurprisePersona()
  } else {
    selectedPersona = PERSONAS.find((p) => p.key === value) || PERSONAS[0]
  }
  // Mirrors `value` (what's visibly selected), not selectedPersona — see
  // the same note in listPersonas().
  $('persona-hint').textContent = PERSONA_HINTS[value] || ''
  replayAnimation($('persona-hint'), 'flash')
  // Refresh the sidebar's read-only agent view if it's the one showing.
  if (!$('agent-body').hidden) loadAgentTab()
}

$('btn').onclick = () => (ws?.readyState <= 1 ? stop() : start())
$('log-toggle').onclick = () => {
  const hidden = document.body.classList.toggle('no-side')
  $('log-toggle-label').textContent = hidden ? 'Show events' : 'Hide events'
}

// Purely visual: flips data-theme + persists it, independent of the
// system-preference media query. See the FOUC-prevention script in
// index.html's <head> for how a saved choice survives a reload without
// flashing the wrong theme first — this only ever runs after a click.
const THEME_KEY = 'voxel-theme'
function currentTheme() {
  const explicit = document.documentElement.getAttribute('data-theme')
  if (explicit === 'light' || explicit === 'dark') return explicit
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}
$('theme-toggle').onclick = () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', next)
  try { localStorage.setItem(THEME_KEY, next) } catch (e) {}
  // Chart.js bakes colors in at creation time (see cssVar() below) — a
  // live theme flip needs an explicit re-render to pick up the new ones.
  if (progressChart) renderProgress()
}

// --- side pane tabs ---
// Purely visual: dims punctuation/structure and picks out keys, strings,
// and literals so the raw agent JSON reads lighter without hiding any of
// it. Escapes first so nothing in the agent's own data can inject markup.
function highlightJSON(json) {
  const escaped = json
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'jn'
      if (/^"/.test(match)) cls = /:$/.test(match) ? 'jk' : 'js'
      return `<span class="${cls}">${match}</span>`
    }
  )
}

function loadAgentTab() {
  $('agent-body').replaceChildren()
  if ($('persona').value === 'surprise') {
    // Don't leak who it picked through this side channel — same reveal
    // rule as the dropdown itself.
    const hidden = document.createElement('div')
    hidden.className = 'empty'
    hidden.textContent = "Hidden — you picked Surprise me. It'll reveal itself once the call starts."
    $('agent-body').append(hidden)
    return
  }
  const loading = document.createElement('div')
  loading.className = 'empty'
  loading.textContent = 'Loading the published agent.'
  $('agent-body').append(loading)
  fetch('/agent?key=' + encodeURIComponent(selectedPersona.key))
    .then((res) => res.json())
    .then((agent) => {
      $('agent-body').replaceChildren()
      // system_prompt is a multi-line string, so JSON.stringify-ing it
      // inline would show literal \n escapes instead of real line breaks.
      // Render it separately, as plain text, so it reads the way it was
      // written.
      const { system_prompt, ...rest } = agent
      const pre = document.createElement('pre')
      pre.className = 'json'
      pre.innerHTML = highlightJSON(JSON.stringify(rest, null, 2))
      $('agent-body').append(pre)
      if (system_prompt) {
        const label = document.createElement('div')
        label.className = 'empty'
        label.style.margin = '16px 0 4px'
        label.textContent = 'system_prompt:'
        const promptPre = document.createElement('pre')
        promptPre.textContent = system_prompt
        $('agent-body').append(label, promptPre)
      }
    })
    .catch(() => {
      $('agent-body').textContent = 'Could not load the agent.'
    })
}

function showTab(name) {
  for (const tab of ['events', 'agent']) {
    $('tab-' + tab).classList.toggle('on', tab === name)
    $(tab + '-body').hidden = tab !== name
  }
  if (name === 'agent') loadAgentTab()
}
$('tab-events').onclick = () => showTab('events')
$('tab-agent').onclick = () => showTab('agent')

async function addWorklet(ctx, code, name) {
  const url = blobUrl(code)
  try {
    await ctx.audioWorklet.addModule(url)
  } finally {
    URL.revokeObjectURL(url)
  }
  return new AudioWorkletNode(ctx, name)
}

async function start() {
  // Fresh roll per call so leaving the picker on "Surprise me" across
  // multiple calls doesn't quietly reuse the same hidden persona.
  if ($('persona').value === 'surprise') selectedPersona = pickSurprisePersona()

  $('btn').disabled = true
  $('mic').disabled = true
  $('persona').disabled = true
  setStatus('connecting')
  hideResults()

  try {
    // The API key never reaches the page; this token expires in 60 seconds.
    const res = await fetch('/token')
    if (!res.ok) {
      setStatus('error', 'could not mint a token, check the API key')
      reset()
      return
    }
    const { token, ws_base } = await res.json()

    // Two contexts, created in the click handler so Safari starts them.
    captureCtx = new AudioContext({ sampleRate: WIRE_RATE })
    playbackCtx = new AudioContext({ sampleRate: WIRE_RATE })
    await Promise.all([captureCtx.resume(), playbackCtx.resume()])

    playback = await addWorklet(playbackCtx, PLAYBACK_WORKLET, 'playback')
    playback.connect(playbackCtx.destination)

    const deviceId = $('mic').value
    mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        // A preference, not `exact`: an unplugged device falls back.
        ...(deviceId ? { deviceId } : {}),
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: false,
      },
    })
    listMics()
    const capture = await addWorklet(captureCtx, CAPTURE_WORKLET, 'capture')
    captureCtx.createMediaStreamSource(mic).connect(capture)

    const url = new URL((ws_base || 'wss://agents.assemblyai.com/v1') + '/ws')
    url.searchParams.set('token', token)
    ws = new WebSocket(url)
    let ready = false

    // The API takes base64 inside JSON, not binary frames.
    capture.port.onmessage = ({ data }) => {
      if (!ready || ws.readyState !== 1) return
      const bytes = new Uint8Array(data)
      let binary = ''
      for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
      }
      ws.send(JSON.stringify({ type: 'input.audio', audio: btoa(binary) }))
      logEvent('up', 'input.audio')
    }

    // Everything about the agent lives server-side; the session just names it.
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'session.update', session: { agent_id: selectedPersona.id } }))
      logEvent('up', 'session.update', selectedPersona.id)
    }

    ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data)
      switch (msg.type) {
        case 'session.ready':
          ready = true
          sessionId = msg.session_id
          callStart = Date.now()
          timer = setInterval(tick, 1000)
          tick()
          setStatus('listening')
          $('btn').disabled = false
          $('btn-label').textContent = 'End call'
          $('btn').classList.add('live')
          logEvent('down', msg.type, msg.session_id)
          break

        case 'input.speech.started':
          // Barge-in: empty the ring buffer so the agent stops mid-word.
          playback?.port.postMessage('stop')
          setStatus('listening')
          setWaveform('user')
          logEvent('down', msg.type)
          break

        case 'reply.started':
          setStatus('speaking')
          setWaveform('agent')
          logEvent('down', msg.type)
          break

        case 'reply.audio': {
          const raw = atob(msg.data)
          const bytes = new Uint8Array(raw.length)
          for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
          playback?.port.postMessage(bytes.buffer, [bytes.buffer])
          logEvent('down', msg.type)
          break
        }

        case 'reply.done':
          setStatus('listening')
          setWaveform(null)
          if (msg.status === 'interrupted') playback?.port.postMessage('stop')
          logEvent('down', msg.type, msg.status)
          break

        // text is the full transcript so far, so it replaces.
        case 'transcript.user.delta':
          partial('you', msg.text)
          logEvent('down', msg.type, msg.text)
          break

        // delta is the next word only, so it appends.
        case 'transcript.agent.delta':
          logEvent('down', msg.type, msg.delta)
          if (msg.reply_id && msg.reply_id === printedReply) break
          if (msg.reply_id !== liveReply) {
            liveReply = msg.reply_id
            dropPartial('agent')
          }
          partial('agent', appendDelta(partialText.agent || '', msg.delta))
          break

        case 'transcript.user':
          addLine('you', msg.text)
          logEvent('down', msg.type, msg.text)
          break

        case 'transcript.agent':
          printedReply = msg.reply_id ?? printedReply
          addLine('agent', msg.text)
          logEvent('down', msg.type, msg.text)
          break

        case 'tool.call': {
          // http tools run on AssemblyAI's side; no result comes back here.
          const args = JSON.stringify(msg.arguments ?? {})
          addLine('tool', `${msg.name}(${args})`)
          logEvent('down', msg.type, `${msg.name} ${args}`)
          break
        }

        case 'session.ended':
          logEvent('down', msg.type)
          ws.close()
          // Captured now, not read later from selectedPersona: the call is
          // over and the persona picker is unlocked again well before the
          // judge pass (15-45s) finishes, so the user could switch personas
          // while waiting for these results.
          if (sessionId) fetchJudgeResults(sessionId, selectedPersona)
          break

        case 'session.error':
          setStatus('error', msg.message)
          logEvent('down', msg.type, `${msg.code}: ${msg.message}`)
          break

        default:
          logEvent('down', msg.type)
      }
    }

    ws.onclose = () => { setStatus('idle'); reset() }
    ws.onerror = () => { setStatus('error', 'connection failed'); reset() }
  } catch (error) {
    setStatus('error', error.message)
    reset()
  }
}

function stop() {
  // Close cleanly so the session record ends, falling back to the socket.
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({ type: 'session.end' }))
    logEvent('up', 'session.end')
    const socket = ws
    setTimeout(() => { if (socket.readyState === 1) socket.close() }, 3000)
  } else {
    ws?.close()
  }
  playback?.port.postMessage('stop')
  mic?.getTracks().forEach((track) => track.stop())
  captureCtx?.close()
  playbackCtx?.close()
  captureCtx = playbackCtx = playback = mic = null
  reset()
  setStatus('idle')
}

function reset() {
  clearInterval(timer)
  clearPartials()
  open.forEach((run) => paint(run, true))
  open.clear()
  setWaveform(null)
  $('btn').disabled = false
  $('mic').disabled = false
  $('persona').disabled = false
  $('btn-label').textContent = 'Start call'
  $('btn').classList.remove('live')
}

function setStatus(state, detail) {
  $('status').className = 'status ' + state
  $('status-text').textContent = detail || state
}

// Purely a visual cue for which side of the conversation is active right
// now — driven by websocket event boundaries, not real audio levels.
function setWaveform(state) {
  const cls = state ? ' ' + state : ''
  $('waveform').className = 'waveform' + cls
  $('orb').className = 'orb' + cls
}

// Purely a visual click-ripple on the call button — positions the CSS
// ripple at the exact click point via custom properties. Runs alongside
// $('btn').onclick above, no effect on the actual start/stop logic.
$('btn').addEventListener('click', e => {
  const el = e.currentTarget
  const rect = el.getBoundingClientRect()
  el.style.setProperty('--ripple-x', (e.clientX - rect.left) + 'px')
  el.style.setProperty('--ripple-y', (e.clientY - rect.top) + 'px')
  el.classList.remove('rippling')
  void el.offsetWidth
  el.classList.add('rippling')
})

// $4.50 an hour, the list price at assemblyai.com/pricing. Billing is per
// session minute, so the running figure is an estimate, not an invoice.
const COST_PER_SECOND = 4.5 / 3600

function tick() {
  const seconds = Math.floor((Date.now() - callStart) / 1000)
  $('elapsed').textContent =
    Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0')
  $('cost').textContent = '$' + (seconds * COST_PER_SECOND).toFixed(3)
}

// --- transcript ---
const partialText = {}
const partialEl = {}
// The full reply arrives once its audio has been sent, which beats the audio
// playing out, so deltas keep coming after the line is printed. printedReply
// stops them rebuilding the same sentence underneath it.
let liveReply = null
let printedReply = null

// Deltas arrive with a leading space sometimes and without it other times, so
// add one only when neither side has one and the delta is not punctuation.
const ATTACHES_LEFT = /^[.,!?;:%°)\]}…'"’”]/
const NO_SPACE_AFTER = /[([{$\-\/'"‘“]$/

function appendDelta(text, delta) {
  if (!delta) return text
  if (!text) return delta
  if (/^\s/.test(delta) || /\s$/.test(text)) return text + delta
  if (ATTACHES_LEFT.test(delta) || NO_SPACE_AFTER.test(text)) return text + delta
  return text + ' ' + delta
}

function dropPartial(who) {
  partialEl[who]?.remove()
  delete partialEl[who]
  delete partialText[who]
}

function transcriptLine(who, text, cls) {
  const line = document.createElement('div')
  line.className = 'line ' + who + (cls ? ' ' + cls : '')
  const label = document.createElement('span')
  label.className = 'who'
  label.textContent = who === 'agent' ? (selectedPersona.short_name || selectedPersona.name) : who
  const body = document.createElement('span')
  body.className = 'said'
  body.textContent = text
  line.append(label, body)
  return line
}

function clearEmpty(el) {
  const empty = el.querySelector('.empty')
  if (empty) empty.remove()
}

function scroll(el) {
  el.scrollTop = el.scrollHeight
}

function partial(who, text) {
  clearEmpty($('transcript'))
  partialText[who] = text
  if (partialEl[who]) {
    partialEl[who].querySelector('.said').textContent = text
  } else {
    partialEl[who] = transcriptLine(who, text, 'partial')
    $('transcript').append(partialEl[who])
  }
  scroll($('transcript'))
}

function addLine(who, text) {
  clearEmpty($('transcript'))
  dropPartial(who)
  $('transcript').append(transcriptLine(who, text))
  scroll($('transcript'))
}

function clearPartials() {
  for (const who of Object.keys(partialEl)) dropPartial(who)
  liveReply = printedReply = null
}

// --- event log ---
// Audio frames arrive ~190 times a second each way, so these types hold a row
// open and count into it. Both streams run at once, hence a row per key.
const COALESCE = new Set([
  'input.audio',
  'reply.audio',
  'transcript.user.delta',
  'transcript.agent.delta',
])
const open = new Map()

function eventRow(direction, type, detail) {
  const row = document.createElement('div')
  row.className = 'event ' + direction
  const at = document.createElement('span')
  at.className = 'at'
  at.textContent = (callStart ? (Date.now() - callStart) / 1000 : 0).toFixed(1) + 's'
  const arrow = document.createElement('span')
  arrow.className = 'dir'
  arrow.textContent = direction === 'up' ? '↑' : '↓'
  const name = document.createElement('span')
  name.className = 'type'
  name.textContent = type
  const count = document.createElement('span')
  count.className = 'count'
  const info = document.createElement('span')
  info.className = 'detail'
  if (detail) info.textContent = detail
  row.append(at, arrow, name, count, info)
  return row
}

// Ten repaints a second, plus one when the run closes.
function paint(live, final) {
  const now = performance.now()
  if (!final && now - live.painted < 100) return
  live.painted = now
  live.row.querySelector('.count').textContent = live.count > 1 ? '×' + live.count : ''
  if (live.detail) live.row.querySelector('.detail').textContent = live.detail
}

function logEvent(direction, type, detail) {
  const log = $('events-body')
  clearEmpty(log)
  const key = direction + ' ' + type
  const live = open.get(key)
  if (live) {
    live.count += 1
    if (detail) live.detail = detail
    paint(live)
    return
  }
  // A real event closes the open runs, so the next burst starts a new row.
  if (!COALESCE.has(type)) {
    open.forEach((run) => paint(run, true))
    open.clear()
  }
  // Only follow the tail if the reader is there.
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40
  const row = eventRow(direction, type, detail)
  log.append(row)
  while (log.children.length > 400) log.firstChild.remove()
  if (COALESCE.has(type)) open.set(key, { row, count: 1, detail, painted: 0 })
  if (atBottom) scroll(log)
}

// --- post-call feedback ---
// The judge pass (session retrieval + sentiment + LLM scoring) can take
// 15-45s, most of it the sentiment analysis step. These staged messages are
// just to keep the wait from reading as a hang — there's no real progress
// signal behind them, just a timeline that roughly matches what's happening.
const LOADING_STAGES = [
  [0, 'Reviewing what you said...'],
  [5000, 'Cross-checking tone and sentiment...'],
  [25000, 'Scoring your recovery...'],
]
let loadingTimers = []
let lastSessionId = null
let lastPersona = null
let lastResultsData = null

function startLoadingStages() {
  clearLoadingStages()
  for (const [at, text] of LOADING_STAGES) {
    if (at === 0) $('results-status').textContent = text
    else loadingTimers.push(setTimeout(() => { $('results-status').textContent = text }, at))
  }
}

function clearLoadingStages() {
  loadingTimers.forEach(clearTimeout)
  loadingTimers = []
}

function hideResults() {
  clearLoadingStages()
  $('results').hidden = true
}

function fetchJudgeResults(id, persona) {
  lastSessionId = id
  lastPersona = persona
  $('results').hidden = false
  $('results-body').replaceChildren()
  $('results-error').hidden = true
  $('results-loading').hidden = false
  $('results-copy').hidden = true
  startLoadingStages()
  fetch('/judge?session_id=' + encodeURIComponent(id))
    .then(async (res) => {
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || 'request failed')
      clearLoadingStages()
      $('results-loading').hidden = true
      lastResultsData = data
      renderJudgeResults(data)
      $('results-copy').hidden = false
      saveToHistory(data, persona)
      renderProgress()
    })
    .catch((err) => {
      clearLoadingStages()
      $('results-loading').hidden = true
      $('results-error-message').textContent = err.message || 'Could not generate feedback for this call.'
      $('results-error').hidden = false
    })
}

$('results-retry').onclick = () => { if (lastSessionId) fetchJudgeResults(lastSessionId, lastPersona) }

function formatFeedbackText(data, persona) {
  const lines = []
  const who = persona?.short_name || persona?.name
  lines.push(who ? `Voxel Feedback — ${who}` : 'Voxel Feedback')
  lines.push(`Overall: ${data.overall_score}/100`)
  lines.push('')
  for (const [key, cat] of Object.entries(data.categories)) {
    lines.push(`${key.replace(/_/g, ' ')}: ${cat.score}/100`)
    lines.push(`  ${cat.note}`)
    lines.push('')
  }
  if (data.suggestions.length) {
    lines.push('Suggestions:')
    for (const s of data.suggestions) lines.push(`- ${s}`)
  }
  return lines.join('\n').trim()
}

$('results-copy').onclick = () => {
  if (!lastResultsData) return
  const text = formatFeedbackText(lastResultsData, lastPersona)
  navigator.clipboard.writeText(text).then(() => {
    const label = $('results-copy-label')
    const original = label.textContent
    label.textContent = 'Copied!'
    setTimeout(() => { label.textContent = original }, 1500)
  })
}

const QUALITY_CLASS = { strong: 'quality-good', weak: 'quality-warn', poor: 'quality-bad' }

function tag(text, cls) {
  const el = document.createElement('span')
  el.className = 'tag' + (cls ? ' ' + cls : '')
  el.textContent = text
  return el
}

// Counts a number up from 0 to target over duration ms — purely cosmetic,
// the final textContent is always the real score regardless of timing.
function animateCount(el, target, duration = 700) {
  const start = performance.now()
  function tick(now) {
    const progress = Math.min((now - start) / duration, 1)
    el.textContent = Math.round(target * progress)
    if (progress < 1) requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
}

function renderJudgeResults(data) {
  const root = $('results-body')
  root.replaceChildren()

  const score = document.createElement('div')
  score.className = 'score'
  const num = document.createElement('span')
  num.className = 'score-num'
  num.textContent = '0'
  const label = document.createElement('span')
  label.className = 'score-label'
  label.textContent = '/ 100'
  score.append(num, label)
  root.append(score)
  animateCount(num, data.overall_score)

  const categories = document.createElement('div')
  categories.className = 'categories'
  for (const [key, cat] of Object.entries(data.categories)) {
    const row = document.createElement('div')
    row.className = 'category'
    const catLabel = document.createElement('div')
    catLabel.className = 'category-label'
    catLabel.textContent = key.replace(/_/g, ' ')
    const barWrap = document.createElement('div')
    barWrap.className = 'bar-wrap'
    const bar = document.createElement('div')
    bar.className = 'bar'
    bar.style.width = cat.score + '%'
    barWrap.append(bar)
    const catScore = document.createElement('div')
    catScore.className = 'category-score'
    catScore.textContent = cat.score
    const note = document.createElement('div')
    note.className = 'category-note'
    note.textContent = cat.note
    row.append(catLabel, barWrap, catScore, note)
    categories.append(row)
  }
  root.append(categories)

  const interruptions = document.createElement('div')
  interruptions.className = 'interruptions'
  if (data.interruptions.length === 0) {
    const empty = document.createElement('div')
    empty.className = 'empty'
    // Deliberately not "strong pitch throughout" — no interruptions means
    // the delivery held up, not that the content was flawless (a real test
    // call scored 42/100 here after dodging a follow-up, with zero cut-ins).
    // The category scores above already say how the content itself did.
    empty.textContent = 'No interruptions — you held the floor without a cut-in.'
    interruptions.append(empty)
  }
  for (const item of data.interruptions) {
    const card = document.createElement('div')
    card.className = 'interruption-card'
    const moment = document.createElement('div')
    moment.className = 'interruption-moment'
    moment.textContent = item.moment
    const tags = document.createElement('div')
    tags.className = 'interruption-tags'
    tags.append(
      tag(item.trigger.replace(/_/g, ' ')),
      tag(item.recovery_quality, QUALITY_CLASS[item.recovery_quality]),
      tag(item.recovery_pattern.replace(/_/g, ' ')),
    )
    const note = document.createElement('div')
    note.className = 'interruption-note'
    note.textContent = item.note
    const example = document.createElement('div')
    example.className = 'interruption-example'
    // Verbatim, [X]/[Y] placeholders included when the judge had no real
    // number to point to — see judge.py's sanitize_response_example.
    example.textContent = item.better_response_example
    card.append(moment, tags, note, example)
    interruptions.append(card)
  }
  root.append(interruptions)

  const suggestions = document.createElement('ul')
  suggestions.className = 'suggestions'
  for (const s of data.suggestions) {
    const li = document.createElement('li')
    li.textContent = s
    suggestions.append(li)
  }
  root.append(suggestions)
}

// --- progress history (localStorage, per-browser, never sent anywhere) ---
const HISTORY_KEY = 'voxel_history'
let progressChart = null
// The array a chart click's point index resolves against — always the
// exact list the currently-rendered chart was built from (post-filter),
// never the raw, unfiltered history.
let lastFilteredHistory = []
let openDetailIndex = null

function loadHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]')
    return Array.isArray(raw) ? raw : []
  } catch {
    return []
  }
}

function saveToHistory(data, persona) {
  const history = loadHistory()
  history.push({
    timestamp: new Date().toISOString(),
    persona: persona?.short_name || persona?.name || 'Unknown',
    overall_score: data.overall_score,
    categories: {
      content_substance: data.categories.content_substance.score,
      composure_under_pressure: data.categories.composure_under_pressure.score,
      audience_responsiveness: data.categories.audience_responsiveness.score,
    },
  })
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history))
  } catch (err) {
    // Private browsing, quota exceeded, storage disabled — the call's own
    // feedback still rendered fine, so this is a silent best-effort only.
    console.warn('Could not save progress history:', err)
  }
}

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function personaOptions(history) {
  const seen = []
  for (const entry of history) {
    if (!seen.includes(entry.persona)) seen.push(entry.persona)
  }
  return seen
}

function renderProgress() {
  const history = loadHistory()
  const filterSelect = $('progress-filter')
  const currentFilter = filterSelect.value
  const personas = personaOptions(history)

  filterSelect.replaceChildren()
  const allOption = document.createElement('option')
  allOption.value = ''
  allOption.textContent = 'All personas'
  filterSelect.append(allOption)
  personas.forEach((name) => {
    const option = document.createElement('option')
    option.value = name
    option.textContent = name
    filterSelect.append(option)
  })
  if (personas.includes(currentFilter)) filterSelect.value = currentFilter

  const filtered = filterSelect.value ? history.filter((h) => h.persona === filterSelect.value) : history

  hideProgressDetail()

  if (filtered.length === 0) {
    $('progress-empty').hidden = false
    $('progress-chart').hidden = true
    $('progress-note').hidden = true
    $('progress-best').hidden = true
    if (progressChart) {
      progressChart.destroy()
      progressChart = null
    }
    return
  }

  lastFilteredHistory = filtered
  $('progress-empty').hidden = true
  $('progress-chart').hidden = false
  $('progress-note').hidden = false
  renderPersonalBests(filtered)

  // Fixed locale, not the system's — a demo machine set to a different
  // language shouldn't change what the chart's x-axis reads.
  const labels = filtered.map((h) =>
    new Date(h.timestamp).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
  )
  const scores = filtered.map((h) => h.overall_score)

  if (progressChart) progressChart.destroy()
  const accent = cssVar('--cobolt-500', '#3923c7')
  const textColor = cssVar('--text-muted', '#777673')
  const gridColor = cssVar('--border', '#dad7cb')
  progressChart = new Chart($('progress-chart').getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Overall score',
        data: scores,
        borderColor: accent,
        backgroundColor: accent,
        pointRadius: 4,
        tension: 0.2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'
      },
      onClick: (evt, elements) => {
        if (!elements.length) return
        const index = elements[0].index
        if (openDetailIndex === index) hideProgressDetail()
        else showProgressDetail(index)
      },
      scales: {
        y: { min: 0, max: 100, ticks: { color: textColor }, grid: { color: gridColor } },
        x: { ticks: { color: textColor }, grid: { color: gridColor } },
      },
      plugins: { legend: { display: false } },
    },
  })
}

const CATEGORY_LABELS = {
  content_substance: 'Content Substance',
  composure_under_pressure: 'Composure Under Pressure',
  audience_responsiveness: 'Audience Responsiveness',
}

// One chip per persona present in `entries` — a single chip when the
// persona filter narrows the chart to one, several when it's "All personas".
function renderPersonalBests(entries) {
  const bestByPersona = new Map()
  for (const h of entries) {
    const current = bestByPersona.get(h.persona)
    if (current === undefined || h.overall_score > current) bestByPersona.set(h.persona, h.overall_score)
  }
  const root = $('progress-best')
  root.replaceChildren()
  const single = bestByPersona.size === 1
  for (const [persona, best] of bestByPersona) {
    const chip = document.createElement('span')
    chip.className = 'progress-best-chip'
    const label = single ? 'Personal best' : persona
    chip.append(`${label}: `, Object.assign(document.createElement('strong'), { textContent: best }))
    root.append(chip)
  }
  root.hidden = bestByPersona.size === 0
}

function hideProgressDetail() {
  openDetailIndex = null
  $('progress-detail').hidden = true
  $('progress-detail').replaceChildren()
}

function showProgressDetail(index) {
  const entry = lastFilteredHistory[index]
  if (!entry) return
  openDetailIndex = index

  const root = $('progress-detail')
  root.replaceChildren()

  const head = document.createElement('div')
  head.className = 'progress-detail-head'
  const meta = document.createElement('span')
  meta.className = 'progress-detail-meta'
  const when = new Date(entry.timestamp).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  })
  meta.textContent = `${entry.persona} — ${when}`
  const close = document.createElement('button')
  close.className = 'progress-detail-close'
  close.textContent = '×'
  close.setAttribute('aria-label', 'Close')
  close.onclick = hideProgressDetail
  head.append(meta, close)
  root.append(head)

  const score = document.createElement('div')
  score.className = 'score'
  const num = document.createElement('span')
  num.className = 'score-num'
  num.textContent = entry.overall_score
  const label = document.createElement('span')
  label.className = 'score-label'
  label.textContent = '/ 100'
  score.append(num, label)
  root.append(score)

  const categories = document.createElement('div')
  categories.className = 'categories'
  for (const [key, value] of Object.entries(entry.categories)) {
    const row = document.createElement('div')
    row.className = 'category'
    const catLabel = document.createElement('div')
    catLabel.className = 'category-label'
    catLabel.textContent = CATEGORY_LABELS[key] || key.replace(/_/g, ' ')
    const barWrap = document.createElement('div')
    barWrap.className = 'bar-wrap'
    const bar = document.createElement('div')
    bar.className = 'bar'
    bar.style.width = value + '%'
    barWrap.append(bar)
    const catScore = document.createElement('div')
    catScore.className = 'category-score'
    catScore.textContent = value
    row.append(catLabel, barWrap, catScore)
    categories.append(row)
  }
  root.append(categories)

  root.hidden = false
}

$('progress-filter').onchange = renderProgress
$('progress-clear').onclick = () => {
  if (confirm('Clear all saved progress history? This cannot be undone.')) {
    localStorage.removeItem(HISTORY_KEY)
    renderProgress()
  }
}

// Shows saved history immediately on page load, even before any call this session.
renderProgress()
