import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.join(BASE_DIR, "wa-bridge")
BRIDGE_URL = os.environ.get("WA_BRIDGE_URL", "http://127.0.0.1:5055")
_bridge_proc = None


def normalize_digits(phone: str) -> str:
    d = re.sub(r"\D+", "", str(phone or ""))
    if d.startswith("00"):
        d = d[2:]
    return d


def format_plus_phone(phone: str) -> str:
    d = normalize_digits(phone)
    return f"+{d}" if d else ""


def phone_to_jid(phone: str) -> str:
    d = normalize_digits(phone)
    return f"{d}@s.whatsapp.net" if d else ""


def _find_node_exe() -> Optional[str]:
    portable = os.path.join(BRIDGE_DIR, "node", "node.exe")
    if os.path.isfile(portable):
        return portable
    return shutil.which("node")


def _bridge_headers() -> Dict[str, str]:
    secret = db.get_wa_bridge_secret()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def is_bridge_alive() -> bool:
    try:
        r = requests.get(f"{BRIDGE_URL}/status", headers=_bridge_headers(), timeout=1.2)
        return r.status_code == 200
    except Exception:
        return False


def ensure_bridge_running() -> Dict[str, Any]:
    global _bridge_proc
    if is_bridge_alive():
        return {"ok": True}

    node_exe = _find_node_exe()
    server_js = os.path.join(BRIDGE_DIR, "server.js")
    node_modules = os.path.join(BRIDGE_DIR, "node_modules")
    if not node_exe:
        return {"ok": False, "error": "Runtime Node.js introuvable pour le pont WhatsApp."}
    if not os.path.isfile(server_js) or not os.path.isdir(node_modules):
        return {"ok": False, "error": "Dépendances du pont WhatsApp (wa-bridge) non installées."}

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    env = os.environ.copy()
    env["WA_BRIDGE_SECRET"] = db.get_wa_bridge_secret()
    flask_port = os.environ.get("PORT", "5050")
    env.setdefault("CONTACTSHUB_WEBHOOK", f"http://127.0.0.1:{flask_port}/api/wa-bridge/webhook")
    if os.environ.get("CONTACTSHUB_DATA_DIR"):
        env.setdefault("WA_BRIDGE_AUTH_DIR", os.path.join(db.DATA_DIR, "wa_auth_info"))

    log_path = os.path.join(db.DATA_DIR if os.environ.get("CONTACTSHUB_DATA_DIR") else BRIDGE_DIR, "bridge.log")
    with open(log_path, "a", encoding="utf-8") as log_f:
        _bridge_proc = subprocess.Popen(
            [node_exe, server_js],
            cwd=BRIDGE_DIR,
            env=env,
            stdout=log_f,
            stderr=log_f,
            creationflags=creationflags,
        )

    for _ in range(25):
        time.sleep(0.25)
        if is_bridge_alive():
            return {"ok": True}
    return {"ok": False, "error": "Le pont WhatsApp met trop de temps à démarrer."}


