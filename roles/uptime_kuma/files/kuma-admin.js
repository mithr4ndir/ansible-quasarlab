// Talk to Uptime Kuma as its admin, from inside the uptime-kuma container,
// which already ships node and socket.io-client.
//
// Managed by Ansible (roles/uptime_kuma).
//
//   docker exec -i -e KUMA_ACTION=bootstrap -e KUMA_ADMIN_USERNAME=admin \
//       uptime-kuma node - < kuma-admin.js
//
// KUMA_ACTION=bootstrap  Create the admin account if none exists, then prove
//                        the login works. Prints {"ok", "created"}.
// KUMA_ACTION=list       Log in and print the monitors and notifications Kuma
//                        holds, reduced to names, ids, active flags and
//                        notification wiring: never URLs, tokens or configs.
//                        Prints {"ok", "monitors", "notifications"}.
//
// The password is read from the compose file secret inside the container
// (KUMA_ADMIN_PASSWORD_FILE, default /run/secrets/kuma_admin_password), never
// from the environment or a command line, where `ps` or Ansible's -vvv
// connection log would show it.
//
// A fresh Kuma serves a /setup page where the FIRST visitor becomes admin.
// The role starts Kuma bound to 127.0.0.1 until bootstrap has run. If an
// admin already exists and our password does not work, someone else completed
// setup (or the password file changed) and the deploy must stop.
//
// Prints one JSON line and never the password. Exit: 0 ok, 2 bad input,
// 3 cannot connect or timed out, 4 setup refused, 5 login refused.
"use strict";

const fs = require("fs");
const { io } = require("socket.io-client");

const action = process.env.KUMA_ACTION || "bootstrap";
const url = process.env.KUMA_URL || "http://127.0.0.1:3001";
const username = process.env.KUMA_ADMIN_USERNAME || "";
const passwordFile = process.env.KUMA_ADMIN_PASSWORD_FILE || "/run/secrets/kuma_admin_password";
const deadlineMs = Number(process.env.KUMA_ADMIN_TIMEOUT_MS || 30000);

let password = "";
try {
    password = fs.readFileSync(passwordFile, "utf8").trim();
} catch (err) {
    // Reported below as bad input, without the path's contents.
    password = "";
}

let done = false;
let socket = null;

function finish(code, result) {
    if (done) {
        return;
    }
    done = true;
    process.stdout.write(JSON.stringify(result) + "\n");
    if (socket) {
        socket.close();
    }
    process.exit(code);
}

if (![ "bootstrap", "list" ].includes(action)) {
    finish(2, { ok: false, error: "KUMA_ACTION must be bootstrap or list" });
}
if (!/^[A-Za-z0-9_.-]{1,64}$/.test(username) || password.length < 16) {
    finish(2, { ok: false, created: false, error: "KUMA_ADMIN_USERNAME invalid, or the password file is missing, unreadable or shorter than 16 characters" });
}

setTimeout(() => finish(3, { ok: false, created: false, error: `no answer from Kuma within ${deadlineMs} ms` }), deadlineMs).unref();

socket = io(url, { transports: [ "websocket" ], reconnection: false, timeout: 10000 });

socket.on("connect_error", (err) => {
    finish(3, { ok: false, created: false, error: "connect_error: " + String(err && err.message) });
});

// Kuma pushes these after a successful login (afterLogin in server.js).
let monitors = null;
let notifications = null;
let loggedIn = false;

function maybeFinishList() {
    if (action !== "list" || !loggedIn || monitors === null || notifications === null) {
        return;
    }
    finish(0, {
        ok: true,
        monitors: Object.values(monitors).map((m) => ({
            name: m.name,
            active: m.active,
            notificationIDList: m.notificationIDList || {},
        })),
        notifications: notifications.map((n) => ({ id: n.id, name: n.name, active: n.active })),
    });
}

socket.on("monitorList", (list) => {
    monitors = list || {};
    maybeFinishList();
});
socket.on("notificationList", (list) => {
    notifications = list || [];
    maybeFinishList();
});

function login(created) {
    socket.emit("login", { username, password, token: "" }, (res) => {
        if (!(res && res.ok)) {
            finish(5, { ok: false, created, error: "login refused: " + String(res && res.msg) });
            return;
        }
        loggedIn = true;
        if (action === "bootstrap") {
            finish(0, { ok: true, created });
        } else {
            maybeFinishList();
        }
    });
}

// Wait for Kuma's "info" event before asking anything. Kuma registers its
// socket handlers only after `await sendInfo(socket)` in its connection
// handler (server/server.js), so an event sent straight after "connect" can be
// dropped unanswered. That happened on the first try against a fresh Kuma
// 2.5.5 in testing. "info" is emitted at the end of sendInfo, and the handlers
// are registered in the same tick after it.
let started = false;
socket.on("info", () => {
    if (started) {
        return;
    }
    started = true;
    if (action === "list") {
        login(false);
        return;
    }
    socket.emit("needSetup", (needSetup) => {
        if (!needSetup) {
            login(false);
            return;
        }
        socket.emit("setup", username, password, (res) => {
            if (res && res.ok) {
                login(true);
            } else {
                finish(4, { ok: false, created: false, error: "setup refused: " + String(res && res.msg) });
            }
        });
    });
});
