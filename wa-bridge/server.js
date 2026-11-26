import crypto from "crypto";
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
const BRIDGE_SECRET = String(process.env.WA_BRIDGE_SECRET || "").trim();
const AUTH_DIR = process.env.WA_BRIDGE_AUTH_DIR || path.join(__dirname, "auth_info");

const logger = pino({ level: "warn" });
const app = express();
app.use(express.json({ limit: "4mb" }));

// Block DNS rebinding & enforce shared Bearer token
const ALLOWED_HOSTS = new Set([`127.0.0.1:${PORT}`, `localhost:${PORT}`, "127.0.0.1", "localhost"]);
app.use((req, res, next) => {
  const host = String(req.headers.host || "").toLowerCase();
  if (!ALLOWED_HOSTS.has(host)) {
    return res.status(403).json({ error: "Host non autorisé." });
  }
  if (BRIDGE_SECRET) {
    const auth = String(req.headers.authorization || "");
    const expected = `Bearer ${BRIDGE_SECRET}`;
    const aBuf = Buffer.from(auth);
    const eBuf = Buffer.from(expected);
    if (aBuf.length !== eBuf.length || !crypto.timingSafeEqual(aBuf, eBuf)) {
      return res.status(401).json({ error: "Authentification bridge invalide." });
    }
  }
  next();
});

let sock = null;
let connectionState = "disconnected"; // disconnected | connecting | qr | connected
let currentQrDataUrl = null;
let lastError = null;
let meInfo = null;
let isStarting = false;
let reconnectTimer = null;

// In-memory store for synced WhatsApp contacts, chats, messages, and LID -> PN resolution
const contactsMap = new Map(); // jid -> { jid, phone, name, notify }
const messagesMap = new Map(); // jid -> Array<{ id, jid, phone, fromMe, body, timestamp, pushName, type }>
const lidToPnMap = new Map();  // lidJid (@lid) -> phoneJid (@s.whatsapp.net)
const LID_MAP_FILE = path.join(AUTH_DIR, "lid_map.json");

function loadLidMap() {
  try {
    if (fs.existsSync(LID_MAP_FILE)) {
      const obj = JSON.parse(fs.readFileSync(LID_MAP_FILE, "utf8"));
      for (const [k, v] of Object.entries(obj || {})) {
        if (k && v) lidToPnMap.set(k, v);
      }
    }
  } catch {}
}

let lidSaveTimer = null;
function scheduleSaveLidMap() {
  if (lidSaveTimer) return;
  lidSaveTimer = setTimeout(() => {
    lidSaveTimer = null;
    try {
      fs.mkdirSync(AUTH_DIR, { recursive: true });
      fs.writeFileSync(LID_MAP_FILE, JSON.stringify(Object.fromEntries(lidToPnMap)), "utf8");
    } catch {}
  }, 1500);
}

function registerLidMapping(lidRaw, pnRaw) {
  if (!lidRaw || !pnRaw) return;
  const lid = jidNormalizedUser(String(lidRaw));
  const pn = String(pnRaw).includes("@") ? jidNormalizedUser(String(pnRaw)) : phoneToJid(pnRaw);
  if (lid.endsWith("@lid") && pn.endsWith("@s.whatsapp.net")) {
    if (lidToPnMap.get(lid) !== pn) {
      lidToPnMap.set(lid, pn);
      scheduleSaveLidMap();
      // Migrate any previously stored @lid messages/contact under the real phone JID
      if (messagesMap.has(lid)) {
        const oldMsgs = messagesMap.get(lid) || [];
        const targetArr = messagesMap.get(pn) || [];
        const realPhone = jidToPhone(pn);
        for (const m of oldMsgs) {
          m.jid = pn;
          m.phone = realPhone;
          if (!targetArr.some((x) => x.id === m.id)) targetArr.push(m);
        }
        targetArr.sort((a, b) => a.timestamp - b.timestamp);
        messagesMap.set(pn, targetArr.slice(-200));
        messagesMap.delete(lid);
      }
      if (contactsMap.has(lid)) {
        const oldC = contactsMap.get(lid);
        const curC = contactsMap.get(pn) || { jid: pn, phone: jidToPhone(pn), name: "", notify: "" };
        contactsMap.set(pn, {
          jid: pn,
          phone: jidToPhone(pn),
          name: curC.name || oldC.name || "",
          notify: curC.notify || oldC.notify || "",
        });
        contactsMap.delete(lid);
      }
    }
  }
}

