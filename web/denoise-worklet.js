// Шумодав микрофона (#102): RNNoise в AudioWorklet плюс мягкий гейт по
// речевой вероятности, которую RNNoise отдаёт на каждом кадре. Модуль wasm
// приезжает уже скомпилированным в processorOptions — в worklet-скоупе нет
// fetch. Модуля нет (не загрузился, частота не 48 кГц, браузер не умеет
// клонировать WebAssembly.Module) — остаётся один гейт, решающий по энергии;
// из-за шумодава звонок падать не должен ни при каком раскладе.

const FRAME = 480;    // RNNoise: ровно 10 мс при 48 кГц
const SCALE = 32768;  // RNNoise ждёт float в шкале PCM16, WebAudio даёт -1..1
// Речевая вероятность на старте фразы запаздывает на кадр-другой — сети нужен
// контекст. Поэтому наружу отдаём кадр, отставший на LOOKAHEAD: решение о нём
// принято по будущему звуку, и гейт успевает открыться до первого слога.
// Цена — задержка тракта (LOOKAHEAD + 1) * 10 мс; на трёх кадрах срезание
// атак падает вчетверо против одного, дальше выигрыш уже не окупает задержку.
const LOOKAHEAD = 3;

// Пороги низкие намеренно: вероятность у RNNoise разложена по краям — в
// паузах около нуля, на речи выше 0.7, — поэтому высокий порог только режет
// начала фраз, ничего не добавляя к подавлению (мерялось на записи звонка
// с подмешанным фоном и щелчками).
const DEF = {
  openP: 0.2,      // речевая вероятность выше — гейт открывается
  closeP: 0.08,    // ниже — закрывается; зазор, чтобы не дребезжал
  floorDb: -20,    // пауза приглушается, а не гасится: ноль слышен как обрыв
  attackMs: 5,
  releaseMs: 180,
  holdMs: 300,     // держим открытым после спада — не режем окончания фраз
  riseDb: 8,       // фолбэк: насколько кадр громче шумового пола = речь
};

// Имена экспортов минифицированы emscripten-ом, поэтому привязаны к
// вендоренному бинарнику (@jitsi/rnnoise-wasm 0.2.1). Сигнатуры сверены:
// j(state, out, in) -> f32, и этот f32 — речевая вероятность кадра.
const EXP = {mem: "c", ctors: "d", create: "f", destroy: "h",
             malloc: "g", free: "i", process: "j"};

class Rnnoise {
  constructor(mod){
    const inst = new WebAssembly.Instance(mod, {a: {
      // Единственные импорты сборки — emscripten_resize_heap и
      // emscripten_memcpy_big. На 480-сэмпловых кадрах RNNoise память не
      // растит, но без заглушек модуль не инстанцируется.
      a: n => {
        try {
          this.mem.grow(Math.ceil((n - this.mem.buffer.byteLength) / 65536));
          this.sync();
          return 1;
        } catch { return 0; }
      },
      b: (dst, src, n) => { this.u8.copyWithin(dst, src, src + n); },
    }});
    const e = inst.exports;
    for (const k of Object.values(EXP))
      if (!e[k]) throw new Error("нет экспорта " + k);
    this.e = e;
    this.mem = e[EXP.mem];
    this.sync();
    e[EXP.ctors]();
    this.state = e[EXP.create](0);
    this.inPtr = e[EXP.malloc](FRAME * 4);
    this.outPtr = e[EXP.malloc](FRAME * 4);
    if (!this.state || !this.inPtr || !this.outPtr)
      throw new Error("не выделилась память");
  }

  sync(){
    this.u8 = new Uint8Array(this.mem.buffer);
    this.f32 = new Float32Array(this.mem.buffer);
  }

  run(inp, out){
    const f = this.f32, a = this.inPtr >> 2, b = this.outPtr >> 2;
    for (let i = 0; i < FRAME; i++) f[a + i] = inp[i] * SCALE;
    const vad = this.e[EXP.process](this.state, this.outPtr, this.inPtr);
    for (let i = 0; i < FRAME; i++) out[i] = f[b + i] / SCALE;
    return vad;
  }
}

