import hashlib
import hmac
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import db
import executive_brief
import payments_hub
import wa_client


def _portal_secret() -> bytes:
    sec = db.get_or_create_local_secret(
        "portal_hmac_secret",
        env_var="CONTACTSHUB_PORTAL_SECRET",
    )
    return sec.encode("utf-8")


# ---------------------------------------------------------------------------
# #7 Voice-to-CRM (Speech Transcript -> Structured Note + Auto Follow-up Task)
# ---------------------------------------------------------------------------

def process_voice_note(contact_id: int, transcript: str) -> Dict[str, Any]:
    text = re.sub(r"\s+", " ", (transcript or "").strip())
    if not text:
        return {"ok": False, "error": "Transcription vocale vide."}

    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    lower = text.lower()

    # Detect follow-up intent & relative due date
    task_created: Optional[Dict[str, Any]] = None
    has_followup_cue = any(
        kw in lower
        for kw in (
            "relancer", "rappeler", "rappel", "envoyer", "devis", "proforma",
            "demain", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
            "semaine prochaine", "dans ", "rdv", "rendez-vous",
        )
    )
    due_date = ""
    if has_followup_cue:
        if "demain" in lower:
            due_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "après-demain" in lower or "apres-demain" in lower:
            due_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        elif "semaine prochaine" in lower:
            due_date = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            m_days = re.search(r"dans\s+(\d{1,2})\s+jour", lower)
            if m_days:
                due_date = (now + timedelta(days=min(60, int(m_days.group(1))))).strftime("%Y-%m-%d")
            else:
                due_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    with db.get_conn() as conn:
        c = conn.execute("SELECT id, full_name, email FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not c:
            return {"ok": False, "error": "Contact introuvable."}

        note_content = f"🎙️ Note vocale CRM : {text}"
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'call', ?, ?)",
            (contact_id, note_content, now_iso),
        )
        conn.execute("UPDATE contacts SET updated_at = ? WHERE id = ?", (now_iso, contact_id))

        if has_followup_cue:
            short_title = text[:90] + ("…" if len(text) > 90 else "")
            task_title = f"🎙️ Suivi vocal : {short_title}"
            cur = conn.execute(
                "INSERT INTO contact_tasks (contact_id, title, due_date, done, created_at) VALUES (?, ?, ?, 0, ?)",
                (contact_id, task_title, due_date, now_iso),
            )
            task_created = {"id": cur.lastrowid, "title": task_title, "due_date": due_date}

    return {
        "ok": True,
        "note": note_content,
        "task_created": task_created,
    }


# ---------------------------------------------------------------------------
# #8 Geo-Intelligence Map (City/Country clustering + phone prefix inference)
# ---------------------------------------------------------------------------

