// A stand-in for Uptime Kuma's socket.io API, just the three events the
// bootstrap script uses, with the same contracts as server/server.js in
// Kuma 2.5.5 ("needSetup", "setup", "login").
//
//   node fake-kuma.js <fresh|owned|silent> <port>
//
// fresh   no admin yet; the first setup call creates one
// owned   an admin with some other password already exists
// silent  accepts the websocket and never answers an event
"use strict";
const { Server } = require("socket.io");

const mode = process.argv[2];
const port = Number(process.argv[3]);
let admin = mode === "owned" ? { username: "admin", password: "someone-elses-password-0123456789" } : null;

const io = new Server(port);
io.on("connection", (socket) => {
    if (mode === "silent") {
        return;
    }
    socket.on("needSetup", (callback) => callback(admin === null));
    socket.on("setup", (username, password, callback) => {
        if (admin) {
            callback({ ok: false, msg: "Uptime Kuma has been initialized. If you want to run setup again, please delete the database." });
            return;
        }
        admin = { username, password };
        callback({ ok: true, msg: "successAdded", msgi18n: true });
    });
    socket.on("login", (data, callback) => {
        const ok = admin && data.username === admin.username && data.password === admin.password;
        callback(ok ? { ok: true, token: "fake-jwt" } : { ok: false, msg: "authIncorrectCreds", msgi18n: true });
    });
});
process.stdout.write("listening\n");
