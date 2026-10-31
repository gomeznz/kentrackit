// AudioWorklet processors for the dispatch console (runs on the browser's audio thread).
// Radio audio is 8 kHz 16-bit mono in 20 ms frames (160 samples) both ways.

const OUT_RATE = 8000, FRAME = 160;

// Microphone -> 8 kHz frames. A simple low-pass (two one-pole stages at ~3.4 kHz) then
// fractional-step resampling; good enough for speech going out over a 6.25 kHz radio channel.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / OUT_RATE; this.pos = 1;
    const a = Math.exp(-2 * Math.PI * 3400 / sampleRate);
    this.a = a; this.y1 = 0; this.y2 = 0; this.prev = 0;
    this.buf = new Int16Array(FRAME); this.n = 0; this.peak = 0; this.gain = 1;
    this.port.onmessage = (e) => { if (e.data && typeof e.data.gain === "number") this.gain = e.data.gain; };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.y1 = (1 - this.a) * ch[i] * this.gain + this.a * this.y1;
      this.y2 = (1 - this.a) * this.y1 + this.a * this.y2;
      const cur = this.y2;
      // output samples fall every `step` input samples; emit each one that lies between prev and cur
      while (this.pos <= 1) {
        const v = Math.max(-1, Math.min(1, this.prev + (cur - this.prev) * this.pos));
        this.peak = Math.max(this.peak, Math.abs(v));
        this.buf[this.n++] = v * 32767;
        if (this.n === FRAME) {
          this.port.postMessage({ pcm: this.buf.buffer, peak: this.peak }, [this.buf.buffer]);
          this.buf = new Int16Array(FRAME); this.n = 0; this.peak = 0;
        }
        this.pos += this.step;
      }
      this.pos -= 1;
      this.prev = cur;
    }
    return true;
  }
}

// 8 kHz frames from the server -> speakers, with a small jitter buffer (starts after ~120 ms queued).
class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = []; this.cur = null; this.ci = 0; this.pos = 0; this.started = false;
    this.step = OUT_RATE / sampleRate; this.last = 0; this.next = 0; this.vol = 1; this.peak = 0; this.cnt = 0;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.pcm) { this.q.push(new Int16Array(d.pcm)); if (this.q.length > 50) this.q.splice(0, this.q.length - 10); }
      if (typeof d.vol === "number") this.vol = d.vol;
      if (d.flush) { this.q = []; this.cur = null; this.started = false; }
    };
  }
  sample() {
    if (!this.cur || this.ci >= this.cur.length) {
      this.cur = this.q.shift() || null; this.ci = 0;
      if (!this.cur) { this.started = false; return 0; }
    }
    return this.cur[this.ci++] / 32768;
  }
  process(_, outputs) {
    const out = outputs[0][0];
    if (!this.started && this.q.length >= 6) this.started = true;
    for (let i = 0; i < out.length; i++) {
      if (!this.started) { out[i] = 0; continue; }
      this.pos += this.step;
      while (this.pos >= 1) { this.pos -= 1; this.last = this.next; this.next = this.sample(); }
      const v = (this.last + (this.next - this.last) * this.pos) * this.vol;
      out[i] = v; this.peak = Math.max(this.peak, Math.abs(v));
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    if (++this.cnt % 8 === 0) { this.port.postMessage({ peak: this.peak }); this.peak = 0; }
    return true;
  }
}

registerProcessor("kt-mic", MicProcessor);
registerProcessor("kt-player", PlayerProcessor);
