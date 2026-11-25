import express from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import pino from "pino";
import QRCode from "qrcode";
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  jidNormalizedUser,
} from "@whiskeysockets/baileys";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.WA_BRIDGE_PORT || 5055);
const FLASK_WEBHOOK = process.env.CONTACTSHUB_WEBHOOK || "http://127.0.0.1:5050/api/wa-bridge/webhook";
const AUTH_DIR = path.join(__dirname, "auth_info");

const logger = pino({ level: "warn" });
const app = express();
app.use(express.json({ limit: "4mb" }));

let sock = null;
let connectionState = "disconnected"; // disconnected | connecting | qr | connected
let currentQrDataUrl = null;
let lastError = null;
let meInfo = null;
let isStarting = false;
let reconnectTimer = null;

// In-memory store for synced WhatsApp contacts, chats, and messages
const contactsMap = new Map(); // jid -> { jid, phone, name, notify }
const messagesMap = new Map(); // jid -> Array<{ id, jid, phone, fromMe, body, timestamp, pushName, type }>

function jidToPhone(jid) {
  if (!jid || typeof jid !== "string") return "";
  if (jid.endsWith("@g.us") || jid === "status@broadcast") return "";
  const user = jid.split("@")[0].split(":")[0];
  const digits = user.replace(/\D+/g, "");
  return digits.length >= 6 ? `+${digits}` : "";
}

function phoneToJid(phone) {
  let digits = String(phone || "").replace(/\D+/g, "");
  if (digits.startsWith("00")) digits = digits.slice(2);
  if (!digits) return "";
  return `${digits}@s.whatsapp.net`;
}

function extractMessageText(msg) {
  const m = msg?.message;
  if (!m) return "";
  if (m.conversation) return m.conversation;
  if (m.extendedTextMessage?.text) return m.extendedTextMessage.text;
  if (m.imageMessage) return m.imageMessage.caption ? `📷 ${m.imageMessage.caption}` : "📷 [Image]";
  if (m.videoMessage) return m.videoMessage.caption ? `🎥 ${m.videoMessage.caption}` : "🎥 [Vidéo]";
  if (m.audioMessage) return "🎤 [Message vocal]";
  if (m.documentMessage) return `📄 ${m.documentMessage.fileName || "[Document]"}`;
  if (m.stickerMessage) return "🏷️ [Sticker]";
  if (m.locationMessage) return "📍 [Localisation]";
  if (m.contactMessage) return `👤 ${m.contactMessage.displayName || "[Contact]"}`;
  if (m.buttonsResponseMessage?.selectedDisplayText) return m.buttonsResponseMessage.selectedDisplayText;
  if (m.listResponseMessage?.title) return m.listResponseMessage.title;
  return "";
}

function normalizeMessage(msg) {
  const rawJid = msg?.key?.remoteJid;
  if (!rawJid || rawJid === "status@broadcast" || rawJid.endsWith("@g.us") || rawJid.endsWith("@newsletter")) {
    return null;
  }
  const jid = jidNormalizedUser(rawJid);
  const phone = jidToPhone(jid);
  if (!phone) return null;
  const body = extractMessageText(msg);
  if (!body) return null;
  const tsRaw = Number(msg.messageTimestamp?.low ?? msg.messageTimestamp ?? Math.floor(Date.now() / 1000));
  return {
    id: msg.key.id || `${jid}_${tsRaw}_${Math.random().toString(36).slice(2, 7)}`,
    jid,
    phone,
    fromMe: Boolean(msg.key.fromMe),
    body,
    timestamp: tsRaw,
    pushName: msg.pushName || "",
  };
}

function storeContact(c) {
  if (!c?.id) return null;
  if (c.id.endsWith("@g.us") || c.id === "status@broadcast" || c.id.endsWith("@newsletter")) return null;
  const jid = jidNormalizedUser(c.id);
  const phone = jidToPhone(jid);
  if (!phone) return null;
  const prev = contactsMap.get(jid) || { jid, phone, name: "", notify: "" };
  const merged = {
    jid,
    phone,
    name: c.name || c.verifiedName || prev.name || "",
    notify: c.notify || prev.notify || "",
  };
  contactsMap.set(jid, merged);
  return merged;
}

