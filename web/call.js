// Созвоны (#86): WebRTC mesh, сигналинг — WS /api/call/ws?chat=, комната
// привязана к чату. Исходящий звук: AEC/AGC браузера + highpass + компрессор
// (WebAudio) и по желанию RNNoise с гейтом (#102, denoise-worklet.js) — он
// выключен по умолчанию. Каждый участник пишет СВОЙ микрофон MediaRecorder-ом
// и шлёт чанки на сервер — пер-спикерные дорожки для расшифровки.
const CALL = (() => {
  let active = null;    // текущий звонок (см. join)
  let callsMap = {};    // chat_id(str) -> {users:[uid], started_by} из поллинга
  let incoming = null;  // баннер входящего: {chatId, el}
  let viewerEl = null;  // полноэкранный просмотр демонстрации

  const secure = window.isSecureContext;

  function wsUrl(chatId){
    const u = new URL("api/call/ws?chat=" + chatId, location.href);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    return u.toString();
  }

  // Громкости (#99): мой микрофон 0–200% (gain в WebAudio-цепочке), каждый
  // собеседник и звук демонстрации 0–100% (volume их <audio>). Помнятся
  // между звонками: gd_call_vol_mic / _u<uid> / _screen.
  function getVol(key){
    const v = parseFloat(localStorage.getItem("gd_call_vol_" + key));
    const max = key === "mic" ? 2 : 1;
    return isNaN(v) ? 1 : Math.min(max, Math.max(0, v));
  }

  function applyVol(key, v){
    if (!active) return;
    if (key === "mic"){
      if (active.micGain) active.micGain.gain.value = v;
    } else if (key === "screen"){
      const el = document.getElementById("call-screen-audio");
      if (el) el.volume = v;
    } else {
      const p = active.pcs.get(+key.slice(1));
      if (p && p.audioEl) p.audioEl.volume = v;
    }
  }

  // Шумоподавление (#102): RNNoise + гейт в AudioWorklet, тумблер в том же
  // поповере. Помнится между звонками; по умолчанию ВЫКЛЮЧЕНО — обработка
  // добавляет 40 мс задержки и грузит wasm, так что включает её тот, кому
  // она нужна.
  function denoiseOn(){
    return localStorage.getItem("gd_call_denoise") === "1";
  }

  async function setDenoise(on){
    localStorage.setItem("gd_call_denoise", on ? "1" : "0");
    const a = active;
    if (!a) return;
    // Ленивая загрузка: пока шумодав не включили, wasm не качается вовсе.
    if (on && !a.denoise){
      if (!a.denoiseWait) a.denoiseWait = makeDenoise(a.ctx);
      a.denoise = await a.denoiseWait;
      a.denoiseWait = null;
      if (active !== a) return;   // звонок закончился, пока грузился wasm
      if (!a.denoise){
        // Молча оставить галочку включённой нельзя: человек будет думать,
        // что шумодав работает.
        localStorage.setItem("gd_call_denoise", "0");
        const box = document.getElementById("cvp-denoise");
        if (box) box.checked = false;
        APP.alert("Не удалось включить шумоподавление: браузер не поддерживает" +
                  " обработку звука в AudioWorklet.", "Звонок");
        return;
      }
    }
    if (a.denoise) a.denoise.port.postMessage({on});
    routeDenoise();
    syncMicNs();
  }

  // Нода стоит в тракте, только пока шумодав включён: иначе она добавляла бы
  // свои 40 мс задержки и молола тишину впустую.
  function routeDenoise(){
    const a = active;
    if (!a) return;
    try { a.hpNode.disconnect(); } catch {}
    if (a.denoise){
      try { a.denoise.disconnect(); } catch {}
    }
    if (a.denoise && denoiseOn()){
      a.hpNode.connect(a.denoise);
      a.denoise.connect(a.compNode);
    } else {
      a.hpNode.connect(a.compNode);
    }
  }

  // Пока работает свой RNNoise, браузерный шумодав только мешает: двойная
  // обработка даёт артефакты. AEC и AGC остаются за браузером.
  function syncMicNs(){
    if (!active || !active.mic) return;
    const track = active.mic.getAudioTracks()[0];
    if (!track || !track.applyConstraints) return;
    const own = !!(active.denoise && active.denoise.rnnoise) && denoiseOn();
    track.applyConstraints({echoCancellation: true, autoGainControl: true,
                            noiseSuppression: !own}).catch(() => {});
  }

  // Всё, что может не получиться (нет wasm, браузер без AudioWorklet, частота
  // не 48 кГц, DataCloneError на WebAssembly.Module), кончается работой без
  // ноды или без RNNoise: звонок важнее шумодава.
  async function makeDenoise(ctx){
    if (!ctx.audioWorklet) return null;
    let mod = null;
    try {
      if (ctx.sampleRate === 48000){
        const r = await fetch("vendor/rnnoise.wasm");
        if (r.ok) mod = await WebAssembly.compile(await r.arrayBuffer());
      }
    } catch (e){ console.warn("rnnoise wasm", e); }
    try { await ctx.audioWorklet.addModule("denoise-worklet.js"); }
    catch (e){ console.warn("denoise worklet", e); return null; }
    const opts = {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
                  channelCount: 1, channelCountMode: "explicit",
                  processorOptions: {wasm: mod, on: denoiseOn()}};
    let node;
    try { node = new AudioWorkletNode(ctx, "mic-denoise", opts); }
    catch (e){
      // Safari не умеет клонировать WebAssembly.Module в processorOptions —
      // пробуем ещё раз без него, гейт работает и без RNNoise.
      console.warn("denoise node", e);
      if (!mod) return null;
      opts.processorOptions.wasm = null;
      try { node = new AudioWorkletNode(ctx, "mic-denoise", opts); }
      catch { return null; }
    }
    node.port.onmessage = e => {
      const d = e.data || {};
      if (d.t !== "ready") return;
      node.rnnoise = d.rnnoise;
      if (d.err) console.warn("rnnoise:", d.err);
      syncMicNs();
    };
    return node;
  }

  // Выбранный микрофон помнится между звонками; ideal — если устройство
  // пропало, браузер молча возьмёт дефолтное, а не уронит getUserMedia.
  function micConstraints(exactId){
    const c = {echoCancellation: true, noiseSuppression: true,
               autoGainControl: true};
    const saved = localStorage.getItem("gd_mic_id");
    if (exactId) c.deviceId = {exact: exactId};
    else if (saved) c.deviceId = {ideal: saved};
    return c;
  }

  // ------------------------------------------------------------- запись
  // Загрузчик живёт отдельно от active: после leave() дослать хвост очереди.

  function makeUploader(callId, session){
    const up = {queue: [], seq: 0, busy: false, unloading: false};
    up.push = blob => {
      up.queue.push({seq: up.seq++, blob});
      up.pump();
    };
    up.pump = async () => {
      if (up.busy || !up.queue.length) return;
      up.busy = true;
      const item = up.queue[0];
      const url = `api/call/${callId}/rec?session=${session}&seq=${item.seq}`;
      if (up.unloading && navigator.sendBeacon){
        if (navigator.sendBeacon(url, item.blob)) up.queue.shift();
        up.busy = false;
        return;
      }
      try {
        const r = await fetch(url, {method: "POST", body: item.blob});
        // 409 — звонок финализирован или дыра в seq: чанк уже не пристроить.
        if (r.ok || r.status === 409) up.queue.shift();
        up.busy = false;
        if (up.queue.length) up.pump();
      } catch {
        up.busy = false;
        setTimeout(up.pump, 1500);  // сеть моргнула — ретрай с тем же seq
      }
    };
    return up;
  }

  // ------------------------------------------------------------- звонок

  async function join(chat){
    if (active){
      if (active.chat.id === chat.id) return;
      APP.alert("Сначала завершите текущий звонок.", "Звонок");
      return;
    }
    let mic;
    try {
      mic = await navigator.mediaDevices.getUserMedia({audio: micConstraints()});
    } catch {
      APP.alert("Нет доступа к микрофону: разрешите его в браузере.", "Звонок");
      return;
    }
    // Цепочка обработки: срез низкочастотного гула, дальше — включённый
    // шумодав (строго ДО компрессора: иначе компрессор накачивает шум,
    // который гейт следом приглушает, и это слышно как «дыхание») и
    // компрессор, чтобы тихих было слышно.
    const AC = window.AudioContext || window.webkitAudioContext;
    let ctx;
    // RNNoise живёт только на 48 кГц; не дал браузер — работаем без него.
    try { ctx = new AC({sampleRate: 48000, latencyHint: "interactive"}); }
    catch { ctx = new AC(); }
    const src = ctx.createMediaStreamSource(mic);
    const hp = ctx.createBiquadFilter();
    hp.type = "highpass"; hp.frequency.value = 90; hp.Q.value = 0.7;
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -28; comp.knee.value = 12; comp.ratio.value = 4;
    comp.attack.value = 0.003; comp.release.value = 0.25;
    const makeup = ctx.createGain(); makeup.gain.value = 1.2;
    const micGain = ctx.createGain(); micGain.gain.value = getVol("mic");
    const dest = ctx.createMediaStreamDestination();

    // Включённый шумодав грузится параллельно с ICE — оба нужны до первого
    // пира; выключенный не грузится совсем.
    const [ice, denoise] = await Promise.all([
      fetch("api/call/ice").then(r => r.json()).catch(() => null),
      denoiseOn() ? makeDenoise(ctx) : null,
    ]);
    const iceServers = (ice && ice.iceServers) || [];

    src.connect(hp);
    comp.connect(makeup).connect(micGain).connect(dest);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    micGain.connect(analyser);

    active = {
      chat, callId: 0, session: 1, ws: null, closing: false,
      ctx, mic, srcNode: src, hpNode: hp, compNode: comp, micGain, denoise,
      denoiseWait: null, outStream: dest.stream,
      outTrack: dest.stream.getAudioTracks()[0],
      iceServers, pcs: new Map(), roster: new Map(),
      muted: false, sharing: false, screenStream: null,
      sharerId: 0, recorder: null, uploader: null,
      analysers: new Map([[window.ME.id, analyser]]),
      startedAt: Date.now(), timers: [],
    };
    dismissIncoming();
    routeDenoise();
    syncMicNs();  // ready из worklet мог прийти раньше, чем появился active

    const ws = new WebSocket(wsUrl(chat.id));
    active.ws = ws;
    ws.onmessage = e => { onWs(JSON.parse(e.data)); };
    ws.onclose = e => {
      if (!active || active.ws !== ws || active.closing) return;
      const replaced = e.code === 4000;
      teardown();
      APP.alert(replaced ? "Звонок продолжен в другой вкладке."
                         : "Соединение со звонком потеряно.", "Звонок");
    };
    active.timers.push(setInterval(() => {
      if (ws.readyState === 1) ws.send(JSON.stringify({t: "ping"}));
    }, 20000));
    active.timers.push(setInterval(updateClock, 1000));
    active.timers.push(setInterval(pollSpeaking, 150));
    SOUND.callJoin();
    renderDock();
  }

  function send(obj){
    if (active && active.ws && active.ws.readyState === 1)
      active.ws.send(JSON.stringify(obj));
  }

  function onWs(msg){
    if (!active) return;
    if (msg.t === "joined"){
      active.callId = msg.call_id;
      active.session = msg.session;
      active.uploader = makeUploader(msg.call_id, msg.session);
      startRecorder();
      for (const p of msg.peers || []){
        active.roster.set(p.id, p);
        newPeer(p.id);
        if (p.sharing) active.sharerId = p.id;
      }
      renderDock();
    } else if (msg.t === "peer-joined"){
      active.roster.set(msg.peer.id, msg.peer);
      newPeer(msg.peer.id);
      renderDock();
    } else if (msg.t === "peer-left"){
      dropPeer(msg.id);
      renderDock();
    } else if (msg.t === "signal"){
      onSignal(msg.from, msg.data || {});
    } else if (msg.t === "state"){
      const p = active.roster.get(msg.id);
      if (p){ p.muted = msg.muted; p.sharing = msg.sharing; }
      if (msg.sharing) active.sharerId = msg.id;
      else if (active.sharerId === msg.id){ active.sharerId = 0; setRemoteScreen(null, ""); }
      renderDock();
    } else if (msg.t === "stop-share"){
      stopShare(false);
    }
  }

  // Perfect negotiation: на пару вежлив тот, у кого id больше.
  function newPeer(uid){
    const pc = new RTCPeerConnection({iceServers: active.iceServers});
    const p = {pc, polite: window.ME.id > uid, makingOffer: false,
               ignoreOffer: false, audioEl: null, videoSender: null,
               screenAudioSender: null};
    active.pcs.set(uid, p);
    pc.addTrack(active.outTrack, active.outStream);
    if (active.sharing && active.screenStream){
      p.videoSender = pc.addTrack(active.screenStream.getVideoTracks()[0],
                                  active.screenStream);
      const sa = active.screenStream.getAudioTracks()[0];
      if (sa) p.screenAudioSender = pc.addTrack(sa, active.screenStream);
    }
    pc.onnegotiationneeded = async () => {
      try {
        p.makingOffer = true;
        await pc.setLocalDescription();
        send({t: "signal", to: uid, data: {description: pc.localDescription}});
      } catch {} finally { p.makingOffer = false; }
    };
    pc.onicecandidate = e => {
      if (e.candidate) send({t: "signal", to: uid, data: {candidate: e.candidate}});
    };
    pc.onconnectionstatechange = () => {
      if (!active) return;
      const bad = pc.connectionState === "failed";
      const el = document.querySelector(`.call-tile[data-uid="${uid}"]`);
      if (el) el.classList.toggle("unreachable", bad);
      if (bad && !active.failWarned){
        active.failWarned = true;
        const name = (active.roster.get(uid) || {}).name || "участником";
        APP.alert(`Не удалось установить соединение с ${name}: сети не` +
          ` пропускают прямой трафик. Нужен TURN-сервер (карточка #86).`,
          "Звонок");
      }
    };
    pc.ontrack = e => {
      const stream = e.streams[0] || new MediaStream([e.track]);
      if (e.track.kind === "audio"){
        // Звук демонстрации приходит в одном стриме с её видео; видео-трек
        // добавляется отправителем первым, так что его m-line (и ontrack)
        // раньше — screenSid к этому моменту уже известен.
        if (stream.getVideoTracks().length ||
            (active.screenSid && stream.id === active.screenSid)){
          let el = document.getElementById("call-screen-audio");
          if (!el){
            el = document.createElement("audio");
            el.id = "call-screen-audio";
            el.autoplay = true;
            ensureAudioBox().appendChild(el);
          }
          el.volume = getVol("screen");
          el.srcObject = stream;
          el.play().catch(() => {});
          return;
        }
        if (!p.audioEl){
          p.audioEl = document.createElement("audio");
          p.audioEl.autoplay = true;
          ensureAudioBox().appendChild(p.audioEl);
        }
        p.audioEl.volume = getVol("u" + uid);
        p.audioEl.srcObject = stream;
        p.audioEl.play().catch(() => {});
        try {
          const an = active.ctx.createAnalyser();
          an.fftSize = 512;
          active.ctx.createMediaStreamSource(stream).connect(an);
          active.analysers.set(uid, an);
        } catch {}
      } else {
        active.screenSid = stream.id;
        const name = (active.roster.get(uid) || {}).name || "";
        setRemoteScreen(stream, name);
        e.track.onended = () => {
          if (active && active.sharerId === uid) setRemoteScreen(null, "");
        };
      }
    };
    return p;
  }

  async function onSignal(from, data){
    let p = active.pcs.get(from);
    if (!p) p = newPeer(from);
    const pc = p.pc;
    try {
      if (data.description){
        const collision = data.description.type === "offer" &&
          (p.makingOffer || pc.signalingState !== "stable");
        p.ignoreOffer = !p.polite && collision;
        if (p.ignoreOffer) return;
        await pc.setRemoteDescription(data.description);
        if (data.description.type === "offer"){
          await pc.setLocalDescription();
          send({t: "signal", to: from, data: {description: pc.localDescription}});
        }
      } else if (data.candidate){
        try { await pc.addIceCandidate(data.candidate); }
        catch (e){ if (!p.ignoreOffer) throw e; }
      }
    } catch (e){ console.warn("call signal", e); }
  }

  function dropPeer(uid){
    const p = active.pcs.get(uid);
    if (p){
      try { p.pc.close(); } catch {}
      if (p.audioEl) p.audioEl.remove();
      active.pcs.delete(uid);
    }
    active.roster.delete(uid);
    active.analysers.delete(uid);
    if (active.sharerId === uid){ active.sharerId = 0; setRemoteScreen(null, ""); }
  }

  // ------------------------------------------------------------- запись

  function startRecorder(){
    let rec;
    try {
      rec = new MediaRecorder(active.outStream,
        {mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: 64000});
    } catch {
      try { rec = new MediaRecorder(active.outStream); } catch { return; }
    }
    active.recorder = rec;
    const up = active.uploader;
    rec.ondataavailable = e => { if (e.data && e.data.size) up.push(e.data); };
    rec.start(5000);
  }

  window.addEventListener("pagehide", () => {
    if (!active) return;
    active.uploader && (active.uploader.unloading = true);
    try { active.recorder && active.recorder.state !== "inactive" &&
          active.recorder.stop(); } catch {}
  });

  // ------------------------------------------------------------- управление

  function toggleMute(){
    if (!active) return;
    active.muted = !active.muted;
    active.outTrack.enabled = !active.muted;
    send({t: "state", muted: active.muted, sharing: active.sharing});
    renderDock();
  }

  // Смена микрофона на лету: PC и MediaRecorder держат трек dest-ноды,
  // поэтому меняется только вход WebAudio-графа — без перепереговоров
  // и без разрыва записи.
  async function switchMic(deviceId){
    if (!active) return;
    let mic;
    try {
      mic = await navigator.mediaDevices.getUserMedia(
        {audio: micConstraints(deviceId)});
    } catch {
      APP.alert("Не удалось открыть выбранный микрофон.", "Звонок");
      return;
    }
    localStorage.setItem("gd_mic_id", deviceId);
    for (const t of active.mic.getTracks()) t.stop();
    try { active.srcNode.disconnect(); } catch {}
    active.mic = mic;
    active.srcNode = active.ctx.createMediaStreamSource(mic);
    active.srcNode.connect(active.hpNode);
    syncMicNs();
  }

  async function pickMic(e){
    if (!active) return;
    let devs = [];
    try {
      devs = (await navigator.mediaDevices.enumerateDevices())
        .filter(d => d.kind === "audioinput" && d.deviceId);
    } catch {}
    if (!devs.length){
      APP.alert("Не удалось получить список микрофонов.", "Звонок");
      return;
    }
    const curId =
      (active.mic.getAudioTracks()[0].getSettings() || {}).deviceId;
    APP.openContextMenu(e.clientX, e.clientY, devs.map(d => ({
      icon: ICON(d.deviceId === curId ? "check" : "mic", "sm"),
      label: MD.esc(d.label || "Микрофон"),
      act: () => switchMic(d.deviceId),
    })));
  }

  async function toggleShare(){
    if (!active) return;
    if (active.sharing){ stopShare(true); return; }
    let stream;
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: {frameRate: {ideal: 15, max: 30},
                width: {max: 1920}, height: {max: 1080}},
        // Звук экрана/вкладки — если пользователь отметил галку в диалоге.
        audio: true});
    } catch { return; }  // отменил выбор экрана
    active.screenStream = stream;
    active.sharing = true;
    active.sharerId = window.ME.id;
    const track = stream.getVideoTracks()[0];
    const audio = stream.getAudioTracks()[0] || null;
    track.onended = () => stopShare(true);  // системная кнопка «Stop sharing»
    for (const p of active.pcs.values()){
      // Видео добавляется первым — приёмник по нему узнаёт стрим демонстрации.
      p.videoSender = p.pc.addTrack(track, stream);
      if (audio) p.screenAudioSender = p.pc.addTrack(audio, stream);
    }
    send({t: "state", muted: active.muted, sharing: true});
    setRemoteScreen(stream, "Ваш экран", true);
    renderDock();
  }

  function stopShare(notify){
    if (!active || !active.sharing) return;
    active.sharing = false;
    if (active.sharerId === window.ME.id){
      active.sharerId = 0;
      setRemoteScreen(null, "");
    }
    if (active.screenStream){
      for (const t of active.screenStream.getTracks()) t.stop();
      active.screenStream = null;
    }
    for (const p of active.pcs.values()){
      if (p.videoSender){
        try { p.pc.removeTrack(p.videoSender); } catch {}
        p.videoSender = null;
      }
      if (p.screenAudioSender){
        try { p.pc.removeTrack(p.screenAudioSender); } catch {}
        p.screenAudioSender = null;
      }
    }
    if (notify) send({t: "state", muted: active.muted, sharing: false});
    renderDock();
  }

  function leave(){
    if (!active) return;
    active.closing = true;
    try { active.recorder && active.recorder.state !== "inactive" &&
          active.recorder.stop(); } catch {}
    stopShare(false);
    for (const uid of [...active.pcs.keys()]) dropPeer(uid);
    try { active.ws && active.ws.close(); } catch {}
    for (const t of active.timers) clearInterval(t);
    for (const tr of active.mic.getTracks()) tr.stop();
    try { active.ctx.close(); } catch {}
    closeViewer();
    active = null;
    closeVolPop();
    removeDock();
    removePill();
    const audioBox = document.getElementById("call-audio");
    if (audioBox) audioBox.remove();
    SOUND.callLeave();
    APP.rerenderLists();
  }

  // ------------------------------------------------------------- док и пилюля

  function ensureDock(){
    let dock = document.getElementById("call-dock");
    if (!dock){
      dock = document.createElement("div");
      dock.id = "call-dock";
      const pane = document.getElementById("chat-pane");
      pane.insertBefore(dock, document.getElementById("messages"));
    }
    return dock;
  }

  function removeDock(){
    const dock = document.getElementById("call-dock");
    if (dock) dock.remove();
  }

  // Скрытые <audio> удалённых участников — вне дока: перерисовка innerHTML
  // не должна прерывать воспроизведение.
  function ensureAudioBox(){
    let box = document.getElementById("call-audio");
    if (!box){
      box = document.createElement("div");
      box.id = "call-audio";
      box.style.display = "none";
      document.body.appendChild(box);
    }
    return box;
  }

  function fmtClock(ms){
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60), h = Math.floor(m / 60);
    const mm = h ? String(m % 60).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(s % 60).padStart(2, "0");
  }

  function updateClock(){
    if (!active) return;
    const t = fmtClock(Date.now() - active.startedAt);
    const el = document.getElementById("call-clock");
    if (el) el.textContent = t;
    const pl = document.getElementById("call-pill-clock");
    if (pl) pl.textContent = t;
  }

  function tile(uid, name, muted){
    const speakClass = "";  // класс .speaking вешает pollSpeaking
    return `<div class="call-tile${speakClass}" data-uid="${uid}">
      <span class="call-ava" style="background:${CHAT.avatarColor(name)}">${CHAT.initials(name)}</span>
      <span class="call-name">${MD.esc(name)}</span>
      ${muted ? `<span class="call-muted">${ICON("mic-off", "xs")}</span>` : ""}
    </div>`;
  }

  // Поповер громкостей: контекстное меню не годится — оно закрывается по
  // любому клику, а слайдер надо таскать.
  function closeVolPop(){
    const pop = document.getElementById("call-vol-pop");
    if (pop) pop.remove();
    document.removeEventListener("mousedown", onVolPopOutside);
  }

  function onVolPopOutside(e){
    const pop = document.getElementById("call-vol-pop");
    const t = e.target instanceof Element ? e.target : null;
    if (pop && (!t || (!pop.contains(t) && !t.closest("#call-vol"))))
      closeVolPop();
  }

  function toggleVolPop(e){
    if (document.getElementById("call-vol-pop")){ closeVolPop(); return; }
    if (!active) return;
    const row = (label, key, max) => {
      const v = Math.round(getVol(key) * 100);
      return `<div class="cvp-row"><span class="cvp-name">${label}</span>
        <input type="range" min="0" max="${max}" value="${v}" data-key="${key}">
        <b class="cvp-val">${v}%</b></div>`;
    };
    let html = `<label class="cvp-toggle">
      <span class="cvp-name">${ICON("spark", "sm")} Шумоподавление</span>
      <input type="checkbox" id="cvp-denoise"${denoiseOn() ? " checked" : ""}>
      </label>`;
    html += row(`${ICON("mic", "sm")} Мой микрофон`, "mic", 200);
    for (const p of active.roster.values())
      html += row(MD.esc(p.name), "u" + p.id, 100);
    if (active.sharerId && active.sharerId !== window.ME.id)
      html += row(`${ICON("screen", "sm")} Демонстрация`, "screen", 100);
    const pop = document.createElement("div");
    pop.id = "call-vol-pop";
    pop.innerHTML = html;
    document.body.appendChild(pop);
    const r = e.currentTarget.getBoundingClientRect();
    pop.style.left = Math.min(r.left, window.innerWidth - pop.offsetWidth - 8) + "px";
    pop.style.top = (r.bottom + 6) + "px";
    pop.oninput = ev => {
      const inp = ev.target;
      if (!inp.dataset.key) return;
      const v = (+inp.value) / 100;
      localStorage.setItem("gd_call_vol_" + inp.dataset.key, String(v));
      applyVol(inp.dataset.key, v);
      inp.parentElement.querySelector(".cvp-val").textContent = inp.value + "%";
    };
    pop.querySelector("#cvp-denoise").onchange =
      ev => { setDenoise(ev.target.checked).catch(() => {}); };
    document.addEventListener("mousedown", onVolPopOutside);
  }

  function renderDock(){
    updatePlacement();
    closeVolPop();  // состав участников мог поменяться
    if (!active){
      // В открытом чате идёт чужой звонок — плашка «присоединиться».
      const cur = CHAT.currentId();
      const info = cur && callsMap[String(cur)];
      if (!info || !info.users.length || !secure){ removeDock(); return; }
      const dock = ensureDock();
      const names = info.users.map(peerName).join(", ");
      dock.className = "call-join-bar";
      dock.style.display = "";
      dock.innerHTML = `${ICON("phone")}<span>Идёт звонок · ${MD.esc(names)}</span>
        <button class="btn-primary" id="call-join-btn">Присоединиться</button>`;
      dock.querySelector("#call-join-btn").onclick = joinCurrent;
      return;
    }
    const dock = ensureDock();
    dock.className = "";
    const selfTile = tile(window.ME.id, window.ME.display_name, active.muted);
    const peerTiles = [...active.roster.values()]
      .map(p => tile(p.id, p.name, p.muted)).join("");
    const sharerName = active.sharerId
      ? (active.sharerId === window.ME.id ? "Ваш экран"
         : (active.roster.get(active.sharerId) || {}).name || "")
      : "";
    dock.innerHTML = `
      <div id="call-tiles">${selfTile}${peerTiles}</div>
      <div id="call-screenbox" style="display:none">
        <video id="call-screen-video" autoplay muted playsinline></video>
        <button id="call-screen-expand" title="Развернуть">${ICON("expand", "sm")}</button>
        <span id="call-screen-name"></span>
      </div>
      <div id="call-ctl">
        <span id="call-clock">${fmtClock(Date.now() - active.startedAt)}</span>
        <button id="call-mute" class="${active.muted ? "danger-on" : ""}"
          title="${active.muted ? "Включить микрофон" : "Выключить микрофон"}">
          ${ICON(active.muted ? "mic-off" : "mic")}</button>
        <button id="call-mic-pick" title="Выбрать микрофон">${ICON("chevron-down", "sm")}</button>
        <button id="call-vol" title="Громкость">${ICON("volume")}</button>
        <button id="call-share" class="${active.sharing ? "accent-on" : ""}"
          title="${active.sharing ? "Остановить демонстрацию" : "Демонстрация экрана"}">
          ${ICON(active.sharing ? "screen-off" : "screen")}</button>
        <button id="call-leave" title="Завершить">${ICON("phone-off")}</button>
      </div>`;
    for (const [uid, p] of active.pcs)
      if (p.pc.connectionState === "failed"){
        const el = dock.querySelector(`.call-tile[data-uid="${uid}"]`);
        if (el) el.classList.add("unreachable");
      }
    dock.querySelector("#call-mute").onclick = toggleMute;
    dock.querySelector("#call-mic-pick").onclick = pickMic;
    dock.querySelector("#call-vol").onclick = toggleVolPop;
    dock.querySelector("#call-share").onclick = toggleShare;
    dock.querySelector("#call-leave").onclick = leave;
    dock.querySelector("#call-screen-expand").onclick = () => openViewer();
    dock.querySelector("#call-screenbox").ondblclick = () => openViewer();
    if (active.currentScreen) setRemoteScreen(active.currentScreen, sharerName, active.sharerId === window.ME.id);
  }

  function setRemoteScreen(stream, name, isMine){
    if (active) active.currentScreen = stream;
    const box = document.getElementById("call-screenbox");
    if (!box) return;
    const video = document.getElementById("call-screen-video");
    if (!stream){
      box.style.display = "none";
      if (video) video.srcObject = null;
      const sa = document.getElementById("call-screen-audio");
      if (sa) sa.srcObject = null;
      if (active) active.screenSid = null;
      closeViewer();
      return;
    }
    box.style.display = "";
    video.srcObject = stream;
    video.play().catch(() => {});
    document.getElementById("call-screen-name").textContent = name || "";
  }

  function pollSpeaking(){
    if (!active) return;
    const buf = new Uint8Array(256);
    for (const [uid, an] of active.analysers){
      let el = document.querySelector(`.call-tile[data-uid="${uid}"]`);
      if (!el) continue;
      an.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++){
        const d = (buf[i] - 128) / 128;
        sum += d * d;
      }
      const speaking = Math.sqrt(sum / buf.length) > 0.04 &&
                       !(uid === window.ME.id && active.muted);
      el.classList.toggle("speaking", speaking);
    }
  }

  // Пилюля: звонок жив, но открыт другой чат.
  function ensurePill(){
    let pill = document.getElementById("call-pill");
    if (!pill){
      pill = document.createElement("div");
      pill.id = "call-pill";
      document.body.appendChild(pill);
    }
    return pill;
  }

  function removePill(){
    const pill = document.getElementById("call-pill");
    if (pill) pill.remove();
  }

  function updatePlacement(){
    if (!active){ removePill(); return; }
    const inOwnChat = CHAT.currentId() === active.chat.id;
    const dock = ensureDock();
    dock.style.display = inOwnChat ? "" : "none";
    if (inOwnChat){ removePill(); return; }
    const pill = ensurePill();
    pill.innerHTML = `${ICON("phone", "sm")}
      <span>${MD.esc(active.chat.title)}</span>
      <span id="call-pill-clock">${fmtClock(Date.now() - active.startedAt)}</span>
      <button id="call-pill-mute">${ICON(active.muted ? "mic-off" : "mic", "sm")}</button>`;
    pill.querySelector("#call-pill-mute").onclick = e => {
      e.stopPropagation(); toggleMute();
    };
    pill.onclick = () => APP.selectChat(active.chat.id);
  }

  // ------------------------------------------------------------- просмотр экрана

  function openViewer(){
    if (!active || !active.currentScreen) return;
    if (!viewerEl){
      viewerEl = document.createElement("div");
      viewerEl.id = "call-viewer";
      viewerEl.innerHTML = `<div class="cv-bar">
          <span id="cv-name"></span>
          <button id="cv-close" title="Закрыть (Esc)">${ICON("x")}</button>
        </div>
        <div class="cv-stage"><video autoplay muted playsinline></video></div>`;
      document.body.appendChild(viewerEl);
      viewerEl.querySelector("#cv-close").onclick = closeViewer;
      document.addEventListener("keydown", e => {
        if (e.key === "Escape" && viewerEl && viewerEl.style.display !== "none")
          closeViewer();
      });
    }
    viewerEl.style.display = "flex";
    viewerEl.querySelector("#cv-name").textContent =
      document.getElementById("call-screen-name")?.textContent || "";
    const v = viewerEl.querySelector("video");
    v.srcObject = active.currentScreen;
    v.play().catch(() => {});
  }

  function closeViewer(){
    if (viewerEl){
      viewerEl.style.display = "none";
      viewerEl.querySelector("video").srcObject = null;
    }
    // Окно звонка перекрывало чат (CALL.isOpen в chatVisible) — по закрытии
    // помечаем накопившееся, как это делает закрытие доски задач.
    if (typeof CHAT !== "undefined" && CHAT.markVisible) CHAT.markVisible();
  }

  // ------------------------------------------------------------- дозвон

  function peerName(uid){
    if (uid === window.ME.id) return window.ME.display_name;
    const u = (window.USERS || []).find(x => x.id === uid);
    return u ? u.display_name : "?";
  }

  function showIncoming(chat, fromUid){
    if (incoming || active) return;
    const el = document.createElement("div");
    el.id = "call-incoming";
    el.innerHTML = `${ICON("phone")}
      <span>Входящий звонок · <b>${MD.esc(peerName(fromUid))}</b></span>
      <button class="btn-primary" id="ci-join">Присоединиться</button>
      <button id="ci-dismiss">Отклонить</button>`;
    document.body.appendChild(el);
    incoming = {chatId: chat.id, el};
    SOUND.ring();
    if (window.isSecureContext && "Notification" in window &&
        Notification.permission === "granted"){
      try { new Notification("Входящий звонок", {body: peerName(fromUid)}); }
      catch {}
    }
    el.querySelector("#ci-join").onclick = async () => {
      const id = incoming.chatId;
      dismissIncoming();
      await APP.selectChat(id);
      joinCurrent();
    };
    el.querySelector("#ci-dismiss").onclick = dismissIncoming;
  }

  function dismissIncoming(){
    if (!incoming) return;
    incoming.el.remove();
    incoming = null;
    SOUND.ringStop();
  }

  function joinCurrent(){
    const id = CHAT.currentId();
    const chat = id && APP.chatById(id);
    if (chat) join(chat);
  }

  // ------------------------------------------------------------- интеграция

  // Из поллинга: карта активных звонков в видимых чатах.
  function onCalls(map){
    const prev = callsMap;
    callsMap = map || {};
    const changed = JSON.stringify(prev) !== JSON.stringify(callsMap);
    if (!changed) return;
    APP.rerenderLists();
    if (!active) renderDock();
    // Дозвон: новый звонок в ЛС, меня в нём нет.
    if (!active && secure){
      for (const [cid, info] of Object.entries(callsMap)){
        if (prev[cid]) continue;
        if (info.users.includes(window.ME.id)) continue;
        const chat = APP.chatById(+cid);
        if (chat && chat.kind === "dm") showIncoming(chat, info.started_by);
      }
    }
    // Звонок, на который звали, кончился (или я уже в нём с другого места).
    if (incoming){
      const info = callsMap[String(incoming.chatId)];
      if (!info || !info.users.length || info.users.includes(window.ME.id))
        dismissIncoming();
    }
  }

  // Из CHAT.open(): показать кнопку и плашку «идёт звонок».
  function onChatOpen(chat){
    const btn = document.getElementById("btn-call");
    if (btn){
      btn.style.display = chat.kind !== "agent" && secure ? "" : "none";
      btn.onclick = () => active && active.chat.id === chat.id
        ? null : join(chat);
    }
    if (incoming && incoming.chatId === chat.id) dismissIncoming();
    renderDock();
  }

  const teardown = () => leave();

  return {
    onCalls, onChatOpen,
    hasCall: chatId => {
      const info = callsMap[String(chatId)];
      return !!(info && info.users.length);
    },
    isOpen: () => !!(viewerEl && viewerEl.style.display !== "none"),
    inCall: () => !!active,
  };
})();