CITY_COORDS: Dict[str, Tuple[str, str, float, float]] = {
    "lome": ("Lomé", "Togo", 6.1375, 1.2123),
    "lomé": ("Lomé", "Togo", 6.1375, 1.2123),
    "togo": ("Lomé", "Togo", 6.1375, 1.2123),
    "kara": ("Kara", "Togo", 9.5511, 1.1861),
    "sokode": ("Sokodé", "Togo", 8.9833, 1.1333),
    "cotonou": ("Cotonou", "Bénin", 6.3703, 2.3912),
    "benin": ("Cotonou", "Bénin", 6.3703, 2.3912),
    "bénin": ("Cotonou", "Bénin", 6.3703, 2.3912),
    "abidjan": ("Abidjan", "Côte d'Ivoire", 5.3600, -4.0083),
    "cote d'ivoire": ("Abidjan", "Côte d'Ivoire", 5.3600, -4.0083),
    "côte d'ivoire": ("Abidjan", "Côte d'Ivoire", 5.3600, -4.0083),
    "accra": ("Accra", "Ghana", 5.6037, -0.1870),
    "ghana": ("Accra", "Ghana", 5.6037, -0.1870),
    "dakar": ("Dakar", "Sénégal", 14.7167, -17.4677),
    "senegal": ("Dakar", "Sénégal", 14.7167, -17.4677),
    "sénégal": ("Dakar", "Sénégal", 14.7167, -17.4677),
    "ouagadougou": ("Ouagadougou", "Burkina Faso", 12.3714, -1.5197),
    "burkina": ("Ouagadougou", "Burkina Faso", 12.3714, -1.5197),
    "lagos": ("Lagos", "Nigeria", 6.5244, 3.3792),
    "nigeria": ("Lagos", "Nigeria", 6.5244, 3.3792),
    "douala": ("Douala", "Cameroun", 4.0511, 9.7679),
    "yaounde": ("Yaoundé", "Cameroun", 3.8480, 11.5021),
    "cameroun": ("Douala", "Cameroun", 4.0511, 9.7679),
    "libreville": ("Libreville", "Gabon", 0.4162, 9.4673),
    "gabon": ("Libreville", "Gabon", 0.4162, 9.4673),
    "bamako": ("Bamako", "Mali", 12.6392, -8.0029),
    "mali": ("Bamako", "Mali", 12.6392, -8.0029),
    "niamey": ("Niamey", "Niger", 13.5116, 2.1254),
    "niger": ("Niamey", "Niger", 13.5116, 2.1254),
    "kinshasa": ("Kinshasa", "RD Congo", -4.4419, 15.2663),
    "casablanca": ("Casablanca", "Maroc", 33.5731, -7.5898),
    "maroc": ("Casablanca", "Maroc", 33.5731, -7.5898),
    "paris": ("Paris", "France", 48.8566, 2.3522),
    "france": ("Paris", "France", 48.8566, 2.3522),
    "lyon": ("Lyon", "France", 45.7640, 4.8357),
    "bruxelles": ("Bruxelles", "Belgique", 50.8503, 4.3517),
    "belgique": ("Bruxelles", "Belgique", 50.8503, 4.3517),
    "geneve": ("Genève", "Suisse", 46.2044, 6.1432),
    "suisse": ("Genève", "Suisse", 46.2044, 6.1432),
    "montreal": ("Montréal", "Canada", 45.5017, -73.5673),
    "canada": ("Montréal", "Canada", 45.5017, -73.5673),
    "london": ("Londres", "Royaume-Uni", 51.5074, -0.1278),
    "londres": ("Londres", "Royaume-Uni", 51.5074, -0.1278),
    "new york": ("New York", "États-Unis", 40.7128, -74.0060),
    "usa": ("New York", "États-Unis", 40.7128, -74.0060),
}

PHONE_PREFIX_GEO: List[Tuple[str, str]] = [
    ("+228", "lome"),
    ("+229", "cotonou"),
    ("+225", "abidjan"),
    ("+233", "accra"),
    ("+221", "dakar"),
    ("+226", "ouagadougou"),
    ("+234", "lagos"),
    ("+237", "douala"),
    ("+241", "libreville"),
    ("+223", "bamako"),
    ("+227", "niamey"),
    ("+243", "kinshasa"),
    ("+212", "casablanca"),
    ("+33", "paris"),
    ("+32", "bruxelles"),
    ("+41", "geneve"),
    ("+44", "london"),
    ("+1", "new york"),
]


def _resolve_geo(country: str, notes: str, tags: str, phone: str) -> Optional[Tuple[str, str, float, float]]:
    hay = f"{country} {tags} {notes}".lower()
    for key, val in CITY_COORDS.items():
        if key in hay:
            return val
    ph = re.sub(r"\s+", "", phone or "")
    for pref, city_key in PHONE_PREFIX_GEO:
        if ph.startswith(pref):
            return CITY_COORDS[city_key]
    return None


