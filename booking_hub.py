import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import db
import wa_client

DEFAULT_SLOTS = ["09:00", "10:30", "12:00", "14:00", "15:30", "17:00", "18:30"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_booking_settings() -> Dict[str, Any]:
    with db.get_conn() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = 1").fetchone()
        ws_d = dict(ws) if ws else {"name": "MSF'S English Learning Academy", "tagline": "Centre de Formation", "primary_color": "#2563eb"}
        slots_r = conn.execute("SELECT value FROM app_settings WHERE key = 'booking_slots'").fetchone()
        loc_r = conn.execute("SELECT value FROM app_settings WHERE key = 'booking_location'").fetchone()
        topics_r = conn.execute("SELECT value FROM app_settings WHERE key = 'booking_topics'").fetchone()

    slots = [s.strip() for s in (slots_r["value"] if slots_r else ",".join(DEFAULT_SLOTS)).split(",") if s.strip()]
    location = loc_r["value"] if loc_r and loc_r["value"].strip() else "Lomé — Carrefour Adroukpapé, Avedji Limosine"
    topics = [
        t.strip()
        for t in (
            topics_r["value"]
            if topics_r and topics_r["value"].strip()
            else "Test de niveau d'anglais gratuit\nEntretien d'inscription\nAudit Formation Entreprise (B2B)\nCoaching IELTS / TOEFL"
        ).splitlines()
        if t.strip()
    ]
    return {
        "workspace": ws_d,
        "slots": slots or DEFAULT_SLOTS,
        "location": location,
        "topics": topics,
    }


def save_booking_settings(slots_str: str, location: str, topics_str: str) -> Dict[str, Any]:
    with db.get_conn() as conn:
        for k, v in [
            ("booking_slots", slots_str.strip()),
            ("booking_location", location.strip()),
            ("booking_topics", topics_str.strip()),
        ]:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )
    return get_booking_settings()


def get_booked_slots(date_str: str) -> List[str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT appointment_time FROM appointments WHERE appointment_date = ? AND status != 'cancelled'",
            (date_str,),
        ).fetchall()
    return [r["appointment_time"] for r in rows]


def _send_wa_async(phone: str, text: str) -> None:
    def _bg():
        try:
            if not wa_client.is_bridge_alive():
                return
            st = wa_client.bridge_get("/status", timeout=2.0)
            if st.get("state") != "connected":
                return
            wa_client.bridge_post("/send", {"phone": phone, "text": text}, timeout=12.0)
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True).start()


def create_appointment(data: Dict[str, Any]) -> Dict[str, Any]:
    import sqlite3 as _sqlite3

    full_name = (data.get("full_name") or "").strip()[:120]
    phone = wa_client.format_plus_phone(data.get("phone") or "")
    email = (data.get("email") or "").strip().lower()[:160]
    topic = (data.get("topic") or "Test de niveau d'anglais gratuit").strip()[:160]
    mode = (data.get("mode") or "Présentiel").strip()[:60]
    appt_date = (data.get("appointment_date") or "").strip()
    appt_time = (data.get("appointment_time") or "").strip()
    notes = (data.get("notes") or "").strip()[:500]
    ws_id = int(data.get("workspace_id") or 1)

    if not full_name or not phone or not appt_date or not appt_time:
        return {"ok": False, "error": "Nom complet, téléphone WhatsApp, date et heure sont requis."}

    try:
        parsed_date = datetime.strptime(appt_date, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "error": "Format de date invalide (YYYY-MM-DD attendu)."}
    if parsed_date < datetime.now().date():
        return {"ok": False, "error": "Impossible de réserver une date passée."}

    cfg = get_booking_settings()
    allowed_slots = cfg.get("slots") or []
    if allowed_slots and appt_time not in allowed_slots:
        return {"ok": False, "error": f"Créneau horaire invalide : {appt_time}."}

    if appt_time in get_booked_slots(appt_date):
        return {"ok": False, "error": f"Le créneau {appt_time} du {appt_date} est déjà réservé. Choisissez une autre heure."}

    booking_ref = f"RDV-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    now = _now()
    digits = wa_client.normalize_digits(phone)
    crm_email = email or f"rdv.{digits}@booking.local"

    db.upsert_contacts(
        records=[{
            "email": crm_email,
            "full_name": full_name,
            "phone": phone,
            "notes": f"RDV {booking_ref} : {topic} ({mode}) le {appt_date} à {appt_time}. {notes}".strip(),
            "status": "followup",
        }],
        source_type="booking",
        source_name=f"RDV · {topic}",
        source_ref=booking_ref,
        extra_tag="RDV Test Niveau",
    )

    try:
        with db.get_conn() as conn:
            c = conn.execute("SELECT id FROM contacts WHERE email = ?", (crm_email,)).fetchone()
            cid = int(c["id"]) if c else None
            cur = conn.execute(
                """
                INSERT INTO appointments (
                    booking_ref, contact_id, full_name, phone, email, topic, mode,
                    appointment_date, appointment_time, status, notes, workspace_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)
                """,
                (booking_ref, cid, full_name, phone, email, topic, mode, appt_date, appt_time, notes, ws_id, now),
            )
            aid = cur.lastrowid
            if cid:
                conn.execute(
                    "UPDATE contacts SET lead_score = MAX(COALESCE(lead_score, 0), 92), workspace_id = COALESCE(workspace_id, ?), updated_at = ? WHERE id = ?",
                    (ws_id, now, cid),
                )
                conn.execute(
                    "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'meeting', ?, ?)",
                    (cid, f"📅 RDV confirmé ({booking_ref}) : {topic} ({mode}) le {appt_date} à {appt_time}", now),
                )
                conn.execute(
                    "INSERT INTO contact_tasks (contact_id, title, due_date, done, created_at) VALUES (?, ?, ?, 0, ?)",
                    (cid, f"RDV {appt_time} — {topic} ({full_name})", appt_date, now),
                )
    except _sqlite3.IntegrityError:
        return {"ok": False, "error": f"Le créneau {appt_time} du {appt_date} vient d'être réservé. Choisissez une autre heure."}

    ws_name = cfg["workspace"].get("name") or "MSF'S English Learning Academy"
    loc = cfg["location"]
    first = full_name.split(" ")[0]
    wa_msg = (
        f"Bonjour {first} 👋\n"
        f"✅ Votre rendez-vous est confirmé auprès de *{ws_name}* !\n\n"
        f"• Réf : *{booking_ref}*\n"
        f"• Objet : {topic}\n"
        f"• Date : *{appt_date} à {appt_time}*\n"
        f"• Mode : {mode}" + (f"\n• Lieu : {loc}" if "présentiel" in mode.lower() else "") + "\n\n"
        f"À très bientôt ! 🙏"
    )
    _send_wa_async(phone, wa_msg)
    try:
        import workflows_engine
        workflows_engine.fire_event("booking_created", {
            "contact_id": cid,
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "course_name": topic,
        })
    except Exception:
        pass

    return {"ok": True, "id": aid, "booking_ref": booking_ref}


