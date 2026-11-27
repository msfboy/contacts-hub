import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import db
import licensing
import payments_hub
import premium_hub
import wa_client

DEFAULT_EVENT_SLUG = "lome-digital-ai-summit"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_default_event(workspace_id: int = 1) -> Dict[str, Any]:
    """Seed the default Lomé VIP Launch Event if none exists."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vip_events WHERE COALESCE(workspace_id, 1) = ? ORDER BY id ASC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if row:
            return _hydrate_event(dict(row))

        default_date = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
        speakers = [
            {
                "name": "Mawougna Yao Ossentini",
                "role": "Fondateur & Architecte CRM / IA Commerciale",
                "topic": "Démonstration Live : Automatiser sa prospection, son WhatsApp IA et ses encaissements",
            },
            {
                "name": "Koutandji Simpa Ignace",
                "role": "Expert Croissance & Stratégie Entreprise",
                "topic": "Structurer son entreprise et multiplier son chiffre d'affaires avec les bons systèmes",
            },
            {
                "name": "Justin Padaki",
                "role": "Expert Acquisition Digitale & Conversion",
                "topic": "Attirer des clients qualifiés chaque jour et transformer ses prospects en acheteurs",
            },
        ]
        perks = [
            "🔑 7 jours d'accès GRATUIT à la plateforme complète ContactsHub SaaS (clé activée automatiquement)",
            "👕 1 T-Shirt Officiel VIP offert (votre taille réservée à l'inscription)",
            "🎤 Workshop exclusif animé par 3 Experts + Démonstration Live en conditions réelles",
            "🤝 Networking VIP avec dirigeants, entrepreneurs et décideurs de Lomé",
        ]
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO vip_events (
                slug, title, subtitle, event_date, event_time, location,
                price, currency, capacity, speakers_json, perks_json,
                trial_days, workspace_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DEFAULT_EVENT_SLUG,
                "Lomé Digital & AI Summit — Présentation Officielle",
                "Le Workshop exclusif qui transforme votre gestion client, vos ventes WhatsApp et vos encaissements grâce à l'IA.",
                default_date,
                "09:00",
                "Carrefour Adroukpapé, Avedji Limosine — Lomé, Togo",
                10000.0,
                "FCFA",
                100,
                json.dumps(speakers, ensure_ascii=False),
                json.dumps(perks, ensure_ascii=False),
                7,
                workspace_id,
                now,
            ),
        )
        row2 = conn.execute("SELECT * FROM vip_events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _hydrate_event(dict(row2))


def _hydrate_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ev["speakers"] = json.loads(ev.get("speakers_json") or "[]")
    except Exception:
        ev["speakers"] = []
    try:
        ev["perks"] = json.loads(ev.get("perks_json") or "[]")
    except Exception:
        ev["perks"] = []
    with db.get_conn() as conn:
        cnt_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN payment_status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
                   SUM(CASE WHEN checked_in = 1 THEN 1 ELSE 0 END) AS checked_in_count,
                   COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN amount_paid ELSE 0 END), 0) AS revenue
            FROM event_registrations
            WHERE event_id = ?
            """,
            (ev["id"],),
        ).fetchone()
        size_rows = conn.execute(
            "SELECT tshirt_size, COUNT(*) AS c FROM event_registrations WHERE event_id = ? GROUP BY tshirt_size",
            (ev["id"],),
        ).fetchall()
    ev["registered_count"] = int(cnt_row["total"] or 0)
    ev["paid_count"] = int(cnt_row["paid_count"] or 0)
    ev["checked_in_count"] = int(cnt_row["checked_in_count"] or 0)
    ev["revenue"] = float(cnt_row["revenue"] or 0.0)
    ev["seats_left"] = max(0, int(ev.get("capacity") or 100) - ev["registered_count"])
    ev["tshirt_counts"] = {r["tshirt_size"]: int(r["c"]) for r in size_rows}
    return ev


def get_event_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    ensure_default_event(1)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM vip_events WHERE slug = ?", (slug.strip(),)).fetchone()
    return _hydrate_event(dict(row)) if row else None