def bridge_get(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
    ensure = ensure_bridge_running()
    if not ensure.get("ok"):
        return {"state": "offline", "error": ensure.get("error")}
    try:
        r = requests.get(f"{BRIDGE_URL}{path}", params=params, headers=_bridge_headers(), timeout=timeout)
        return r.json()
    except Exception as exc:
        return {"state": "offline", "error": str(exc)}


def bridge_post(path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Dict[str, Any]:
    ensure = ensure_bridge_running()
    if not ensure.get("ok"):
        return {"ok": False, "error": ensure.get("error")}
    try:
        r = requests.post(f"{BRIDGE_URL}{path}", json=payload or {}, headers=_bridge_headers(), timeout=timeout)
        data = r.json()
        if r.status_code >= 400 and "error" not in data:
            data["error"] = f"Erreur bridge HTTP {r.status_code}"
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def find_contact_id_by_phone(conn, phone: str) -> Optional[int]:
    digits = normalize_digits(phone)
    if len(digits) < 6:
        return None
    tail8 = digits[-8:]
    rows = conn.execute(
        "SELECT id, phone FROM contacts WHERE phone != '' AND phone LIKE ?",
        (f"%{tail8[-6:]}%",),
    ).fetchall()
    for r in rows:
        rd = normalize_digits(r["phone"])
        if not rd:
            continue
        if rd == digits or (len(rd) >= 8 and len(digits) >= 8 and rd[-8:] == tail8):
            return int(r["id"])
    return None


def _is_real_wa_phone(jid: str, phone: str) -> bool:
    if not jid or jid.endswith("@lid") or jid.endswith("@g.us") or jid.endswith("@newsletter"):
        return False
    d = normalize_digits(phone)
    # Real international phone numbers are 7–13 digits; WhatsApp privacy LIDs are 14–15 digits
    return 7 <= len(d) <= 13


def process_webhook_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    event = payload.get("event") or ""
    now = datetime.now().isoformat(timespec="seconds")
    contacts_saved = 0
    messages_saved = 0

    with db.get_conn() as conn:
        for c in payload.get("contacts") or []:
            jid = (c.get("jid") or "").strip()
            phone = format_plus_phone(c.get("phone") or "")
            if not _is_real_wa_phone(jid, phone):
                continue
            cid = find_contact_id_by_phone(conn, phone)
            name = (c.get("name") or "").strip()
            notify = (c.get("notify") or "").strip()
            conn.execute(
                """
                INSERT INTO whatsapp_contacts (jid, phone, name, notify, contact_id, last_synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET
                    phone = excluded.phone,
                    name = CASE WHEN excluded.name != '' THEN excluded.name ELSE whatsapp_contacts.name END,
                    notify = CASE WHEN excluded.notify != '' THEN excluded.notify ELSE whatsapp_contacts.notify END,
                    contact_id = COALESCE(excluded.contact_id, whatsapp_contacts.contact_id),
                    last_synced_at = excluded.last_synced_at
                """,
                (jid, phone, name, notify, cid, now),
            )
            contacts_saved += 1

        is_realtime = event == "messages"
        for m in payload.get("messages") or []:
            wa_id = str(m.get("id") or "").strip()
            jid = (m.get("jid") or "").strip()
            phone = format_plus_phone(m.get("phone") or "")
            body = (m.get("body") or "").strip()
            if not wa_id or not body or not _is_real_wa_phone(jid, phone):
                continue
            ts = int(m.get("timestamp") or time.time())
            sent_at = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
            from_me = 1 if m.get("fromMe") else 0
            push_name = (m.get("pushName") or "").strip()
            cid = find_contact_id_by_phone(conn, phone)

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO whatsapp_messages
                (wa_msg_id, jid, phone, contact_id, from_me, body, push_name, sent_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wa_id, jid, phone, cid, from_me, body, push_name, sent_at, now),
            )
            if cur.rowcount > 0:
                messages_saved += 1
                # Ensure whatsapp_contacts entry exists
                conn.execute(
                    """
                    INSERT INTO whatsapp_contacts (jid, phone, name, notify, contact_id, last_synced_at)
                    VALUES (?, ?, '', ?, ?, ?)
                    ON CONFLICT(jid) DO UPDATE SET
                        notify = CASE WHEN excluded.notify != '' AND whatsapp_contacts.notify = '' THEN excluded.notify ELSE whatsapp_contacts.notify END,
                        contact_id = COALESCE(excluded.contact_id, whatsapp_contacts.contact_id),
                        last_synced_at = excluded.last_synced_at
                    """,
                    (jid, phone, "" if from_me else push_name, cid, now),
                )
                # Log real-time messages into CRM timeline for matched contacts
                if is_realtime and cid:
                    prefix = "💬 WhatsApp envoyé : " if from_me else "💬 WhatsApp reçu : "
                    conn.execute(
                        "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'whatsapp', ?, ?)",
                        (cid, f"{prefix}{body[:220]}", now),
                    )
                    if from_me:
                        conn.execute(
                            """
                            UPDATE contacts SET
                                status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                                last_contacted_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (now, now, cid),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE contacts SET
                                status = CASE WHEN status IN ('new', 'contacted') THEN 'replied' ELSE status END,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (now, cid),
                        )

    return {"ok": True, "contacts_saved": contacts_saved, "messages_saved": messages_saved}


def list_conversations(search: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                m.jid,
                m.phone,
                MAX(m.sent_at) AS last_sent_at,
                COUNT(*) AS message_count,
                (SELECT body FROM whatsapp_messages m2 WHERE m2.jid = m.jid ORDER BY m2.sent_at DESC, m2.id DESC LIMIT 1) AS last_body,
                (SELECT from_me FROM whatsapp_messages m2 WHERE m2.jid = m.jid ORDER BY m2.sent_at DESC, m2.id DESC LIMIT 1) AS last_from_me,
                wc.name AS wa_name,
                wc.notify AS wa_notify,
                COALESCE(wc.contact_id, m.contact_id) AS contact_id,
                c.full_name AS crm_name,
                c.email AS crm_email,
                c.company AS crm_company,
                c.lead_score AS crm_score,
                c.status AS crm_status
            FROM whatsapp_messages m
            LEFT JOIN whatsapp_contacts wc ON wc.jid = m.jid
            LEFT JOIN contacts c ON c.id = COALESCE(wc.contact_id, m.contact_id)
            GROUP BY m.jid
            ORDER BY last_sent_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        items = []
        q = search.strip().lower()
        for r in rows:
            d = dict(r)
            display = d["crm_name"] or d["wa_name"] or d["wa_notify"] or d["phone"]
            d["display_name"] = display
            if q:
                hay = f"{display} {d['phone']} {d['crm_company'] or ''} {d['last_body'] or ''}".lower()
                if q not in hay:
                    continue
            items.append(d)
        return items


def list_messages(phone: str = "", jid: str = "", contact_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        if contact_id and not phone and not jid:
            row = conn.execute("SELECT phone FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            if row and row["phone"]:
                phone = row["phone"]
        norm_phone = format_plus_phone(phone)
        target_jid = jid or phone_to_jid(norm_phone)

        clauses = []
        params: List[Any] = []
        if target_jid:
            clauses.append("jid = ?")
            params.append(target_jid)
        if norm_phone:
            clauses.append("phone = ?")
            params.append(norm_phone)
        if contact_id:
            clauses.append("contact_id = ?")
            params.append(int(contact_id))
        if not clauses:
            return []

        sql = f"""
            SELECT id, wa_msg_id, jid, phone, contact_id, from_me, body, push_name, sent_at
            FROM whatsapp_messages
            WHERE {' OR '.join(clauses)}
            ORDER BY sent_at ASC, id ASC
            LIMIT 300
        """
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def import_wa_contacts_to_crm(workspace_id: int = 1, only_with_messages: bool = False) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    linked = 0
    with db.get_conn() as conn:
        if only_with_messages:
            wa_rows = conn.execute(
                """
                SELECT wc.* FROM whatsapp_contacts wc
                WHERE EXISTS (SELECT 1 FROM whatsapp_messages m WHERE m.jid = wc.jid)
                """
            ).fetchall()
        else:
            wa_rows = conn.execute("SELECT * FROM whatsapp_contacts").fetchall()

        for wc in wa_rows:
            phone = wc["phone"]
            digits = normalize_digits(phone)
            if not _is_real_wa_phone(wc["jid"], phone):
                continue
            existing_id = find_contact_id_by_phone(conn, phone)
            if existing_id:
                conn.execute("UPDATE whatsapp_contacts SET contact_id = ? WHERE jid = ?", (existing_id, wc["jid"]))
                conn.execute("UPDATE whatsapp_messages SET contact_id = ? WHERE jid = ? AND contact_id IS NULL", (existing_id, wc["jid"]))
                linked += 1
                continue

            full_name = (wc["name"] or wc["notify"] or phone).strip()
            parts = full_name.split(" ", 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""
            synth_email = f"wa.{digits}@whatsapp.local"

            row_email = conn.execute("SELECT id FROM contacts WHERE email = ?", (synth_email,)).fetchone()
            if row_email:
                cid = int(row_email["id"])
                conn.execute("UPDATE whatsapp_contacts SET contact_id = ? WHERE jid = ?", (cid, wc["jid"]))
                conn.execute("UPDATE whatsapp_messages SET contact_id = ? WHERE jid = ? AND contact_id IS NULL", (cid, wc["jid"]))
                linked += 1
                continue

            cur = conn.execute(
                """
                INSERT INTO contacts (
                    email, first_name, last_name, full_name, phone,
                    tags, source_type, source_detail, status, email_type, mx_status,
                    lead_score, workspace_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'WhatsApp', 'whatsapp', 'Sync WhatsApp interne', 'new', 'unknown', 'unchecked', 35, ?, ?, ?)
                """,
                (synth_email, first_name, last_name, full_name, phone, workspace_id, now, now),
            )
            cid = cur.lastrowid
            conn.execute("UPDATE whatsapp_contacts SET contact_id = ? WHERE jid = ?", (cid, wc["jid"]))
            conn.execute("UPDATE whatsapp_messages SET contact_id = ? WHERE jid = ? AND contact_id IS NULL", (cid, wc["jid"]))
            inserted += 1

    return {"ok": True, "inserted": inserted, "linked": linked}


def list_groups() -> Dict[str, Any]:
    res = bridge_get("/groups", timeout=25.0)
    if res.get("error") and not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "items": []}
    return {"ok": True, "items": res.get("items") or []}


def import_group_members_to_crm(
    group_subject: str,
    participants: List[Dict[str, Any]],
    tag: str = "",
    workspace_id: int = 1,
) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    subj = (group_subject or "Groupe WhatsApp").strip()[:100]
    group_tag = (tag or f"Groupe: {subj}").strip()[:80]
    inserted = 0
    linked = 0
    skipped = 0

    with db.get_conn() as conn:
        for p in participants or []:
            jid = (p.get("jid") or "").strip()
            phone = format_plus_phone(p.get("phone") or "")
            if not _is_real_wa_phone(jid, phone):
                skipped += 1
                continue
            digits = normalize_digits(phone)
            name = (p.get("name") or "").strip()

            # Upsert into whatsapp_contacts so it's ready in WA inbox
            conn.execute(
                """
                INSERT INTO whatsapp_contacts (jid, phone, name, notify, last_synced_at)
                VALUES (?, ?, ?, '', ?)
                ON CONFLICT(jid) DO UPDATE SET
                    phone = excluded.phone,
                    name = CASE WHEN excluded.name != '' AND whatsapp_contacts.name = '' THEN excluded.name ELSE whatsapp_contacts.name END,
                    last_synced_at = excluded.last_synced_at
                """,
                (jid, phone, name, now),
            )

            existing_id = find_contact_id_by_phone(conn, phone)
            if existing_id:
                row = conn.execute("SELECT tags FROM contacts WHERE id = ?", (existing_id,)).fetchone()
                cur_tags = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()] if row else []
                if group_tag and group_tag.lower() not in {t.lower() for t in cur_tags}:
                    cur_tags.append(group_tag)
                    conn.execute("UPDATE contacts SET tags = ?, updated_at = ? WHERE id = ?", (", ".join(cur_tags), now, existing_id))
                conn.execute("UPDATE whatsapp_contacts SET contact_id = ? WHERE jid = ?", (existing_id, jid))
                linked += 1
                continue

            full_name = name or phone
            parts = full_name.split(" ", 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""
            synth_email = f"wa.{digits}@whatsapp.local"

            row_email = conn.execute("SELECT id, tags FROM contacts WHERE email = ?", (synth_email,)).fetchone()
            if row_email:
                cid = int(row_email["id"])
                cur_tags = [t.strip() for t in (row_email["tags"] or "").split(",") if t.strip()]
                if group_tag and group_tag.lower() not in {t.lower() for t in cur_tags}:
                    cur_tags.append(group_tag)
                    conn.execute("UPDATE contacts SET tags = ?, updated_at = ? WHERE id = ?", (", ".join(cur_tags), now, cid))
                conn.execute("UPDATE whatsapp_contacts SET contact_id = ? WHERE jid = ?", (cid, jid))
                linked += 1
                continue

            cur = conn.execute(
                """
                INSERT INTO contacts (
                    email, first_name, last_name, full_name, phone,
                    tags, source_type, source_detail, status, email_type, mx_status,
                    lead_score, workspace_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'whatsapp_group', ?, 'new', 'unknown', 'unchecked', 40, ?, ?, ?)
                """,
                (synth_email, first_name, last_name, full_name, phone, group_tag, f"Groupe WA: {subj}", workspace_id, now, now),
            )
            cid = cur.lastrowid
            conn.execute("UPDATE whatsapp_contacts SET contact_id = ? WHERE jid = ?", (cid, jid))
            inserted += 1

    return {"ok": True, "inserted": inserted, "linked": linked, "skipped": skipped, "tag": group_tag}