def list_appointments(status: str = "") -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = ? ORDER BY appointment_date DESC, appointment_time DESC LIMIT 200",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appointments ORDER BY appointment_date DESC, appointment_time DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]


def update_appointment_status(aid: int, status: str) -> Dict[str, Any]:
    if status not in ("confirmed", "completed", "cancelled"):
        return {"ok": False, "error": "Statut invalide"}
    with db.get_conn() as conn:
        conn.execute("UPDATE appointments SET status = ? WHERE id = ?", (status, aid))
    return {"ok": True}


def send_appointment_reminder_wa(aid: int) -> Dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    if not row:
        return {"ok": False, "error": "Rendez-vous introuvable."}
    a = dict(row)
    cfg = get_booking_settings()
    ws_name = cfg["workspace"].get("name") or "MSF'S English Learning Academy"
    first = (a["full_name"] or "").split(" ")[0]
    msg = (
        f"Bonjour {first} 👋\n"
        f"⏰ Petit rappel de votre rendez-vous avec *{ws_name}* :\n"
        f"• Objet : {a['topic']}\n"
        f"• Date : *{a['appointment_date']} à {a['appointment_time']}*\n"
        f"• Mode : {a['mode']}" + (f"\n• Lieu : {cfg['location']}" if "présentiel" in (a["mode"] or "").lower() else "") + "\n\n"
        f"Merci de nous confirmer votre présence en répondant simplement *OUI* 👍"
    )
    res = wa_client.bridge_post("/send", {"phone": a["phone"], "text": msg}, timeout=15.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "WhatsApp non connecté."}
    return {"ok": True}


def process_due_appointment_reminders() -> Dict[str, Any]:
    """Automatically send J-1 and H-2 WhatsApp reminders for upcoming confirmed appointments."""
    if not wa_client.is_bridge_alive():
        return {"checked": 0, "sent_j1": 0, "sent_h2": 0}
    st = wa_client.bridge_get("/status", timeout=2.0)
    if st.get("state") != "connected":
        return {"checked": 0, "sent_j1": 0, "sent_h2": 0}

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    sent_j1 = 0
    sent_h2 = 0

    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM appointments
                WHERE status = 'confirmed' AND appointment_date IN (?, ?)
                  AND (COALESCE(reminded_j1, 0) = 0 OR COALESCE(reminded_h2, 0) = 0)
                """,
                (today_str, tomorrow_str),
            ).fetchall()
        ]

    for a in rows:
        if not a.get("phone"):
            continue
        if a["appointment_date"] == tomorrow_str and not a.get("reminded_j1") and 8 <= now.hour <= 21:
            res = send_appointment_reminder_wa(a["id"])
            if res.get("ok"):
                with db.get_conn() as conn:
                    conn.execute("UPDATE appointments SET reminded_j1 = 1 WHERE id = ?", (a["id"],))
                sent_j1 += 1
        elif a["appointment_date"] == today_str and not a.get("reminded_h2"):
            try:
                slot_dt = datetime.strptime(f"{a['appointment_date']} {a['appointment_time'][:5]}", "%Y-%m-%d %H:%M")
                diff_min = (slot_dt - now).total_seconds() / 60.0
                if 15 <= diff_min <= 150:
                    res = send_appointment_reminder_wa(a["id"])
                    if res.get("ok"):
                        with db.get_conn() as conn:
                            conn.execute("UPDATE appointments SET reminded_h2 = 1 WHERE id = ?", (a["id"],))
                        sent_h2 += 1
            except ValueError:
                pass

    return {"checked": len(rows), "sent_j1": sent_j1, "sent_h2": sent_h2}