def get_geo_points(workspace_id: int = 1) -> Dict[str, Any]:
    ws = db.get_workspace(workspace_id)
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, full_name, email, phone, company, country, tags, notes, status, deal_value
            FROM contacts
            WHERE COALESCE(workspace_id, 1) = ?
            LIMIT 3000
            """,
            (workspace_id,),
        ).fetchall()

    clusters: Dict[str, Dict[str, Any]] = {}
    mapped = 0
    for r in rows:
        geo = _resolve_geo(r["country"] or "", r["notes"] or "", r["tags"] or "", r["phone"] or "")
        if not geo:
            continue
        mapped += 1
        city, country, lat, lng = geo
        k = f"{city}|{country}"
        if k not in clusters:
            clusters[k] = {
                "city": city,
                "country": country,
                "lat": lat,
                "lng": lng,
                "total_contacts": 0,
                "clients_count": 0,
                "pipeline_value": 0.0,
                "sample_names": [],
            }
        cl = clusters[k]
        cl["total_contacts"] += 1
        if r["status"] == "client":
            cl["clients_count"] += 1
        cl["pipeline_value"] += float(r["deal_value"] or 0)
        if len(cl["sample_names"]) < 4:
            cl["sample_names"].append(r["full_name"] or r["company"] or r["email"])

    points = sorted(clusters.values(), key=lambda x: x["total_contacts"], reverse=True)
    return {
        "currency": ws.get("currency") or "FCFA",
        "total_mapped": mapped,
        "cities_count": len(points),
        "points": points,
    }


# ---------------------------------------------------------------------------
# #9 Private VIP Client Portal (/mon-espace/<token>)
# ---------------------------------------------------------------------------

def make_portal_token(contact_id: int) -> str:
    msg = f"portal:{int(contact_id)}".encode("utf-8")
    sig = hmac.new(_portal_secret(), msg, hashlib.sha256).hexdigest()[:20]
    return f"{int(contact_id)}-{sig}"


def verify_portal_token(token: str) -> Optional[int]:
    if not token or "-" not in token:
        return None
    parts = token.strip().split("-", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    cid = int(parts[0])
    expected = make_portal_token(cid)
    if not hmac.compare_digest(token.strip(), expected):
        return None
    return cid


def get_portal_data(contact_id: int) -> Optional[Dict[str, Any]]:
    with db.get_conn() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not c:
            return None
        contact = dict(c)
        ws_id = int(contact.get("workspace_id") or 1)
        ws = db.get_workspace(ws_id)

        payments = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM student_payments
                WHERE contact_id = ? OR (LOWER(student_email) != '' AND LOWER(student_email) = LOWER(?))
                ORDER BY paid_at DESC, id DESC
                """,
                (contact_id, contact.get("email") or ""),
            ).fetchall()
        ]
        submissions = [
            dict(r)
            for r in conn.execute(
                """
                SELECT s.*, f.title AS form_title
                FROM form_submissions s
                LEFT JOIN lead_forms f ON f.id = s.form_id
                WHERE s.contact_id = ? OR (LOWER(s.email) != '' AND LOWER(s.email) = LOWER(?))
                ORDER BY s.id DESC
                """,
                (contact_id, contact.get("email") or ""),
            ).fetchall()
        ]
        quotes = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM quotes
                WHERE contact_id = ? OR (LOWER(client_email) != '' AND LOWER(client_email) = LOWER(?))
                ORDER BY id DESC
                """,
                (contact_id, contact.get("email") or ""),
            ).fetchall()
        ]
        appointments = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM appointments
                WHERE contact_id = ? OR (LOWER(email) != '' AND LOWER(email) = LOWER(?))
                ORDER BY appointment_date DESC, appointment_time DESC
                LIMIT 10
                """,
                (contact_id, contact.get("email") or ""),
            ).fetchall()
        ]

    total_paid = sum(float(p.get("amount_paid") or 0) for p in payments)
    # Compute remaining across distinct courses
    course_fees: Dict[str, float] = {}
    course_paid: Dict[str, float] = {}
    for p in payments:
        ck = (p.get("course_name") or "").strip().lower()
        course_fees[ck] = max(course_fees.get(ck, 0.0), float(p.get("total_fee") or 0))
        course_paid[ck] = course_paid.get(ck, 0.0) + float(p.get("amount_paid") or 0)
    total_remaining = sum(max(0.0, course_fees[k] - course_paid[k]) for k in course_fees)

    online_pay_url = ""
    if total_remaining > 0:
        online_pay_url = payments_hub.build_payment_link(
            amount=total_remaining,
            ref=f"PORTAL-{contact_id}",
            customer_name=contact.get("full_name") or contact.get("email") or "",
            workspace_id=ws_id,
        )

    return {
        "contact": {
            "id": contact["id"],
            "full_name": contact.get("full_name") or contact.get("email"),
            "email": contact.get("email") or "",
            "phone": contact.get("phone") or "",
            "company": contact.get("company") or "",
            "status": contact.get("status") or "new",
        },
        "workspace": ws,
        "payments": payments,
        "submissions": submissions,
        "quotes": quotes,
        "appointments": appointments,
        "summary": {
            "total_paid": total_paid,
            "total_remaining": total_remaining,
            "currency": ws.get("currency") or "FCFA",
        },
        "online_pay_url": online_pay_url,
        "referral": get_referral_stats(contact_id, workspace_id=ws_id),
    }


