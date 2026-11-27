// Input for Big Picture: one place that turns keyboard, mouse and any gamepad
// into the same small set of events, and tracks WHICH device the user touched
// last so the on-screen button hints always match what is in their hands.
//
//   device: 'keyboard' | 'mouse' | 'xbox' | 'playstation' | 'nintendo' | 'gamepad'
//
// Gamepads use the browser's "standard" layout, so buttons are addressed by
// position and only their printed label differs per controller family:
//   0 bottom, 1 right, 2 left, 3 top, 4 LB/L1, 5 RB/R1, 8 View/Create/-,
//   9 Menu/Options/+, 12 up, 13 down, 14 left, 15 right; axes 0/1 = left stick.

export const FACE_LABELS = {
  xbox:        { bottom: 'A', right: 'B', left: 'X', top: 'Y', lb: 'LB', rb: 'RB', view: 'View', menu: 'Menu' },
  playstation: { bottom: '✕', right: '○', left: '□', top: '△', lb: 'L1', rb: 'R1', view: 'Create', menu: 'Options' },
  nintendo:    { bottom: 'B', right: 'A', left: 'Y', top: 'X', lb: 'L', rb: 'R', view: '−', menu: '+' },
  gamepad:     { bottom: 'A', right: 'B', left: 'X', top: 'Y', lb: 'LB', rb: 'RB', view: 'View', menu: 'Menu' },
};

export function padFamily(id) {
  const s = String(id || '').toLowerCase();
  if (/054c|sony|playstation|dualshock|dualsense|wireless controller/.test(s)) return 'playstation';
  if (/057e|nintendo|switch|joy-con|pro controller/.test(s)) return 'nintendo';
  if (/045e|xbox|xinput|microsoft/.test(s)) return 'xbox';
  return 'gamepad';
}

// Held-direction repeat, like a console menu: one step on press, a pause, then
// a steady fast repeat.
const REPEAT_DELAY = 380;
const REPEAT_RATE = 85;
const DEADZONE = 0.5;

export class InputManager {
  constructor() {
    this.device = 'keyboard';
    this._deviceListeners = [];
    this._handlers = {};
    this._prevButtons = {};
    this._held = { x: { dir: 0, next: 0 }, y: { dir: 0, next: 0 } };

    window.addEventListener('keydown', () => this._setDevice('keyboard'), true);
    window.addEventListener('mousedown', () => this._setDevice('mouse'), true);
    window.addEventListener('wheel', () => this._setDevice('mouse'), { passive: true, capture: true });
    window.addEventListener('mousemove', (e) => {
      // Chromium fires synthetic zero-movement mousemoves when content moves
      // under a still cursor; those must not flip a controller user to "mouse".
      if (Math.abs(e.movementX) + Math.abs(e.movementY) > 3) this._setDevice('mouse');
    }, true);

    this._poll = this._poll.bind(this);
    requestAnimationFrame(this._poll);
  }

  get isPad() { return !(this.device === 'keyboard' || this.device === 'mouse'); }
  /** 'keyboard' | 'gamepad' -- coarse view kept for simple callers. */
  get mode() { return this.isPad ? 'gamepad' : 'keyboard'; }
  /** Labels for the buttons of the controller currently in use. */
  get labels() { return FACE_LABELS[this.isPad ? this.device : 'xbox']; }

  onModeChange(cb) { this._deviceListeners.push(cb); }
  /** name: up|down|left|right|confirm|back|button<N> */
  on(name, cb) { (this._handlers[name] = this._handlers[name] || []).push(cb); }
  onConfirm(cb) { this.on('confirm', cb); }
  onBack(cb) { this.on('back', cb); }
  onUp(cb) { this.on('up', cb); }
  onDown(cb) { this.on('down', cb); }
  onLeft(cb) { this.on('left', cb); }
  onRight(cb) { this.on('right', cb); }
  onShoulderL(cb) { this.on('button4', cb); }
  onShoulderR(cb) { this.on('button5', cb); }
  onButton(index, cb) { this.on('button' + index, cb); }

  _fire(name, repeat) { (this._handlers[name] || []).forEach((cb) => cb({ repeat: !!repeat })); }

  _setDevice(device) {
    if (this.device === device) return;
    this.device = device;
    this._deviceListeners.forEach((cb) => cb(device));
  }

  _poll(now) {
    requestAnimationFrame(this._poll);
    if (document.hidden) return;
    const pads = navigator.getGamepads ? navigator.getGamepads() : [];
    let pad = null;
    for (const p of pads) { if (p && p.connected) { pad = p; break; } }
    if (!pad) return;

    const pressed = (i) => !!(pad.buttons[i] && pad.buttons[i].pressed);
    for (let i = 0; i < pad.buttons.length; i++) {
      const down = pressed(i);
      if (down && !this._prevButtons[i] && !(i >= 12 && i <= 15)) {
        this._setDevice(padFamily(pad.id));
        if (i === 0) this._fire('confirm');
        else if (i === 1) this._fire('back');
        else this._fire('button' + i);
      }
      this._prevButtons[i] = down;
    }

    const ax = pad.axes[0] || 0, ay = pad.axes[1] || 0;
    const dx = pressed(14) ? -1 : pressed(15) ? 1 : ax < -DEADZONE ? -1 : ax > DEADZONE ? 1 : 0;
    const dy = pressed(12) ? -1 : pressed(13) ? 1 : ay < -DEADZONE ? -1 : ay > DEADZONE ? 1 : 0;
    this._axis('x', dx, dx < 0 ? 'left' : 'right', pad, now);
    this._axis('y', dy, dy < 0 ? 'up' : 'down', pad, now);
  }

  _axis(axis, dir, name, pad, now) {
    const h = this._held[axis];
    if (!dir) { h.dir = 0; return; }
    this._setDevice(padFamily(pad.id));
    if (h.dir !== dir) { h.dir = dir; h.next = now + REPEAT_DELAY; this._fire(name, false); }
    else if (now >= h.next) { h.next = now + REPEAT_RATE; this._fire(name, true); }
  }
}