function resolveJid(rawJid, altJid = "") {
  if (!rawJid || typeof rawJid !== "string") return "";
  const norm = jidNormalizedUser(rawJid);
  if (altJid && typeof altJid === "string") {
    const normAlt = jidNormalizedUser(altJid);
    if (norm.endsWith("@lid") && normAlt.endsWith("@s.whatsapp.net")) {
      registerLidMapping(norm, normAlt);
      return normAlt;
    }
    if (normAlt.endsWith("@lid") && norm.endsWith("@s.whatsapp.net")) {
      registerLidMapping(normAlt, norm);
      return norm;
    }
  }
  if (norm.endsWith("@lid") && lidToPnMap.has(norm)) {
    return lidToPnMap.get(norm);
  }
  return norm;
}

function jidToPhone(jid) {
  if (!jid || typeof jid !== "string") return "";
  if (jid.endsWith("@g.us") || jid === "status@broadcast" || jid.endsWith("@newsletter")) return "";
  const resolved = resolveJid(jid);
  // Never turn an unresolved WhatsApp @lid privacy ID into a fake phone number!
  if (resolved.endsWith("@lid")) return "";
  const user = resolved.split("@")[0].split(":")[0];
  const digits = user.replace(/\D+/g, "");
  return digits.length >= 6 && digits.length <= 15 ? `+${digits}` : "";
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
  const altJid = msg?.key?.remoteJidAlt || msg?.key?.senderPn || "";
  const jid = resolveJid(rawJid, altJid);
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
  // Capture WhatsApp LID <-> Phone Number mappings from Baileys contact objects
  if (c.lid && c.id.endsWith("@s.whatsapp.net")) {
    registerLidMapping(c.lid, c.id);
  }
  if (c.id.endsWith("@lid") && (c.phoneNumber || c.pn)) {
    registerLidMapping(c.id, c.phoneNumber || c.pn);
  }
  const jid = resolveJid(c.id, c.phoneNumber || c.pn || "");
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
    const headers = { "Content-Type": "application/json" };
    if (BRIDGE_SECRET) {
      headers["X-WA-Bridge-Secret"] = BRIDGE_SECRET;
    }
    await fetch(FLASK_WEBHOOK, {
      method: "POST",
      headers,
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

app.get("/groups", async (req, res) => {
  if (connectionState !== "connected" || !sock) {
    return res.status(400).json({ ok: false, error: "WhatsApp n'est pas connecté. Scannez d'abord le QR Code.", items: [] });
  }
  try {
    const groupsObj = await sock.groupFetchAllParticipating();
    const groups = [];
    for (const [gjid, g] of Object.entries(groupsObj || {})) {
      const rawParts = Array.isArray(g?.participants) ? g.participants : [];
      const participants = [];
      const seenPhones = new Set();
      for (const p of rawParts) {
        const rawId = String(p?.id || "");
        const altPn = String(p?.phoneNumber || p?.pn || "");
        if (p?.lid && altPn) registerLidMapping(p.lid, altPn);
        if (rawId.endsWith("@lid") && altPn) registerLidMapping(rawId, altPn);
        const resolved = resolveJid(rawId, altPn);
        const phone = jidToPhone(resolved);
        if (!phone) continue;
        const digits = phone.replace(/\D+/g, "");
        if (digits.length < 7 || digits.length > 13 || seenPhones.has(phone)) continue;
        seenPhones.add(phone);
        const cInfo = contactsMap.get(resolved) || {};
        participants.push({
          jid: resolved,
          phone,
          name: cInfo.name || cInfo.notify || p?.notify || "",
          isAdmin: Boolean(p?.admin),
        });
      }
      groups.push({
        jid: gjid,
        subject: g?.subject || "Groupe sans nom",
        description: (g?.desc || "").slice(0, 220),
        size: rawParts.length,
        phoneCount: participants.length,
        participants,
      });
    }
    groups.sort((a, b) => b.phoneCount - a.phoneCount || a.subject.localeCompare(b.subject));
    res.json({ ok: true, items: groups });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err?.message || err), items: [] });
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
  loadLidMap();
  console.log(`ContactsHub WA Bridge listening on http://127.0.0.1:${PORT}`);
  // Auto-reconnect if session credentials already exist
  if (fs.existsSync(path.join(AUTH_DIR, "creds.json"))) {
    startSocket();
  }
});