# ---------------------------------------------------------------------------
# #10 Real-Time Notification Center
# ---------------------------------------------------------------------------

def get_notifications(workspace_id: int = 1) -> Dict[str, Any]:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    since_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    items: List[Dict[str, Any]] = []

    with db.get_conn() as conn:
        # 1. Urgent / overdue tasks & WhatsApp escalations
        tasks = conn.execute(
            """
            SELECT t.id, t.title, t.due_date, t.created_at, c.full_name
            FROM contact_tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.done = 0 AND (t.due_date <= ? OR t.title LIKE '🚨%')
            ORDER BY t.id DESC
            LIMIT 8
            """,
            (today,),
        ).fetchall()
        for t in tasks:
            is_urgent = (t["title"] or "").startswith("🚨")
            items.append({
                "id": f"task-{t['id']}",
                "kind": "urgent" if is_urgent else "task",
                "icon": "🚨" if is_urgent else "⏰",
                "title": t["title"] or "Rappel à traiter",
                "subtitle": f"Contact : {t['full_name'] or '—'} · Échéance : {t['due_date'] or 'Aujourd’hui'}",
                "tab": "crm",
                "time": (t["created_at"] or "")[:16].replace("T", " "),
            })

        # 2. Recent payments
        pays = conn.execute(
            """
            SELECT id, receipt_num, student_name, amount_paid, payment_method, created_at
            FROM student_payments
            WHERE COALESCE(workspace_id, 1) = ? AND paid_at >= ?
            ORDER BY id DESC
            LIMIT 6
            """,
            (workspace_id, since_7d),
        ).fetchall()
        for p in pays:
            amt = f"{int(round(float(p['amount_paid'] or 0))):,}".replace(",", " ")
            items.append({
                "id": f"pay-{p['id']}",
                "kind": "payment",
                "icon": "💰",
                "title": f"Paiement encaissé : {amt} ({p['payment_method']})",
                "subtitle": f"{p['student_name']} · Reçu {p['receipt_num']}",
                "tab": "payments",
                "time": (p["created_at"] or "")[:16].replace("T", " "),
            })

        # 3. Upcoming appointments today
        appts = conn.execute(
            """
            SELECT id, full_name, topic, appointment_date, appointment_time, created_at
            FROM appointments
            WHERE COALESCE(workspace_id, 1) = ? AND status != 'cancelled' AND appointment_date >= ?
            ORDER BY appointment_date ASC, appointment_time ASC
            LIMIT 5
            """,
            (workspace_id, today),
        ).fetchall()
        for a in appts:
            items.append({
                "id": f"appt-{a['id']}",
                "kind": "booking",
                "icon": "📅",
                "title": f"RDV : {a['full_name']} ({a['appointment_date']} {a['appointment_time']})",
                "subtitle": a["topic"] or "Rendez-vous",
                "tab": "booking",
                "time": f"{a['appointment_date']} {a['appointment_time']}",
            })

        # 4. Hot new leads
        hot = conn.execute(
            """
            SELECT id, full_name, email, lead_score, created_at
            FROM contacts
            WHERE COALESCE(workspace_id, 1) = ? AND status = 'new' AND lead_score >= 70
            ORDER BY lead_score DESC, id DESC
            LIMIT 5
            """,
            (workspace_id,),
        ).fetchall()
        for h in hot:
            items.append({
                "id": f"hot-{h['id']}",
                "kind": "hot_lead",
                "icon": "🔥",
                "title": f"Lead chaud (Score {h['lead_score']}) : {h['full_name'] or h['email']}",
                "subtitle": "Nouveau prospect prioritaire à contacter",
                "tab": "contacts",
                "time": (h["created_at"] or "")[:16].replace("T", " "),
            })

    return {
        "count": len(items),
        "items": items[:20],
    }


# ---------------------------------------------------------------------------
# PACK 4: WAR ROOM TV, REVENUE FORECASTER, AT-RISK WIN-BACK & VIRAL REFERRALS
# ---------------------------------------------------------------------------

