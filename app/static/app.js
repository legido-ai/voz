/* voz — the whole client: capture 16 kHz PCM, stream it, play 24 kHz PCM back.
   No model runs here: the phone is only a microphone and a speaker. */

(() => {
  const $ = (id) => document.getElementById(id);
  const mic = $('mic'), statusEl = $('status'), captionEl = $('caption'),
        net = $('net'), panel = $('panel'), hangup = $('hangup'),
        levelBar = $('levelbar'), levelFill = $('level'),
        voiceEl = $('voice'), speedEl = $('speed'), captionsEl = $('captions');

  const IN_RATE = 16000, OUT_RATE = 24000;
  const BUILD = 'scriptproc-1';   // logged by the server on connect
  const LS = { voice: 'voz.voice', speed: 'voz.speed', captions: 'voz.captions' };

  let ws = null, ctx = null, mediaStream = null, micNode = null, micSource = null;
  let analyser = null, levelTimer = 0, wakeLock = null, active = false, sink = null;
  let silentFrames = 0, rearmed = false;
  let playHead = 0, playing = new Set();
  let acc = new Float32Array(0), ratio = 3;
  let state = 'idle', showCaptions = false, connectedAt = 0;
  let phase = 'auth';              // auth | open — governed by the voz-mfa challenge
  let challenge = null;            // {id} while waiting for the spoken code
  let _session = '';               // session token handed over by the relay
  let reconnects = 0;              // consecutive reconnects in this call
  let lastPong = 0;                // heartbeat: a silent socket is a dead socket
  let heartbeat = 0;

  const saved = {
    voice: localStorage.getItem(LS.voice) || 'es_piper_f',
    speed: parseFloat(localStorage.getItem(LS.speed) || '1'),
    captions: localStorage.getItem(LS.captions) === '1',
  };
  voiceEl.value = saved.voice;
  speedEl.value = String(saved.speed);
  captionsEl.checked = saved.captions;
  showCaptions = saved.captions;

  function setState(next,label) {
    state = next;
    document.body.classList.remove('is-idle','is-listen','is-think','is-speak');
    document.body.classList.add('is-' + next);
    statusEl.textContent = label;
  }

  function netState(cls) { net.className = 'dot ' + cls; }

  function setStatusLabel() {
    if (state === 'idle') statusEl.textContent = 'Toca y habla';
    else if (state === 'listen') statusEl.textContent = 'Te escucho';
    else if (state === 'think') statusEl.textContent = 'Pensando…';
    else if (state === 'speak') statusEl.textContent = 'Hablando · toca para cortar';
  }

  function caption(text) {
    captionEl.textContent = showCaptions ? text : '';
  }

  /* ---------------------------------------------------------------- playback */

  function playPCM(int16) {
    if (!ctx) return;
    if (playHead < ctx.currentTime + 0.06) playHead = ctx.currentTime + 0.09;
    const buffer = ctx.createBuffer(1, int16.length, OUT_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < int16.length; i++) channel[i] = int16[i] / 32768;
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.start(playHead);
    playHead += buffer.duration;
    playing.add(source);
    source.onended = () => playing.delete(source);
  }

  function flushAudio() {
    for (const source of playing) { try { source.stop(); } catch (e) {} }
    playing.clear();
    playHead = 0;
  }

  /* --------------------------------------------------------------- downsampler */

  function toPCM16(floatChunk) {
    const merged = new Float32Array(acc.length + floatChunk.length);
    merged.set(acc, 0);
    merged.set(floatChunk, acc.length);
    const block = Math.max(1, Math.round(ratio));
    const outLen = Math.floor(merged.length / block);
    if (!outLen) { acc = merged; return null; }
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      let sum = 0;
      const base = i * block;
      for (let k = 0; k < block; k++) sum += merged[base + k];
      const v = sum / block;
      out[i] = v <= -1 ? -32768 : v >= 1 ? 32767 : (v * 32767) | 0;
    }
    acc = merged.slice(outLen * block);
    return out;
  }

  let silentSince = 0;

  // One place that turns captured float samples into wire frames, shared by every
  // capture path, with the diagnostic state the logs and the UI read.
  function sendBlock(block) {
    if (!block || !block.length) return;
    if (ctx && ctx.state !== 'running') { try { ctx.resume(); } catch (e) {} }
    let m = 0;
    for (let i = 0; i < block.length; i++) if (Math.abs(block[i]) > m) m = Math.abs(block[i]);
    window.__voz = { block: block.length, blockMax: m, sent: (window.__voz || {}).sent || 0 };
    if (!ws || ws.readyState !== 1) return;
    const pcm = toPCM16(block);
    if (pcm && pcm.length) { ws.send(pcm.buffer); window.__voz.sent++; }
  }

  let lastDiag = 0;

  function sendDiag(level) {
    const now = Date.now();
    if (now - lastDiag < 2000 || !ws || ws.readyState !== 1) return;
    lastDiag = now;
    const track = (mediaStream && mediaStream.getAudioTracks()[0]) || null;
    ws.send(JSON.stringify({ t: 'diag', v: {
      ctx: ctx ? ctx.state : '-',
      rate: ctx ? ctx.sampleRate : 0,
      track: track ? track.readyState : 'none',
      muted: track ? track.muted : null,
      enabled: track ? track.enabled : null,
      peak: level,
      blockMax: (window.__voz || {}).blockMax,
      sent: (window.__voz || {}).sent,
      ua: navigator.userAgent.slice(0, 60),
    }}));
  }

  function analyse() {
    if (!analyser) return;
    const data = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (let i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i] - 128));
    const level = Math.min(1, peak / 90);
    if (levelFill) { levelBar.hidden = false; levelFill.style.width = Math.round(level * 100) + '%'; }
    if (active) sendDiag(peak);
    // iOS hands us silence when the context was created outside a gesture, or when the
    // mic is muted. Say so instead of pretending to listen.
    if (peak === 0) {
      silentFrames += 1;
      // 1.5 s of digital silence: re-acquire the microphone once without any DSP
      // before telling the user something is wrong.
      // Never re-acquire on our own: a rearm mid-sentence kills a healthy stream
      // (verified) and silence is the normal state while the user reads a code.
      if (!silentSince) silentSince = Date.now();
      else if (Date.now() - silentSince > 4000 && state === 'listen') {
        // In-app browsers hand over a muted track: the graph is alive, the samples are
        // zero. Naming the browser is more useful than blaming the microphone.
        const inApp = /Telegram|FBAN|FBAV|Instagram|Line\//i.test(navigator.userAgent);
        statusEl.textContent = inApp
          ? 'Micrófono bloqueado por este navegador · abre en Safari'
          : 'Micrófono sin señal · toca para reintentar';
        silentSince = Date.now();
      }
    } else {
      silentFrames = 0;
      silentSince = 0;
      rearmed = false;
    }
    mic.style.transform = state === 'listen' ? `scale(${1 + level * 0.06})` : '';
    if (!active) mic.style.transform = '';
  }

  /* --------------------------------------------------------------- auth (voz-mfa) */

  // The greeting and the two auth lines are only text on screen. Audio comes from the
  // assistant itself (turn handshake), so it uses the same voice as every answer.
  async function speak(text) { if (text) caption(text); }

  // Remove the spoken code (including spaces between digits) from the transcript.
  function stripCode(text) {
    return (text || '')
      .replace(/\b\d(?:\s*\d){5}\b/g, ' ')
      .replace(/\s*(c[oó]digo|clave)\s*/gi, ' ')
      .replace(/[\s,;.]{2,}/g, ' ')
      .replace(/^[\s,;.]+|[\s,;.]+$/g, '')
      .trim();
  }

  async function checkSession() {
    const response = await fetch('/api/session', { cache: 'no-store' });
    phase = response.ok ? 'open' : 'auth';
    return phase === 'open';
  }

  // Mint on intent only. The server reuses a live challenge for this client, so a
  // second tap does not push a second Telegram message.
  // Ask for a code at most once per episode: voz-mfa reuses the caller's live challenge,
  // so this cannot produce a second message, and the flag stops the UI from asking twice.
  let asked = false;

  function ensureChallenge() {
    if (asked) return;
    asked = true;
    needChallenge();
  }

  async function needChallenge() {
    const response = await fetch('/api/auth/challenge', { cache: 'no-store' });
    if (response.status === 429) {
      phase = 'auth';
      setState('idle', 'Error en la autenticación.');
      return false;
    }
    const body = await response.json();
    if (!body.needs_auth) { phase = 'open'; return true; }
    phase = 'auth';
    challenge = body.challenge || null;
    // A challenge whose code was not delivered is useless: say so instead of waiting.
    const delivered = !challenge || challenge.sent !== false;
    setState('idle', delivered ? (body.message || 'Necesitas autenticarte.')
                               : 'Error en la autenticación.');
    speak(delivered ? (body.message || 'Necesitas autenticarte.') : 'Error en la autenticación.');
    return false;
  }

  // The code is verified server-side, in the same phrase as the request: the app never
  // knows which six digits were sent to Telegram.
  async function verify(token) {
    try {
      const response = await fetch('/api/auth/verify', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, challenge: challenge && challenge.id }),
      });
      const body = await response.json();
      if (body.ok) {
        phase = 'open';
        asked = false;
        challenge = null;
        // The code rode along with the request: run the request now, once, without the
        // digits (the user must not have to dictate the same sentence twice).
        const request = stripCode(token);
        if (request && ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'text', v: request }));
        return;
      }
      phase = 'auth';
      setState('idle', body.message || 'Error en la autenticación.');
      speak(body.message || 'Error en la autenticación.');
    } catch (e) {
      phase = 'auth';
      setState('idle', 'Error en la autenticación.');
    }
    // Keep the challenge and the microphone: a wrong reading is retried against the
    // same code (10 attempts, 10 minutes) instead of spawning a second Telegram message.
    setState('listen', 'Te escucho');
  }

  async function newChallenge() {
    challenge = null;
    await fetch('/api/auth/challenge', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null)
      .then((body) => { if (body && body.challenge) challenge = body.challenge; });
  }

  // Reopen the socket without tearing down the microphone or the audio graph.
  function reconnect() {
    if (!active) return;
    reconnects += 1;
    netState('busy');
    setState('listen', 'Reconectando…');
    openSocket();
    // Auth comes after the microphone exists: minting may take a round trip, and the
    // socket no longer waits on it.
    if (phase === 'auth' && !challenge) needChallenge();
  }

  // Give the microphone a second chance without dropping the call: stop the tracks,
  // build the graph again, keep the socket and the session.
  async function rearmAudio() {
    try {
      if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
      if (micNode) { try { micNode.disconnect(); } catch (e) {} }
      if (micSource) { try { micSource.disconnect(); } catch (e) {} }
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        video: false,
      });
      micSource = ctx.createMediaStreamSource(mediaStream);
      if (analyser) micSource.connect(analyser);
      // WebKit feeds a MediaStreamSource into ScriptProcessor but hands zeros to an
      // AudioWorkletNode on the same graph (analyser shows signal, worklet does not).
      // Deprecated, and the only capture path that works on every iPhone here.
      micNode = ctx.createScriptProcessor(2048, 1, 1);
      micNode.onaudioprocess = (event) => sendBlock(event.inputBuffer.getChannelData(0));
      micSource.connect(micNode);
      // Keep the graph pulling without echoing the mic to the speaker.
      const mute = ctx.createGain(); mute.gain.value = 0;
      micNode.connect(mute).connect(ctx.destination);
      silentFrames = 0;
      console.warn('audio: microphone re-armed without DSP');
    } catch (error) {
      console.error('rearm failed', error);
      setState('idle', 'Micrófono sin señal · toca para reintentar');
    }
  }

  /* ------------------------------------------------------------------- session */

  async function start() {
    if (active) return;
    // iOS only grants audio inside the tap's own task. Anything awaited before this —
    // a challenge request, for instance — loses the gesture and the phone then feeds
    // digital silence (seen in production: rms 0.0000 with a socket wide open).
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (error) {
      setState('idle', 'Sin audio');
      return;
    }
    active = true;
    netState('busy');
    setState('listen', 'Conectando…');

    try {
      await ctx.resume();
      ratio = ctx.sampleRate / IN_RATE;

      // Plain constraints first: the DSP trio can make iOS hand back a muted track
      // on some hardware/headset combinations, and silence is indistinguishable from
      // a broken server. The fallback below re-acquires raw audio if this is silent.
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      if (!mediaStream.getAudioTracks().length) throw new Error('no audio track');
      mediaStream.getAudioTracks()[0].enabled = true;
      // The permission prompt interrupts the audio session: iOS leaves the context
      // suspended, and a suspended context still runs the worklet — emitting zeros.
      // This is the "socket open, rms=0.0000" failure, so resume again here.
      if (ctx.state !== 'running') { try { await ctx.resume(); } catch (e) {} }

      micSource = ctx.createMediaStreamSource(mediaStream);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      micSource.connect(analyser);
      levelTimer = window.setInterval(analyse, 70);

      // WebKit feeds a MediaStreamSource into ScriptProcessor but hands zeros to an
      // AudioWorkletNode on the same graph: the analyser shows signal while the worklet
      // block is all zeros, which reached production as rms=0.0000. Deprecated API, and
      // the only capture path that works on every iPhone here.
      micNode = ctx.createScriptProcessor(2048, 1, 1);
      micNode.onaudioprocess = (event) => sendBlock(event.inputBuffer.getChannelData(0));
      micSource.connect(micNode);
      // Keep the graph pulling without echoing the microphone to the speaker.
      const mute = ctx.createGain(); mute.gain.value = 0;
      micNode.connect(mute).connect(ctx.destination);

    } catch (error) {
      console.error(error);
      setState('idle', 'Sin permiso de micrófono');
      await stop(true);
      return;
    }

    openSocket();

    try {
      if ('wakeLock' in navigator) wakeLock = await navigator.wakeLock.request('screen');
    } catch (e) { /* not fatal */ }

    hangup.hidden = false;
    $('hint').style.display = 'none';
  }

  // The socket alone is replaceable: the microphone, the audio graph and the UI state
  // stay up, so a dropped connection is invisible unless it cannot be restored.
  function openSocket() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const saved = _session || (function () { try { return localStorage.getItem('voz.session') || ''; } catch (e) { return ''; } })();
    const query = saved ? `?s=${encodeURIComponent(saved)}` : '';
    ws = new WebSocket(`${proto}://${location.host}/ws${query}`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      connectedAt = Date.now();
      reconnects = 0;
      lastPong = Date.now();
      netState('live');
      setState('listen', 'Te escucho');
      ws.send(JSON.stringify({ t: 'hello', v: BUILD }));
      ws.send(JSON.stringify({ t: 'voice', v: voiceEl.value }));
      ws.send(JSON.stringify({ t: 'speed', v: parseFloat(speedEl.value) }));
      if (!heartbeat) heartbeat = window.setInterval(() => {
        if (!ws || ws.readyState !== 1) return;
        // A socket that stops answering is worse than a closed one: it stays "open"
        // while swallowing everything the user says.
        if (Date.now() - lastPong > 45000) { try { ws.close(); } catch (e) {} return; }
        ws.send(JSON.stringify({ t: 'ping' }));
      }, 15000);
    };
    ws.onclose = (event) => {
      netState('down');
      if (!active) return;
      if (event.code === 4401) { phase = 'auth'; }     // no login page any more
      if (reconnects < 5) { reconnect(); return; }
      setState('idle', 'Desconectado · toca para reintentar');
      stop(true);
    };
    ws.onerror = () => netState('down');
    ws.onmessage = (event) => {
      if (typeof event.data !== 'string') {
        playPCM(new Int16Array(event.data));
        if (state !== 'speak') { setState('speak', 'Hablando · toca para cortar'); caption(''); }
        return;
      }
      let message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      if (message.t === 'pong') { lastPong = Date.now(); return; }
      handle(message);
    };
  }

  function handle(message) {
    switch (message.t) {
      case 'ready':
        // The server is the authority on the session: if it says we are not authorised,
        // make sure a code exists *now*, before the user speaks. Trusting the client's
        // own idea of the session is what left him talking with no code in Telegram.
        if (message.authorised === false) {
          phase = 'auth';
          ensureChallenge();
        } else {
          phase = 'open';
        }
        break;
      case 'state':
        if (message.v === 'listen') { setState('listen', 'Te escucho'); }
        else if (message.v === 'think') { flushAudio(); setState('think', 'Pensando…'); }
        else if (message.v === 'speak') { setState('speak', 'Hablando · toca para cortar'); }
        break;
      case 'heard':
        caption(message.text ? '“' + message.text + '”' : '');
        break;
      case 'session':
        // The relay verified the code and handed us the session: remember it for reconnects.
        phase = 'open';
        asked = false;
        challenge = null;
        try { localStorage.setItem('voz.session', message.token || ''); } catch (e) {}
        _session = message.token || '';
        break;
      case 'partial':
        if (showCaptions) caption('… ' + message.text);
        break;
      case 'say':
        break;
      case 'flush':
        flushAudio();
        break;
      case 'error':
        caption(message.msg || '');
        break;
      case 'auth_expired':
        // Keep the socket and the microphone: hanging up here is what made the app
        // feel like it "stops listening" mid-sentence.
        phase = 'auth';
        setState('listen', 'Te escucho');
        ensureChallenge();
        break;
      case 'voice':
        break;
      default:
        break;
    }
  }

  async function stop(silent) {
    active = false;
    if (levelTimer) { clearInterval(levelTimer); levelTimer = 0; }
    if (heartbeat) { clearInterval(heartbeat); heartbeat = 0; }
    reconnects = 0;
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    if (micNode) { try { micNode.disconnect(); } catch (e) {} micNode = null; }
    if (micSource) { try { micSource.disconnect(); } catch (e) {} micSource = null; }
    if (mediaStream) { mediaStream.getTracks().forEach((t) => t.stop()); mediaStream = null; }
    sink = null;
    flushAudio();
    if (ctx) { try { await ctx.close(); } catch (e) {} ctx = null; }
    if (wakeLock) { try { await wakeLock.release(); } catch (e) {} wakeLock = null; }
    analyser = null;
    acc = new Float32Array(0);
    mic.style.transform = '';
    hangup.hidden = true;
    netState('');
    if (!silent) setState('idle', 'Toca y habla');
    caption('');
  }

  /* -------------------------------------------------------------------- events */

  mic.addEventListener('click', async () => {
    if (!active) return start();
    if (state === 'listen') return stop(false);            // second tap hangs up
    flushAudio();                                          // thinking/speaking: cut in
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'stop' }));
    setState('listen', 'Te escucho');
  });

  hangup.addEventListener('click', (event) => { event.stopPropagation(); stop(false); });

  const logoutEl = $('logout');
  if (logoutEl) {
    logoutEl.addEventListener('click', async (event) => {
      event.preventDefault();
      const token = _session || (function () { try { return localStorage.getItem('voz.session') || ''; } catch (e) { return ''; } })();
      try {
        await fetch('/logout' + (token ? `?s=${encodeURIComponent(token)}` : ''), { credentials: 'same-origin' });
      } catch (e) { /* revoking locally still matters */ }
      try { localStorage.removeItem('voz.session'); } catch (e) {}
      _session = '';
      challenge = null;
      asked = false;
      phase = 'auth';
      if (active) stop(false);
      location.href = '/';
    });
  }

  const settingsBtn = $('settings');
  settingsBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    panel.hidden = !panel.hidden;
  });
  $('close').addEventListener('click', () => { panel.hidden = true; });

  // Tapping anywhere outside the panel collapses it.
  document.addEventListener('click', (event) => {
    if (panel.hidden) return;
    if (panel.contains(event.target) || settingsBtn.contains(event.target)) return;
    panel.hidden = true;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') panel.hidden = true;
  });

  voiceEl.addEventListener('change', () => {
    localStorage.setItem(LS.voice, voiceEl.value);
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'voice', v: voiceEl.value }));
  });
  speedEl.addEventListener('input', () => {
    localStorage.setItem(LS.speed, speedEl.value);
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'speed', v: parseFloat(speedEl.value) }));
  });
  captionsEl.addEventListener('change', () => {
    showCaptions = captionsEl.checked;
    localStorage.setItem(LS.captions, showCaptions ? '1' : '0');
    if (!showCaptions) caption('');
  });

  // Switching to Telegram to read the code hides the tab. Hanging up here killed the
  // socket and the microphone, and the page came back showing "Te escucho" with
  // nothing behind it. Now the session survives; iOS may suspend audio, so resume it.
  document.addEventListener('visibilitychange', async () => {
    if (document.hidden) return;
    if (ctx && ctx.state !== 'running') { try { await ctx.resume(); } catch (e) {} }
    if (active && (!ws || ws.readyState > 1)) reconnect();
    else if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'ping' }));
  });
  window.addEventListener('pagehide', () => { if (active) stop(true); });

  setState('idle', 'Toca y habla');
  // No challenge is minted on load: a challenge pushes a Telegram message, and an
  // anonymous GET must not be able to ring the user's phone. It is minted on the
  // first mic tap, which is deliberate intent.
  checkSession().catch(() => { phase = 'auth'; });
})();