def update_event(event_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    fields = []
    params: List[Any] = []
    for col in ("title", "subtitle", "event_date", "event_time", "location", "currency"):
        if col in data and data[col] is not None:
            fields.append(f"{col} = ?")
            params.append(str(data[col]).strip())
    for col in ("capacity", "trial_days"):
        if col in data and data[col] is not None:
            fields.append(f"{col} = ?")
            params.append(max(1, int(data[col])))
    if "price" in data and data["price"] is not None:
        fields.append("price = ?")
        params.append(max(0.0, float(data["price"])))
    if fields:
        params.append(event_id)
        with db.get_conn() as conn:
            conn.execute(f"UPDATE vip_events SET {', '.join(fields)} WHERE id = ?", params)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM vip_events WHERE id = ?", (event_id,)).fetchone()
    return _hydrate_event(dict(row)) if row else {}


def register_attendee(slug: str, data: Dict[str, Any]) -> Dict[str, Any]:
    ev = get_event_by_slug(slug)
    if not ev:
        return {"ok": False, "error": "Événement introuvable."}
    if ev["seats_left"] <= 0:
        return {"ok": False, "error": "Désolé, toutes les places VIP sont réservées !"}

    full_name = (data.get("full_name") or "").strip()
    phone = wa_client.format_plus_phone(data.get("phone") or "") if data.get("phone") else ""
    email = (data.get("email") or "").strip().lower()
    company = (data.get("company") or "").strip()
    tshirt = (data.get("tshirt_size") or "L").strip().upper()
    if tshirt not in ("S", "M", "L", "XL", "XXL"):
        tshirt = "L"
    pay_method = (data.get("payment_method") or "TMoney").strip()
    pay_ref = (data.get("payment_ref") or "").strip()
    ref_code = (data.get("ref") or data.get("referred_by") or "").strip().upper()

    if not full_name or not phone:
        return {"ok": False, "error": "Nom complet et numéro WhatsApp requis."}

    ws_id = int(ev.get("workspace_id") or 1)
    synth_email = email if (email and "@" in email) else f"evt.{secrets.token_hex(4)}@event.local"
    db.upsert_contacts(
        records=[{
            "email": synth_email,
            "full_name": full_name,
            "phone": phone,
            "company": company,
            "status": "contacted",
            "deal_value": float(ev.get("price") or 10000),
            "notes": f"Inscrit Événement VIP : {ev['title']} (T-Shirt {tshirt})",
        }],
        source_type="event",
        source_name=ev["title"],
        extra_tag=f"Événement VIP, T-Shirt {tshirt}",
        workspace_id=ws_id,
    )

    now = _now()
    ticket_code = f"VIP-{datetime.now().strftime('%m%d')}-{secrets.token_hex(3).upper()}"
    cid = None
    with db.get_conn() as conn:
        c_row = conn.execute("SELECT id FROM contacts WHERE email = ?", (synth_email,)).fetchone()
        if not c_row and phone:
            cid = wa_client.find_contact_id_by_phone(conn, phone)
        elif c_row:
            cid = int(c_row["id"])

        cur = conn.execute(
            """
            INSERT INTO event_registrations (
                event_id, ticket_code, contact_id, full_name, phone, email,
                company, tshirt_size, payment_method, payment_ref, payment_status,
                amount_paid, checked_in, checked_in_at, trial_license_key, referred_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, '', '', ?, ?)
            """,
            (
                ev["id"], ticket_code, cid, full_name, phone, email,
                company, tshirt, pay_method, pay_ref, ref_code, now,
            ),
        )
        reg_id = cur.lastrowid

    if cid and ref_code:
        try:
            premium_hub.attribute_referral(cid, ref_code)
        except Exception:
            pass

    return {
        "ok": True,
        "id": reg_id,
        "ticket_code": ticket_code,
        "contact_id": cid,
    }


def confirm_registration_payment(reg_id: int, payment_ref: str = "") -> Dict[str, Any]:
    """Mark an event registration as PAID, record payment receipt & generate 7-day SaaS trial key."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM event_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Inscription introuvable."}
        reg = dict(row)
        ev_row = conn.execute("SELECT * FROM vip_events WHERE id = ?", (reg["event_id"],)).fetchone()
        ev = dict(ev_row) if ev_row else {"title": "Événement VIP", "price": 10000, "trial_days": 7, "workspace_id": 1}

    price = float(ev.get("price") or 10000)
    trial_days = int(ev.get("trial_days") or 7)
    trial_key = reg.get("trial_license_key") or ""
    if not trial_key:
        exp = (datetime.now() + timedelta(days=trial_days)).strftime("%Y-%m-%d")
        lic = licensing.generate_license_key(
            client_name=reg["full_name"],
            plan="pro",
            expires_on=exp,
            max_contacts=5000,
        )
        trial_key = lic.get("license_key") or ""

    ref_final = (payment_ref or reg.get("payment_ref") or reg["ticket_code"]).strip()
    if reg.get("payment_status") != "paid":
        payments_hub.create_payment({
            "contact_id": reg.get("contact_id"),
            "student_name": reg["full_name"],
            "student_phone": reg["phone"],
            "student_email": reg.get("email") or "",
            "course_name": f"Ticket VIP — {ev['title']}",
            "total_fee": price,
            "amount_paid": price,
            "payment_method": reg.get("payment_method") or "TMoney",
            "payment_ref": ref_final,
            "notes": f"Billet {reg['ticket_code']} · T-Shirt {reg['tshirt_size']} · Licence {trial_days}j",
            "workspace_id": int(ev.get("workspace_id") or 1),
        })

    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE event_registrations SET
                payment_status = 'paid',
                amount_paid = ?,
                payment_ref = ?,
                trial_license_key = ?
            WHERE id = ?
            """,
            (price, ref_final, trial_key, reg_id),
        )

    # Best-effort WhatsApp confirmation with 7-day trial key
    if reg.get("phone"):
        msg = (
            f"🎟️ *BILLET VIP CONFIRMÉ — {ev['title']}*\n"
            f"Félicitations {reg['full_name']} !\n"
            f"• N° Billet : *{reg['ticket_code']}*\n"
            f"• Statut : *PAYÉ ({int(price):,} FCFA)* ✅\n"
            f"• T-Shirt réservé : *Taille {reg['tshirt_size']}*\n"
            f"• Lieu : {ev.get('location') or 'Lomé'}\n\n"
            f"🔑 *Votre Clé d'Essai Gratuit ({trial_days} jours) :*\n`{trial_key}`\n\n"
            f"Présentez votre QR Code à l'entrée le jour J. À très vite ! 🚀"
        ).replace(",", " ")
        try:
            wa_client.bridge_post("/send", {"phone": reg["phone"], "text": msg}, timeout=10.0)
        except Exception:
            pass

    return {"ok": True, "ticket_code": reg["ticket_code"], "trial_license_key": trial_key}