def get_war_room_live(workspace_id: int = 1) -> Dict[str, Any]:
    brief = executive_brief.generate_executive_brief(workspace_id)
    lb = executive_brief.compute_sales_leaderboard(workspace_id)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    with db.get_conn() as conn:
        today_rev = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount_paid), 0) FROM student_payments WHERE COALESCE(workspace_id, 1) = ? AND paid_at = ?",
                (workspace_id, today),
            ).fetchone()[0]
            or 0.0
        )
        today_leads = int(
            conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ? AND created_at LIKE ?",
                (workspace_id, f"{today}%"),
            ).fetchone()[0]
            or 0
        )
        recent_pays = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, receipt_num, student_name, course_name, amount_paid, payment_method, paid_at, created_at
                FROM student_payments
                WHERE COALESCE(workspace_id, 1) = ?
                ORDER BY id DESC
                LIMIT 8
                """,
                (workspace_id,),
            ).fetchall()
        ]
        recent_quotes = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, quote_num, client_name, client_company, total_amount, status, signed_by, signed_at
                FROM quotes
                WHERE COALESCE(workspace_id, 1) = ? AND status IN ('accepted', 'paid')
                ORDER BY id DESC
                LIMIT 5
                """,
                (workspace_id,),
            ).fetchall()
        ]

    top_reps = [r for r in (lb.get("leaderboard") or []) if r["username"] != "Non assigné"][:3]
    if not top_reps:
        top_reps = (lb.get("leaderboard") or [])[:3]

    return {
        "server_time": now.strftime("%H:%M:%S"),
        "server_date": now.strftime("%d/%m/%Y"),
        "workspace": brief["workspace"],
        "currency": brief["currency"],
        "kpis": brief["kpis"],
        "today_revenue": today_rev,
        "today_leads": today_leads,
        "top_reps": top_reps,
        "recent_payments": recent_pays,
        "recent_signed_quotes": recent_quotes,
    }


def compute_revenue_forecast(workspace_id: int = 1) -> Dict[str, Any]:
    """Baseline 30/60/90-day cashflow forecast + What-If simulator parameters."""
    ws = db.get_workspace(workspace_id)
    currency = ws.get("currency") or "FCFA"
    now = datetime.now()
    since_30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    with db.get_conn() as conn:
        rev_30d = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount_paid), 0) FROM student_payments WHERE COALESCE(workspace_id, 1) = ? AND paid_at >= ?",
                (workspace_id, since_30),
            ).fetchone()[0]
            or 0.0
        )
        avg_pay_row = conn.execute(
            "SELECT AVG(amount_paid) FROM student_payments WHERE COALESCE(workspace_id, 1) = ? AND amount_paid > 0",
            (workspace_id,),
        ).fetchone()
        avg_ticket = float(avg_pay_row[0] or 35000.0)

        leads_30d = int(
            conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ? AND created_at >= ?",
                (workspace_id, since_30),
            ).fetchone()[0]
            or 0
        )
        total_contacts = int(
            conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ?",
                (workspace_id,),
            ).fetchone()[0]
            or 0
        )
        clients_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ? AND status = 'client'",
                (workspace_id,),
            ).fetchone()[0]
            or 0
        )
        open_pipeline = float(
            conn.execute(
                "SELECT COALESCE(SUM(deal_value), 0) FROM contacts WHERE COALESCE(workspace_id, 1) = ? AND status NOT IN ('client', 'unsubscribed')",
                (workspace_id,),
            ).fetchone()[0]
            or 0.0
        )
        open_quotes = float(
            conn.execute(
                "SELECT COALESCE(SUM(total_amount), 0) FROM quotes WHERE COALESCE(workspace_id, 1) = ? AND status IN ('sent', 'accepted')",
                (workspace_id,),
            ).fetchone()[0]
            or 0.0
        )

    unpaid = payments_hub.list_unpaid_balances(workspace_id)
    unpaid_total = sum(float(u.get("remaining_balance") or 0) for u in unpaid)

    conv_rate = round((clients_count / total_contacts) * 100.0, 1) if total_contacts > 0 else 18.0
    conv_rate = max(5.0, min(85.0, conv_rate))
    monthly_leads = max(10, leads_30d)
    # Weighted 30-day baseline = recent velocity + 45% unpaid recovery + 35% open quotes + 20% open pipeline
    base_30 = round(max(rev_30d, monthly_leads * (conv_rate / 100.0) * avg_ticket) + unpaid_total * 0.45 + open_quotes * 0.35 + open_pipeline * 0.15)
    base_60 = round(base_30 * 2.05)
    base_90 = round(base_30 * 3.15)

    return {
        "currency": currency,
        "rev_30d_actual": round(rev_30d),
        "unpaid_recoverable": round(unpaid_total),
        "open_quotes_value": round(open_quotes),
        "open_pipeline_value": round(open_pipeline),
        "avg_ticket": round(avg_ticket),
        "conversion_rate": conv_rate,
        "monthly_leads": monthly_leads,
        "forecast_30d": base_30,
        "forecast_60d": base_60,
        "forecast_90d": base_90,
    }


