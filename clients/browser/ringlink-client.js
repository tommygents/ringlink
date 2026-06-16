// ringlink-client.js — reference browser/JS client for the ringlink protocol.
//
// Runs UNMODIFIED in a browser and in Node 22+ (both expose a global WebSocket).
// All rendering is injected via callbacks, so the same core powers index.html
// (renders to the DOM) and contract-check.js (asserts, headless). This file is
// the deliverable the spike validates — it is carried forward as the v1 client,
// not rewritten.
//
// The whole protocol is consumed by one switch with no per-message special-casing
// beyond the contract itself (a session_id change means the server restarted, so
// derived state is dropped — design [C1]/[C2]).

const EMPTY_STATE = {
  flex: 0, squeeze: 0, pull: 0,
  lean: { pitch: 0, roll: 0 },
  gait: "rest", squatting: false, squat_reps: 0,
  buttons: [], stick: [0, 0],
};

export class RingLinkClient {
  constructor(url, handlers = {}) {
    this.url = url;
    this.h = handlers; // {onHello, onState, onCalibrating, onStatus, onError, onConnection}
    this.sessionId = null;
    this.state = { ...EMPTY_STATE };
    this.ws = null;
    this._closed = false;
    this._retry = 0;
  }

  connect() {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    // Reconnect must fire whether the socket errors OR closes, exactly once per
    // socket: a browser emits error+close on a dropped connection, but a failed
    // *initial* connect (server not up yet) emits only error in Node/undici.
    // Handling both keeps reconnect working before the server exists and after it dies.
    let down = false;
    const onDown = () => {
      if (down) return;
      down = true;
      this.h.onConnection?.("closed");
      if (!this._closed) setTimeout(() => this.connect(), Math.min(1000, 100 * ++this._retry));
    };
    ws.onopen = () => { this._retry = 0; this.h.onConnection?.("open"); };
    ws.onmessage = (ev) => this._dispatch(JSON.parse(ev.data));
    ws.onerror = onDown;
    ws.onclose = onDown;
    return this;
  }

  _dispatch(msg) {
    switch (msg.type) {
      case "hello":
        // A new session_id means the server restarted: drop derived state.
        if (this.sessionId && msg.session_id !== this.sessionId) this.state = { ...EMPTY_STATE };
        this.sessionId = msg.session_id;
        this.h.onHello?.(msg);
        break;
      case "frame":
        this.state = msg.state;
        this.h.onState?.(this.state, msg.events || []);
        break;
      case "calibrating": this.h.onCalibrating?.(msg); break;
      case "status":      this.h.onStatus?.(msg.pads); break;
      case "error":       this.h.onError?.(msg); break;
    }
  }

  calibrate(pad = "R", pose = "rest") {
    this.ws?.send(JSON.stringify({ type: "calibrate", pad, pose, seconds: 3 }));
  }

  close() { this._closed = true; this.ws?.close(); }
}