def checkin_ticket(ticket_code: str, auto_confirm_payment: bool = False) -> Dict[str, Any]:
    code = (ticket_code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "Code billet vide."}
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM event_registrations WHERE UPPER(ticket_code) = ?", (code,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Billet '{code}' introuvable."}
        reg = dict(row)

    if auto_confirm_payment and reg.get("payment_status") != "paid":
        confirm_registration_payment(reg["id"])
        with db.get_conn() as conn:
            reg = dict(conn.execute("SELECT * FROM event_registrations WHERE id = ?", (reg["id"],)).fetchone())

    already = bool(reg.get("checked_in"))
    now = _now()
    if not already:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE event_registrations SET checked_in = 1, checked_in_at = ? WHERE id = ?",
                (now, reg["id"]),
            )
            reg["checked_in"] = 1
            reg["checked_in_at"] = now
    return {
        "ok": True,
        "already_checked_in": already,
        "registration": reg,
    }


def get_ticket_data(ticket_code: str) -> Optional[Dict[str, Any]]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM event_registrations WHERE UPPER(ticket_code) = ?", (ticket_code.strip().upper(),)).fetchone()
        if not row:
            return None
        reg = dict(row)
        ev_row = conn.execute("SELECT * FROM vip_events WHERE id = ?", (reg["event_id"],)).fetchone()
        if not ev_row:
            return None
        ev = _hydrate_event(dict(ev_row))
    ws_id = int(ev.get("workspace_id") or 1)
    ws = db.get_workspace(ws_id)
    ref_code = premium_hub.ensure_referral_code(int(reg["contact_id"])) if reg.get("contact_id") else ""
    pay_url = payments_hub.build_payment_link(
        amount=float(ev.get("price") or 10000),
        ref=reg["ticket_code"],
        customer_name=reg["full_name"],
        workspace_id=ws_id,
    )
    return {
        "registration": reg,
        "event": ev,
        "workspace": ws,
        "referral_code": ref_code,
        "online_pay_url": pay_url,
    }


def list_registrations(event_id: int) -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM event_registrations WHERE event_id = ? ORDER BY id DESC",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]
