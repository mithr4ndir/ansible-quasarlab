// Create Uptime Kuma's admin account if none exists, then prove we can log in.
//
// Managed by Ansible (roles/uptime_kuma). Run inside the uptime-kuma
// container, which already ships node and socket.io-client:
//
//   docker exec -i -e KUMA_ADMIN_USERNAME=admin uptime-kuma \
//       node - < kuma-bootstrap-admin.js
//
// The password is read from the compose file secret inside the container
// (KUMA_ADMIN_PASSWORD_FILE, default /run/secrets/kuma_admin_password), never
// from the environment or a command line, where `ps` or Ansible's -vvv
// connection log would show it.
//
// A fresh Kuma serves a /setup page where the FIRST visitor becomes admin.
// The role starts Kuma bound to 127.0.0.1 until this script has run, so
// nobody on the LAN can win that race, and runs it on every deploy.
//
// The login check matters as much as the setup. If an admin already exists
// and our password does not work, someone else completed setup (or the
// password file changed) and the deploy must stop rather than report success.
//
// Prints one JSON line: {"ok": bool, "created": bool, "error"?: string}.
// Never prints the password. Exit: 0 ok, 2 bad input, 3 cannot connect or
// timed out, 4 setup refused, 5 login refused.
"use strict";

const fs = require("fs");
const { io } = require("socket.io-client");

const url = process.env.KUMA_URL || "http://127.0.0.1:3001";
const username = process.env.KUMA_ADMIN_USERNAME || "";
const passwordFile = process.env.KUMA_ADMIN_PASSWORD_FILE || "/run/secrets/kuma_admin_password";
let password = "";
try {
    password = fs.readFileSync(passwordFile, "utf8").trim();
} catch (err) {
    password = "";
}
const deadlineMs = Number(process.env.KUMA_BOOTSTRAP_TIMEOUT_MS || 30000);

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

if (!/^[A-Za-z0-9_.-]{1,64}$/.test(username) || password.length < 16) {
    finish(2, { ok: false, created: false, error: "KUMA_ADMIN_USERNAME invalid, or the password file is missing, unreadable or shorter than 16 characters" });
}

setTimeout(() => finish(3, { ok: false, created: false, error: `no answer from Kuma within ${deadlineMs} ms` }), deadlineMs).unref();

socket = io(url, { transports: [ "websocket" ], reconnection: false, timeout: 10000 });

socket.on("connect_error", (err) => {
    finish(3, { ok: false, created: false, error: "connect_error: " + String(err && err.message) });
});

function login(created) {
    socket.emit("login", { username, password, token: "" }, (res) => {
        if (res && res.ok) {
            finish(0, { ok: true, created });
        } else {
            finish(5, { ok: false, created, error: "login refused: " + String(res && res.msg) });
        }
    });
}

socket.on("connect", () => {
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