class MicDenoise extends AudioWorkletProcessor {
  constructor(opt){
    super();
    const o = (opt && opt.processorOptions) || {};
    this.cfg = Object.assign({}, DEF, o.gate || {});
    this.on = o.on !== false;
    this.meter = !!o.meter;   // отчёты об уровне нужны только тестовой странице
    this.rn = null;
    let err = "";
    if (o.wasm){
      try { this.rn = new Rnnoise(o.wasm); }
      catch (e){ err = String(e && e.message || e); }
    }
    this.port.postMessage({t: "ready", rnnoise: !!this.rn, err});

    this.inBuf = new Float32Array(FRAME);
    this.outBuf = new Float32Array(FRAME);
    this.ring = [];
    for (let i = 0; i <= LOOKAHEAD; i++) this.ring.push(new Float32Array(FRAME));
    this.ringPos = 0;
    this.inLen = 0;
    this.outPos = FRAME;   // первый кадр ещё не набран — наружу идёт тишина
    this.gain = 1;
    this.open = true;      // пока гейт не осмотрелся, лучше пропустить лишнее
    this.hold = 0;
    this.floorDb = -60;
    this.frames = 0;

    this.port.onmessage = e => {
      const d = e.data || {};
      if (typeof d.on === "boolean") this.on = d.on;
      if (d.gate) Object.assign(this.cfg, d.gate);
    };
  }

  coef(ms){
    return 1 - Math.exp(-1 / Math.max(1, ms / 1000 * sampleRate));
  }

  // Фолбэк без RNNoise: шумовой пол ползёт вверх медленно и падает сразу,
  // кадр громче пола на riseDb считается речью. Наружу — 0 или 1, чтобы
  // гейт жил по тем же порогам, что и с настоящей вероятностью.
  rmsVad(){
    let s = 0;
    for (let i = 0; i < FRAME; i++) s += this.inBuf[i] * this.inBuf[i];
    const db = 10 * Math.log10(s / FRAME + 1e-12);
    const rise = FRAME / sampleRate * 6;
    this.floorDb = db < this.floorDb ? db : Math.min(db, this.floorDb + rise);
    return db > this.floorDb + this.cfg.riseDb ? 1 : 0;
  }

  frame(){
    const cfg = this.cfg;
    const buf = this.ring[this.ringPos];
    let vad = 0;
    if (this.on && this.rn) vad = this.rn.run(this.inBuf, buf);
    else {
      buf.set(this.inBuf);
      if (this.on) vad = this.rmsVad();
    }
    let target = 1;
    if (this.on){
      const speak = vad >= (this.open ? cfg.closeP : cfg.openP);
      if (speak){ this.open = true; this.hold = cfg.holdMs; }
      else if (this.hold > 0) this.hold -= FRAME / sampleRate * 1000;
      else this.open = false;
      if (!this.open) target = Math.pow(10, cfg.floorDb / 20);
    } else {
      this.open = true;
      this.hold = 0;
    }
    this.ringPos = (this.ringPos + 1) % this.ring.length;
    const src = this.ring[this.ringPos];   // кадр, отставший на LOOKAHEAD
    const k = this.coef(target > this.gain ? cfg.attackMs : cfg.releaseMs);
    for (let i = 0; i < FRAME; i++){
      this.gain += (target - this.gain) * k;
      this.outBuf[i] = src[i] * this.gain;
    }
    if (this.meter && ++this.frames % 5 === 0)
      this.port.postMessage({t: "level", vad, gain: this.gain,
                             open: this.open});
  }

  process(inputs, outputs){
    const inp = inputs[0], outp = outputs[0];
    const x = inp && inp[0];
    const y = outp && outp[0];
    if (!y) return true;
    for (let i = 0; i < y.length; i++){
      this.inBuf[this.inLen++] = x ? x[i] : 0;
      if (this.inLen === FRAME){
        this.frame();
        this.inLen = 0;
        this.outPos = 0;
      }
      y[i] = this.outPos < FRAME ? this.outBuf[this.outPos++] : 0;
    }
    for (let c = 1; c < outp.length; c++) outp[c].set(y);
    return true;
  }
}

registerProcessor("mic-denoise", MicDenoise);
