// UI sounds for Big Picture, synthesized with Web Audio -- no audio files, so
// nothing to license and nothing to load. Each sound is a few milliseconds of
// shaped oscillators/noise through one master gain (the volume setting) and a
// gentle low-pass so nothing is harsh on TV speakers or headphones.
//
// Sounds follow console-launcher conventions: a soft tick when focus moves, a
// brighter rising pair to confirm, a falling pair to go back, a whoosh for tab
// switches, a muted bump at the end of a row, a low buzz for errors, and a
// short chord when a game launches.

export class Sounds {
  constructor() {
    this.enabled = true;
    this.volume = 0.5;
    this._ctx = null;
    this._last = {};
  }

  _audio() {
    if (!this._ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      this._ctx = new AC();
      this._master = this._ctx.createGain();
      const tone = this._ctx.createBiquadFilter();
      tone.type = 'lowpass';
      tone.frequency.value = 7000;
      this._master.connect(tone);
      tone.connect(this._ctx.destination);
      this._noise = this._ctx.createBuffer(1, this._ctx.sampleRate * 0.3, this._ctx.sampleRate);
      const d = this._noise.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    if (this._ctx.state === 'suspended') this._ctx.resume();
    this._master.gain.value = this.volume * 0.9;
    return this._ctx;
  }

  // One enveloped oscillator: freq can glide from f0 to f1.
  _tone(type, f0, f1, start, dur, peak) {
    const c = this._ctx, o = c.createOscillator(), g = c.createGain();
    o.type = type;
    o.frequency.setValueAtTime(f0, start);
    if (f1 && f1 !== f0) o.frequency.exponentialRampToValueAtTime(f1, start + dur);
    g.gain.setValueAtTime(0.0001, start);
    g.gain.exponentialRampToValueAtTime(peak, start + Math.min(0.008, dur / 4));
    g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    o.connect(g); g.connect(this._master);
    o.start(start); o.stop(start + dur + 0.02);
  }

  _whoosh(start, dur, peak, from, to) {
    const c = this._ctx, s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain();
    s.buffer = this._noise;
    f.type = 'bandpass'; f.Q.value = 1.4;
    f.frequency.setValueAtTime(from, start);
    f.frequency.exponentialRampToValueAtTime(to, start + dur);
    g.gain.setValueAtTime(0.0001, start);
    g.gain.exponentialRampToValueAtTime(peak, start + dur * 0.35);
    g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    s.connect(f); f.connect(g); g.connect(this._master);
    s.start(start); s.stop(start + dur + 0.02);
  }

  play(name) {
    if (!this.enabled || this.volume <= 0) return;
    // Rapid repeats (holding a direction) shouldn't machine-gun the same sound.
    const nowMs = performance.now();
    if (nowMs - (this._last[name] || 0) < 45) return;
    this._last[name] = nowMs;
    const c = this._audio();
    if (!c) return;
    const t = c.currentTime + 0.005;
    switch (name) {
      case 'move':
        this._tone('sine', 1320, 1180, t, 0.045, 0.11);
        this._tone('triangle', 2640, 2400, t, 0.02, 0.025);
        break;
      case 'select':
        this._tone('triangle', 740, 740, t, 0.07, 0.16);
        this._tone('triangle', 1110, 1110, t + 0.055, 0.11, 0.16);
        break;
      case 'back':
        this._tone('triangle', 880, 880, t, 0.06, 0.13);
        this._tone('triangle', 587, 587, t + 0.05, 0.1, 0.13);
        break;
      case 'tab':
        this._whoosh(t, 0.16, 0.09, 900, 3200);
        this._tone('sine', 1560, 1560, t + 0.02, 0.05, 0.06);
        break;
      case 'toggle':
        this._tone('square', 1500, 1500, t, 0.018, 0.035);
        this._tone('sine', 980, 1240, t, 0.06, 0.1);
        break;
      case 'bump':
        this._tone('sine', 190, 140, t, 0.08, 0.16);
        break;
      case 'error':
        this._tone('square', 196, 196, t, 0.09, 0.05);
        this._tone('square', 185, 185, t + 0.11, 0.12, 0.05);
        break;
      case 'open':
        this._whoosh(t, 0.22, 0.06, 500, 2400);
        this._tone('sine', 660, 990, t, 0.14, 0.08);
        break;
      case 'close':
        this._whoosh(t, 0.18, 0.05, 2400, 600);
        break;
      case 'success':
        [659, 830, 988].forEach((f, i) => this._tone('triangle', f, f, t + i * 0.07, 0.16, 0.12));
        break;
      case 'launch':
        [523, 659, 784, 1047].forEach((f, i) => this._tone('triangle', f, f, t + i * 0.06, 0.45 - i * 0.05, 0.12));
        this._whoosh(t + 0.05, 0.5, 0.05, 400, 5000);
        break;
      case 'boot':
        [392, 523, 659, 784].forEach((f, i) => this._tone('sine', f, f, t + i * 0.09, 0.9 - i * 0.12, 0.08));
        break;
      default:
        break;
    }
  }
}
