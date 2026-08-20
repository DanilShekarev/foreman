// Звуки интерфейса: синтез Web Audio, без файлов. Набор утверждён на
// sounds-test.html: msg-blip / men-dingdong / agent-arp / send-pop / err-double.
// Автоплей-политика браузера: до первого клика/клавиши контекст suspended —
// такие звуки молча пропускаются (не копим очередь догоняющих бипов).
const SOUND = (() => {
  let ctx = null;
  let enabled = localStorage.getItem("gd_sound") !== "off";
  let vol = Math.min(1, Math.max(0.05,
    +(localStorage.getItem("gd_sound_vol") || 0.5)));

  function ready(){
    if (!enabled) return null;
    if (!ctx){
      try { ctx = new (window.AudioContext || window.webkitAudioContext)(); }
      catch (e) { return null; }
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx.state === "running" ? ctx : null;
  }

  // Разблокировка первым жестом пользователя: контекст СОЗДАЁТСЯ прямо в
  // обработчике жеста (тогда он сразу running) — иначе первый входящий звук
  // создавал бы его вне жеста, заставал suspended и молча глотался.
  ["pointerdown", "keydown"].forEach(ev =>
    document.addEventListener(ev, () => {
      if (!enabled) return;
      if (!ctx){
        try { ctx = new (window.AudioContext || window.webkitAudioContext)(); }
        catch (e) { return; }
      }
      if (ctx.state === "suspended") ctx.resume();
    }, {capture: true}));

  function tone(freq, {type = "sine", at = 0, dur = 0.15, v = 1,
                       sweep = null} = {}){
    const a = ready();
    if (!a) return;
    const t0 = a.currentTime + at;
    const o = a.createOscillator(), g = a.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t0);
    if (sweep) o.frequency.exponentialRampToValueAtTime(sweep, t0 + dur);
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(v * vol, t0 + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(a.destination);
    o.start(t0); o.stop(t0 + dur + 0.05);
  }

  const S = {
    // входящее сообщение — «Блип»: два тона вверх
    msg(){ tone(660, {dur: .06, v: .4});
           tone(990, {at: .07, dur: .09, v: .4}); },
    // упоминание @тебя — «Динь-дон»
    mention(){ tone(1568, {dur: .25, v: .35});
               tone(1175, {at: .16, dur: .4, v: .35}); },
    // агент закончил ход — «Арпеджио»
    agent(){ tone(523, {dur: .12, v: .3});
             tone(659, {at: .09, dur: .12, v: .3});
             tone(784, {at: .18, dur: .3, v: .3}); },
    // отправка своего сообщения — «Поп вверх»
    send(){ tone(520, {dur: .08, v: .4, sweep: 880}); },
    // ошибка — «Двойной вниз»
    error(){ tone(311, {type: "triangle", dur: .12, v: .3});
             tone(233, {type: "triangle", at: .14, dur: .25, v: .3}); },
  };

  return {
    ...S,
    isOn: () => enabled,
    volume: () => vol,
    toggle(){
      enabled = !enabled;
      localStorage.setItem("gd_sound", enabled ? "on" : "off");
      if (enabled) S.msg();  // подтверждение включения
    },
    bumpVol(d){
      vol = Math.min(1, Math.max(0.05, Math.round((vol + d) * 20) / 20));
      localStorage.setItem("gd_sound_vol", String(vol));
      S.msg();  // сразу слышно новую громкость
    },
  };
})();
