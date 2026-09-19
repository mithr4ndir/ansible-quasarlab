// Decide, continuously, whether Uptime Kuma may be reached from the LAN.
//
// Managed by Ansible (roles/uptime_kuma). Runs in its own container from the
// Kuma image (node and socket.io-client are already there), on the compose
// network only. The TLS proxy asks it before every request (nginx
// auth_request): 204 means forward, 403 means refuse.
//
// Why: a Kuma with no admin account serves a setup flow in which the FIRST
// visitor becomes admin. Kuma decides that at startup, so a Kuma restarted
// on a lost or wiped database would hand the admin account to whoever on the
// LAN reaches it first. The gate is OPEN only while all of these hold, on
// the current connection to Kuma:
//
//   - the websocket to Kuma is connected,
//   - Kuma answers needSetup = false (asked on connect and every CHECK_MS),
//   - our own admin login (the password file secret) succeeded.
//
// Everything else is CLOSED: starting up, Kuma down or restarting, Kuma
// needing setup, Kuma owned by an account whose password we do not have, a
// check that gets no answer within ANSWER_MS. A Kuma restart drops the
// websocket, which closes the gate before the new Kuma process is listening;
// the gate only reopens after the new process passes both checks.
//
// Logs one JSON line per state change. Never logs the password.
"use strict";

const fs = require("fs");
const http = require("http");
const { io } = require("socket.io-client");

const KUMA_URL = process.env.KUMA_URL || "http://uptime-kuma:3001";
const USERNAME = process.env.KUMA_ADMIN_USERNAME || "admin";
const PASSWORD_FILE = process.env.KUMA_ADMIN_PASSWORD_FILE || "/run/secrets/kuma_admin_password";
const LISTEN_PORT = Number(process.env.KUMA_GATE_PORT || 8081);
const CHECK_MS = Number(process.env.KUMA_GATE_CHECK_MS || 15000);
const ANSWER_MS = Number(process.env.KUMA_GATE_ANSWER_MS || 5000);

let open = false;
let reason = "starting";

function setState(nextOpen, why) {
    if (open !== nextOpen || reason !== why) {
        process.stdout.write(JSON.stringify({ ts: new Date().toISOString(), gate: nextOpen ? "open" : "closed", reason: why }) + "\n");
    }
    open = nextOpen;
    reason = why;
}

function readPassword() {
    try {
        return fs.readFileSync(PASSWORD_FILE, "utf8").trim();
    } catch (err) {
        // Reported as a closed gate; the contents are never logged.
        return "";
    }
}

// Emit with an answer deadline; a missing answer counts as a failure.
function ask(socket, event, args, onAnswer) {
    let answered = false;
    const timer = setTimeout(() => {
        if (!answered) {
            answered = true;
            onAnswer(undefined, true);
        }
    }, ANSWER_MS);
    socket.emit(event, ...args, (res) => {
        if (!answered) {
            answered = true;
            clearTimeout(timer);
            onAnswer(res, false);
        }
    });
}

const socket = io(KUMA_URL, {
    transports: [ "websocket" ],
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    timeout: 10000,
});

let generation = 0;
let ready = false;      // Kuma has sent "info" on the current connection
let loggedIn = false;   // our login succeeded on the current connection

socket.on("disconnect", () => {
    generation += 1;
    ready = false;
    loggedIn = false;
    setState(false, "disconnected from Kuma");
});
socket.on("connect_error", () => {
    ready = false;
    loggedIn = false;
    setState(false, "cannot connect to Kuma");
});

// needSetup must be false, then our login must succeed.
function fullCheck() {
    const gen = generation;
    ask(socket, "needSetup", [], (needSetup, timedOut) => {
        if (gen !== generation) {
            return;
        }
        if (timedOut || needSetup !== false) {
            setState(false, timedOut ? "no needSetup answer" : "Kuma needs setup");
            return;
        }
        const password = readPassword();
        if (password.length < 16) {
            setState(false, "admin password file missing or unreadable");
            return;
        }
        ask(socket, "login", [ { username: USERNAME, password, token: "" } ], (res, loginTimedOut) => {
            if (gen !== generation) {
                return;
            }
            if (loginTimedOut || !(res && res.ok)) {
                setState(false, loginTimedOut ? "no login answer" : "our admin login was refused");
                return;
            }
            loggedIn = true;
            setState(true, "Kuma has our admin account");
        });
    });
}

// Kuma registers its handlers only after it has sent "info" (see
// kuma-admin.js), so every check on a new connection starts there.
socket.on("info", () => {
    if (ready) {
        return;
    }
    ready = true;
    loggedIn = false;
    setState(false, "checking a new Kuma connection");
    fullCheck();
});

// While open: keep confirming needSetup is false. While closed but
// connected (for example waiting for the admin to be bootstrapped): retry
// the full check.
setInterval(() => {
    if (!socket.connected || !ready) {
        return;
    }
    if (!loggedIn) {
        fullCheck();
        return;
    }
    const gen = generation;
    ask(socket, "needSetup", [], (needSetup, timedOut) => {
        if (gen !== generation || (!timedOut && needSetup === false)) {
            return;
        }
        loggedIn = false;
        setState(false, timedOut ? "no needSetup answer" : "Kuma needs setup");
        // Reconnect, so the next "info" re-runs both checks from scratch.
        socket.disconnect();
        socket.connect();
    });
}, CHECK_MS).unref();

http.createServer((req, res) => {
    if (req.method === "GET" && req.url === "/gate") {
        res.writeHead(open ? 204 : 403, { "Cache-Control": "no-store" });
        res.end();
        return;
    }
    res.writeHead(404);
    res.end();
}).listen(LISTEN_PORT, "0.0.0.0");

setState(false, "starting");
