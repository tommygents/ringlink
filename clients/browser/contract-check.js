// contract-check.js — headless Phase 2 acceptance for the browser/JS client.
//
// Proves a non-Python client consumes the ringlink JSON contract over a real
// WebSocket, with NO shared types and NO hardware: it spawns the Python stub
// server and drives the SAME RingLinkClient the browser uses (Node 22+ exposes a
// global WebSocket, identical API). Asserts: hello+session, frame drain+render
// values, simulated pad_lost->live status, a calibrate round-trip, and reconnect
// with state-reset on a server restart (new session_id).
//
// Run with Node 22+:  RINGLINK_PYTHON=/path/to/python node contract-check.js
// (RINGLINK_PYTHON defaults to "python" — must have ringlink_server installed.)

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { RingLinkClient } from "./ringlink-client.js";

const PY = process.env.RINGLINK_PYTHON || "python";
const PORT = 28599;
const URL = `ws://127.0.0.1:${PORT}`;
const HERE = dirname(fileURLToPath(import.meta.url));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitUntil(pred, ms, label) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (pred()) return; await sleep(25); }
  throw new Error(`timeout waiting for: ${label}`);
}
function spawnServer() {
  const p = spawn(PY, ["-m", "ringlink_server", "serve", "--port", String(PORT), "--simulate-status"],
                  { stdio: ["ignore", "ignore", "inherit"] });
  return p;
}

// ---- observations the handlers accumulate ----
const helloSessions = [];
const flexSamples = [];
const calSteps = [];
const statusR = [];
let frames = 0, closes = 0;

const client = new RingLinkClient(URL, {
  onHello: (m) => helloSessions.push(m.session_id),
  onState: (s) => { frames++; if (flexSamples.length < 500) flexSamples.push(s.flex); },
  onCalibrating: (m) => calSteps.push(m.step),
  onStatus: (pads) => statusR.push(pads.R),
  onConnection: (s) => { if (s === "closed") closes++; },
});

const checks = [];
const check = (name, cond, detail = "") => { checks.push({ name, ok: !!cond, detail }); };

let server = spawnServer();
try {
  client.connect();

  // 1) hello + session id
  await waitUntil(() => helloSessions.length >= 1, 6000, "hello");
  check("hello carries a session_id", typeof helloSessions[0] === "string" && helloSessions[0].length > 0,
        `session_id=${helloSessions[0]}`);

  // 2) frame drain + live render values (flex must actually vary)
  await waitUntil(() => frames > 80, 4000, "frames");
  const span = Math.max(...flexSamples) - Math.min(...flexSamples);
  check("drains frames and flex varies", frames > 80 && span > 0.5, `frames=${frames}, flexSpan=${span.toFixed(2)}`);

  // 3) simulated status pad_lost -> live (the wake/reconnect affordance)
  await waitUntil(() => statusR.includes("lost") && statusR.indexOf("live") > statusR.indexOf("lost"),
                  6000, "status lost->live");
  check("status reports pad_lost then live", statusR.includes("lost") && statusR.includes("live"),
        `statusR=${statusR.join(",")}`);

  // 4) calibrate round-trip: directed-gesture steps + terminal 'done'
  client.calibrate("R");
  await waitUntil(() => calSteps.includes("done"), 4000, "calibrate done");
  check("calibrate drives lean-forward + lean-right + done",
        calSteps.includes("lean-forward") && calSteps.includes("lean-right") && calSteps.includes("done"),
        `steps=${[...new Set(calSteps)].join(",")}`);

  // 5) restart -> reconnect with a NEW session_id (and derived state reset)
  const firstSession = helloSessions[0];
  const framesBeforeKill = frames;
  server.kill();
  await waitUntil(() => closes >= 1, 5000, "socket close on server kill");
  await sleep(400);
  server = spawnServer();
  await waitUntil(() => helloSessions.length >= 2, 10000, "re-hello after restart");
  const reconnected = frames > framesBeforeKill + 20;
  await waitUntil(() => reconnected || frames > framesBeforeKill + 20, 4000, "frames resume after restart").catch(() => {});
  const newSession = helloSessions[helloSessions.length - 1];
  check("reconnects and observes a new session_id", newSession && newSession !== firstSession,
        `first=${firstSession} new=${newSession}`);
  check("frames resume after restart", frames > framesBeforeKill + 20, `frames now=${frames}`);

  // 6) the GO/NO-GO size claim: client logic well under ~50 lines
  const src = readFileSync(join(HERE, "ringlink-client.js"), "utf8").split(/\r?\n/);
  const logic = src.filter((l) => {
    const t = l.trim();
    if (!t) return false;
    if (t.startsWith("//")) return false;
    if (/^[}\]);,]*$/.test(t)) return false; // closing-only lines
    return true;
  }).length;
  check("client logic well under ~50 lines (excl. comments/HTML/braces)", logic < 50, `logic lines=${logic}`);
} finally {
  client.close();
  server.kill();
}

// ---- report ----
let failed = 0;
console.log("\nringlink Phase 2 — browser/JS client contract check\n");
for (const c of checks) {
  console.log(`  ${c.ok ? "PASS" : "FAIL"}  ${c.name}${c.detail ? "  (" + c.detail + ")" : ""}`);
  if (!c.ok) failed++;
}
console.log(`\n  ${failed === 0 ? "GREEN" : "RED"} — ${checks.length - failed}/${checks.length} checks passed\n`);
process.exit(failed === 0 ? 0 : 1);
