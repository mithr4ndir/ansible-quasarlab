// A stand-in for Uptime Kuma's socket.io API, just the three events the
// bootstrap script uses, with the same contracts as server/server.js in
// Kuma 2.5.5 ("needSetup", "setup", "login").
//
//   node fake-kuma.js <fresh|owned|silent> <port>
//
// fresh   no admin yet; the first setup call creates one
// owned   an admin with some other password already exists
// silent  accepts the websocket and never answers an event
//
// In every mode but silent, handlers are registered only after a 1s delay
// and an "info" event, as in Kuma's connection handler.
"use strict";
const { Server } = require("socket.io");

const mode = process.argv[2];
const port = Number(process.argv[3]);
let admin = mode === "owned" ? { username: "admin", password: "someone-elses-password-0123456789" } : null;

const io = new Server(port);
io.on("connection", async (socket) => {
    if (mode === "silent") {
        return;
    }
    // Like Kuma: `await sendInfo(socket)` first, which ends by emitting
    // "info", and only then register the handlers. Anything a client sends
    // before that is dropped. The delay makes the race deterministic.
    await new Promise((resolve) => setTimeout(resolve, 1000));
    socket.emit("info", { version: "2.5.5-fake" });
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
        if (ok) {
            // afterLogin: Kuma pushes the lists, with secrets in them.
            socket.emit("monitorList", {
                "1": { id: 1, name: "Prometheus", active: true, notificationIDList: { "1": true },
                       url: "http://192.0.2.1:9090/-/ready" },
                "2": { id: 2, name: "NFS", active: true, notificationIDList: { "1": true },
                       pushToken: "SECRETpushTOKENvalue0123456789ab" },
            });
            socket.emit("notificationList", [
                { id: 1, name: "Discord", active: true,
                  config: JSON.stringify({ discordWebhookUrl: "https://discord.com/api/webhooks/1/SECRET-webhook" }) },
            ]);
        }
    });
});
process.stdout.write("listening\n");