def detect_at_risk_revenue(workspace_id: int = 1) -> Dict[str, Any]:
    """Radar 'CA en Danger' : detects stale quotes, untouched hot leads & dormant clients."""
    ws = db.get_workspace(workspace_id)
    currency = ws.get("currency") or "FCFA"
    now = datetime.now()
    cut_quote = (now - timedelta(days=5)).isoformat(timespec="seconds")
    cut_hot = (now - timedelta(days=7)).isoformat(timespec="seconds")
    cut_client = (now - timedelta(days=45)).isoformat(timespec="seconds")
    risks: List[Dict[str, Any]] = []

    with db.get_conn() as conn:
        # 1. Unsigned quotes > 5 days
        qs = conn.execute(
            """
            SELECT id, quote_num, client_name, client_phone, title, total_amount, created_at
            FROM quotes
            WHERE COALESCE(workspace_id, 1) = ? AND status = 'sent' AND created_at <= ?
            ORDER BY total_amount DESC LIMIT 8
            """,
            (workspace_id, cut_quote),
        ).fetchall()
        for q in qs:
            risks.append({
                "kind": "stale_quote",
                "item_id": q["id"],
                "icon": "📄",
                "label": f"Devis sans réponse : {q['quote_num']} — {q['client_name']}",
                "detail": q["title"],
                "phone": q["client_phone"] or "",
                "amount": float(q["total_amount"] or 0),
                "since": (q["created_at"] or "")[:10],
            })

        # 2. Hot leads untouched > 7 days
        hots = conn.execute(
            """
            SELECT id, full_name, email, phone, company, deal_value, lead_score, updated_at
            FROM contacts
            WHERE COALESCE(workspace_id, 1) = ? AND status IN ('new', 'followup')
              AND lead_score >= 70 AND updated_at <= ?
            ORDER BY deal_value DESC, lead_score DESC LIMIT 8
            """,
            (workspace_id, cut_hot),
        ).fetchall()
        for h in hots:
            est = float(h["deal_value"] or 35000)
            risks.append({
                "kind": "stale_hot_lead",
                "item_id": h["id"],
                "icon": "🔥",
                "label": f"Lead chaud refroidi (Score {h['lead_score']}) : {h['full_name'] or h['email']}",
                "detail": h["company"] or "Relance commerciale prioritaire",
                "phone": h["phone"] or "",
                "amount": est,
                "since": (h["updated_at"] or "")[:10],
            })

        # 3. Dormant clients > 45 days
        dormant = conn.execute(
            """
            SELECT id, full_name, email, phone, company, deal_value, updated_at
            FROM contacts
            WHERE COALESCE(workspace_id, 1) = ? AND status = 'client' AND updated_at <= ?
            ORDER BY deal_value DESC LIMIT 6
            """,
            (workspace_id, cut_client),
        ).fetchall()
        for d in dormant:
            est = float(d["deal_value"] or 30000)
            risks.append({
                "kind": "dormant_client",
                "item_id": d["id"],
                "icon": "💤",
                "label": f"Client dormant (>45j) : {d['full_name'] or d['email']}",
                "detail": "Offre fidélité / niveau supérieur à proposer",
                "phone": d["phone"] or "",
                "amount": est,
                "since": (d["updated_at"] or "")[:10],
            })

    total_at_risk = round(sum(r["amount"] for r in risks))
    return {
        "currency": currency,
        "count": len(risks),
        "total_at_risk": total_at_risk,
        "items": risks,
    }


