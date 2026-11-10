/* voz — the whole client: capture 16 kHz PCM, stream it, play 24 kHz PCM back.
   No model runs here: the phone is only a microphone and a speaker. */

(() => {
  const $ = (id) => document.getElementById(id);
  const mic = $('mic'), statusEl = $('status'), captionEl = $('caption'),
        net = $('net'), panel = $('panel'), hangup = $('hangup'),
        voiceEl = $('voice'), speedEl = $('speed'), captionsEl = $('captions');

  const IN_RATE = 16000, OUT_RATE = 24000;
  const LS = { voice: 'voz.voice', speed: 'voz.speed', captions: 'voz.captions' };

  let ws = null, ctx = null, mediaStream = null, micNode = null, micSource = null;
  let analyser = null, levelTimer = 0, wakeLock = null, active = false;
  let playHead = 0, playing = new Set();
  let acc = new Float32Array(0), ratio = 3;
  let state = 'idle', showCaptions = false, connectedAt = 0;
  let phase = 'auth';              // auth | open — governed by the voz-mfa challenge
  let challenge = null;            // {id} while waiting for the spoken code

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

  function analyse() {
    if (!analyser) return;
    const data = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (let i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i] - 128));
    const level = Math.min(1, peak / 90);
    mic.style.transform = state === 'listen' ? `scale(${1 + level * 0.06})` : '';
    if (!active) mic.style.transform = '';
  }

  /* --------------------------------------------------------------- auth (voz-mfa) */

  // The greeting and the two auth lines are only text on screen. Audio comes from the
  // assistant itself (turn handshake), so it uses the same voice as every answer.
  async function speak(text) { if (text) caption(text); }

  async function needChallenge() {
    const response = await fetch('/api/auth/challenge', { cache: 'no-store' });
    const body = await response.json();
    if (!body.needs_auth) { phase = 'open'; return true; }
    phase = 'auth';
    challenge = body.challenge || null;
    setState('idle', body.message || 'Necesitas autenticarte.');
    speak(body.message || 'Necesitas autenticarte.');
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
      if (body.ok) { phase = 'open'; return; }
      phase = 'auth';
      setState('idle', body.message || 'Error en la autenticación.');
      speak(body.message || 'Error en la autenticación.');
    } catch (e) {
      phase = 'auth';
      setState('idle', 'Error en la autenticación.');
    }
    // A used, expired or exhausted challenge is dead: ask for a new one, no reload.
    await newChallenge();
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'stop' }));
    setState('listen', 'Te escucho');
    speak('Necesitas autenticarte.');
  }

  async function newChallenge() {
    challenge = null;
    await fetch('/api/auth/challenge', { cache: 'no-store' })
      .then((r) => r.json()).catch(() => null);
  }

  /* ------------------------------------------------------------------- session */

  async function start() {
    if (active) return;
    if (phase === 'auth' && !challenge) await newChallenge();
    active = true;
    netState('busy');
    setState('listen', 'Conectando…');

    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      await ctx.resume();
      ratio = ctx.sampleRate / IN_RATE;

      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });

      micSource = ctx.createMediaStreamSource(mediaStream);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      micSource.connect(analyser);
      levelTimer = window.setInterval(analyse, 70);

      await ctx.audioWorklet.addModule('/static/capture-worklet.js');
      micNode = new AudioWorkletNode(ctx, 'capture', { numberOfOutputs: 1 });
      micNode.port.onmessage = (event) => {
        if (!ws || ws.readyState !== 1) return;
        const pcm = toPCM16(event.data);
        if (pcm && pcm.length) ws.send(pcm.buffer);
      };
      micSource.connect(micNode);
      // Keep the graph alive on iOS without feeding the mic back into the speaker.
      const silent = ctx.createGain();
      silent.gain.value = 0;
      micNode.connect(silent).connect(ctx.destination);
    } catch (error) {
      console.error(error);
      setState('idle', 'Sin permiso de micrófono');
      await stop(true);
      return;
    }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      connectedAt = Date.now();
      netState('live');
      setState('listen', 'Te escucho');
      ws.send(JSON.stringify({ t: 'voice', v: voiceEl.value }));
      ws.send(JSON.stringify({ t: 'speed', v: parseFloat(speedEl.value) }));
    };
    ws.onclose = (event) => {
      netState('down');
      if (active) {
        if (event.code === 4401) { location.href = '/login'; return; }
        setState('idle', 'Desconectado · toca para reintentar');
        stop(true);
      }
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
      handle(message);
    };

    try {
      if ('wakeLock' in navigator) wakeLock = await navigator.wakeLock.request('screen');
    } catch (e) { /* not fatal */ }

    hangup.hidden = false;
    $('hint').style.display = 'none';
  }

  function handle(message) {
    switch (message.t) {
      case 'ready':
        break;
      case 'state':
        if (message.v === 'listen') { setState('listen', 'Te escucho'); }
        else if (message.v === 'think') { flushAudio(); setState('think', 'Pensando…'); }
        else if (message.v === 'speak') { setState('speak', 'Hablando · toca para cortar'); }
        break;
      case 'heard':
        caption(message.text ? '“' + message.text + '”' : '');
        if (phase === 'auth') verify(message.text || '');
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
        phase = 'auth';
        setState('idle', message.msg || 'Necesitas autenticarte.');
        stop(true);
        needChallenge();
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
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    if (micNode) { try { micNode.disconnect(); } catch (e) {} micNode = null; }
    if (micSource) { try { micSource.disconnect(); } catch (e) {} micSource = null; }
    if (mediaStream) { mediaStream.getTracks().forEach((t) => t.stop()); mediaStream = null; }
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

  document.addEventListener('visibilitychange', () => {
    if (document.hidden && active) stop(true);
  });
  window.addEventListener('pagehide', () => { if (active) stop(true); });

  setState('idle', 'Toca y habla');
  needChallenge().catch(() => { phase = 'auth'; });
})();