function storeMessage(norm) {
  if (!norm) return;
  const arr = messagesMap.get(norm.jid) || [];
  if (!arr.some((x) => x.id === norm.id)) {
    arr.push(norm);
    arr.sort((a, b) => a.timestamp - b.timestamp);
    if (arr.length > 200) arr.splice(0, arr.length - 200);
    messagesMap.set(norm.jid, arr);
  }
  if (!contactsMap.has(norm.jid)) {
    contactsMap.set(norm.jid, {
      jid: norm.jid,
      phone: norm.phone,
      name: "",
      notify: norm.fromMe ? "" : norm.pushName || "",
    });
  } else if (!norm.fromMe && norm.pushName) {
    const prev = contactsMap.get(norm.jid);
    if (!prev.notify) {
      prev.notify = norm.pushName;
      contactsMap.set(norm.jid, prev);
    }
  }
}

async function notifyFlask(payload) {
  try {
    await fetch(FLASK_WEBHOOK, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    // Flask may be restarting; ignore silently
  }
}

async function startSocket() {
  if (isStarting) return;
  isStarting = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  lastError = null;
  connectionState = "connecting";

  try {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    let version;
    try {
      const latest = await fetchLatestBaileysVersion();
      version = latest.version;
    } catch {
      version = undefined;
    }

    sock = makeWASocket({
      version,
      auth: state,
      logger,
      printQRInTerminal: false,
      browser: ["ContactsHub", "Chrome", "120.0.0"],
      syncFullHistory: true,
      markOnlineOnConnect: false,
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;
      if (qr) {
        connectionState = "qr";
        try {
          currentQrDataUrl = await QRCode.toDataURL(qr, { width: 280, margin: 2 });
        } catch (err) {
          lastError = String(err);
        }
      }
      if (connection === "open") {
        connectionState = "connected";
        currentQrDataUrl = null;
        lastError = null;
        const user = sock.user || {};
        meInfo = {
          id: user.id || "",
          name: user.name || "",
          phone: jidToPhone(user.id || ""),
        };
        notifyFlask({ event: "connected", me: meInfo });
      }
      if (connection === "close") {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const loggedOut = statusCode === DisconnectReason.loggedOut;
        connectionState = "disconnected";
        currentQrDataUrl = null;
        meInfo = null;
        if (loggedOut) {
          lastError = "Session déconnectée depuis le téléphone.";
          try {
            fs.rmSync(AUTH_DIR, { recursive: true, force: true });
          } catch {}
        } else {
          lastError = lastDisconnect?.error?.message || "Connexion interrompue, reconnexion...";
          reconnectTimer = setTimeout(() => {
            startSocket();
          }, 3000);
        }
      }
    });

    sock.ev.on("messaging-history.set", ({ contacts = [], messages = [] }) => {
      const syncedContacts = [];
      for (const c of contacts) {
        const sc = storeContact(c);
        if (sc) syncedContacts.push(sc);
      }
      const syncedMessages = [];
      for (const m of messages) {
        const nm = normalizeMessage(m);
        if (nm) {
          storeMessage(nm);
          syncedMessages.push(nm);
        }
      }
      if (syncedContacts.length || syncedMessages.length) {
        notifyFlask({
          event: "history_sync",
          contacts: syncedContacts.slice(0, 1000),
          messages: syncedMessages.slice(-500),
        });
      }
    });

    sock.ev.on("contacts.upsert", (contacts) => {
      const synced = [];
      for (const c of contacts) {
        const sc = storeContact(c);
        if (sc) synced.push(sc);
      }
      if (synced.length) {
        notifyFlask({ event: "contacts", contacts: synced });
      }
    });

    sock.ev.on("contacts.update", (updates) => {
      const synced = [];
      for (const c of updates) {
        const sc = storeContact(c);
        if (sc) synced.push(sc);
      }
      if (synced.length) {
        notifyFlask({ event: "contacts", contacts: synced });
      }
    });

    sock.ev.on("messages.upsert", ({ messages = [] }) => {
      const incoming = [];
      for (const m of messages) {
        const nm = normalizeMessage(m);
        if (nm) {
          storeMessage(nm);
          incoming.push(nm);
        }
      }
      if (incoming.length) {
        notifyFlask({ event: "messages", messages: incoming });
      }
    });
  } catch (err) {
    connectionState = "disconnected";
    lastError = String(err?.message || err);
  } finally {
    isStarting = false;
  }
}

// ---------------- HTTP API ----------------

app.get("/status", (req, res) => {
  let msgCount = 0;
  for (const arr of messagesMap.values()) msgCount += arr.length;
  res.json({
    state: connectionState,
    qr: currentQrDataUrl,
    me: meInfo,
    counts: {
      contacts: contactsMap.size,
      chats: messagesMap.size,
      messages: msgCount,
    },
    lastError,
  });
});

app.post("/connect", async (req, res) => {
  if (connectionState === "connected") {
    return res.json({ ok: true, state: connectionState, me: meInfo });
  }
  if (!isStarting) {
    startSocket();
  }
  res.json({ ok: true, state: connectionState });
});

app.post("/logout", async (req, res) => {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  try {
    if (sock) await sock.logout();
  } catch {}
  try {
    sock?.end?.();
  } catch {}
  sock = null;
  connectionState = "disconnected";
  currentQrDataUrl = null;
  meInfo = null;
  lastError = null;
  contactsMap.clear();
  messagesMap.clear();
  try {
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
  } catch {}
  res.json({ ok: true });
});

app.get("/contacts", (req, res) => {
  res.json({ items: Array.from(contactsMap.values()) });
});

app.get("/chats", (req, res) => {
  const chats = [];
  for (const [jid, msgs] of messagesMap.entries()) {
    const last = msgs[msgs.length - 1];
    const c = contactsMap.get(jid) || { jid, phone: jidToPhone(jid), name: "", notify: "" };
    chats.push({
      jid,
      phone: c.phone,
      name: c.name || c.notify || c.phone,
      lastMessage: last?.body || "",
      lastTimestamp: last?.timestamp || 0,
      lastFromMe: Boolean(last?.fromMe),
      messageCount: msgs.length,
    });
  }
  chats.sort((a, b) => b.lastTimestamp - a.lastTimestamp);
  res.json({ items: chats });
});

app.get("/messages", (req, res) => {
  const jid = req.query.jid ? jidNormalizedUser(String(req.query.jid)) : phoneToJid(req.query.phone || "");
  if (!jid) return res.json({ items: [] });
  res.json({ items: messagesMap.get(jid) || [] });
});

app.post("/send", async (req, res) => {
  if (connectionState !== "connected" || !sock) {
    return res.status(400).json({ error: "WhatsApp n'est pas connecté. Scannez d'abord le QR Code." });
  }
  const text = String(req.body?.text || "").trim();
  const rawPhone = req.body?.phone || "";
  const jid = req.body?.jid ? jidNormalizedUser(String(req.body.jid)) : phoneToJid(rawPhone);
  if (!jid || !text) {
    return res.status(400).json({ error: "Numéro de téléphone et message requis." });
  }
  try {
    const sent = await sock.sendMessage(jid, { text });
    const ts = Math.floor(Date.now() / 1000);
    const norm = {
      id: sent?.key?.id || `out_${ts}_${Math.random().toString(36).slice(2, 7)}`,
      jid,
      phone: jidToPhone(jid),
      fromMe: true,
      body: text,
      timestamp: ts,
      pushName: meInfo?.name || "Moi",
    };
    storeMessage(norm);
    notifyFlask({ event: "messages", messages: [norm] });
    res.json({ ok: true, message: norm });
  } catch (err) {
    res.status(500).json({ error: String(err?.message || err) });
  }
});

app.post("/sync", async (req, res) => {
  const allContacts = Array.from(contactsMap.values());
  const allMessages = [];
  for (const arr of messagesMap.values()) {
    allMessages.push(...arr);
  }
  await notifyFlask({
    event: "history_sync",
    contacts: allContacts,
    messages: allMessages.slice(-1000),
  });
  res.json({ ok: true, contacts: allContacts.length, messages: allMessages.length });
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`ContactsHub WA Bridge listening on http://127.0.0.1:${PORT}`);
  // Auto-reconnect if session credentials already exist
  if (fs.existsSync(path.join(AUTH_DIR, "creds.json"))) {
    startSocket();
  }
});