def send_winback_whatsapp(kind: str, item_id: int, workspace_id: int = 1) -> Dict[str, Any]:
    ws = db.get_workspace(workspace_id)
    phone = ""
    msg = ""
    with db.get_conn() as conn:
        if kind == "stale_quote":
            q = conn.execute("SELECT * FROM quotes WHERE id = ?", (item_id,)).fetchone()
            if not q:
                return {"ok": False, "error": "Devis introuvable."}
            phone = q["client_phone"] or ""
            first = (q["client_name"] or "").split(" ")[0] or "Bonjour"
            msg = (
                f"Bonjour {first} 👋\n"
                f"Je reviens vers vous de la part de *{ws['name']}* suite à notre offre *{q['title']}* (Réf {q['quote_num']}).\n"
                f"Avez-vous des questions pour valider et démarrer ? Nous restons disponibles ici !"
            )
        else:
            c = conn.execute("SELECT * FROM contacts WHERE id = ?", (item_id,)).fetchone()
            if not c:
                return {"ok": False, "error": "Contact introuvable."}
            phone = c["phone"] or ""
            first = (c["first_name"] or c["full_name"] or "").split(" ")[0] or "Bonjour"
            if kind == "dormant_client":
                msg = (
                    f"Bonjour {first} 👋\n"
                    f"Toute l'équipe de *{ws['name']}* espère que vous allez bien ! "
                    f"En tant qu'ancien(ne) client(e) VIP, vous bénéficiez d'un accès privilégié sur notre prochaine session. Souhaitez-vous recevoir le programme ?"
                )
            else:
                msg = (
                    f"Bonjour {first} 👋\n"
                    f"Suite à votre intérêt pour *{ws['name']}*, nos prochaines places se remplissent vite. "
                    f"Souhaitez-vous réserver un échange rapide ou un test de niveau gratuit cette semaine ?"
                )
    if not phone:
        return {"ok": False, "error": "Aucun numéro WhatsApp renseigné pour ce contact."}
    res = wa_client.bridge_post("/send", {"phone": phone, "text": msg}, timeout=15.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "WhatsApp non connecté."}
    return {"ok": True}


def ensure_referral_code(contact_id: int) -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT id, referral_code FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            return ""
        code = (row["referral_code"] or "").strip()
        if code:
            return code
        tag_hash = hashlib.sha256(f"ref:{contact_id}".encode("utf-8")).hexdigest()[:4].upper()
        code = f"VIP{contact_id}{tag_hash}"
        conn.execute("UPDATE contacts SET referral_code = ? WHERE id = ?", (code, contact_id))
        return code


def attribute_referral(new_contact_id: int, ref_code: str) -> bool:
    code = (ref_code or "").strip().upper()
    if not code or not new_contact_id:
        return False
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        parrain = conn.execute("SELECT id, full_name FROM contacts WHERE UPPER(referral_code) = ?", (code,)).fetchone()
        if not parrain or int(parrain["id"]) == int(new_contact_id):
            return False
        conn.execute("UPDATE contacts SET referred_by = ? WHERE id = ? AND COALESCE(referred_by, '') = ''", (code, new_contact_id))
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'note', ?, ?)",
            (int(parrain["id"]), f"🔥 Nouveau filleul parrainé via votre code {code} ! (+100 pts ambassadeur)", now),
        )
    return True


def get_referral_stats(contact_id: int, workspace_id: int = 1) -> Dict[str, Any]:
    code = ensure_referral_code(contact_id)
    with db.get_conn() as conn:
        cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE UPPER(referred_by) = ?",
                (code.upper(),),
            ).fetchone()[0]
            or 0
        )
        form_row = conn.execute(
            "SELECT slug FROM lead_forms WHERE form_kind = 'training' ORDER BY id ASC LIMIT 1"
        ).fetchone()
    share_path = f"/f/{form_row['slug']}?ref={code}" if form_row else f"/rdv?ref={code}"
    return {
        "code": code,
        "referrals_count": cnt,
        "points": cnt * 100,
        "share_path": share_path,
    }


def get_referral_leaderboard(workspace_id: int = 1) -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.full_name, p.email, p.referral_code, COUNT(f.id) AS referrals_count
            FROM contacts p
            JOIN contacts f ON UPPER(f.referred_by) = UPPER(p.referral_code)
            WHERE COALESCE(p.workspace_id, 1) = ? AND COALESCE(p.referral_code, '') != ''
            GROUP BY p.id
            ORDER BY referrals_count DESC
            LIMIT 10
            """,
            (workspace_id,),
        ).fetchall()
    return [dict(r) for r in rows]

