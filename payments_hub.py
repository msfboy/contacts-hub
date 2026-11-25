import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

import db
import wa_client


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fmt_money(amount: float, currency: str = "FCFA") -> str:
    return f"{int(round(float(amount or 0))):,}".replace(",", " ") + f" {currency}"


def create_payment(data: Dict[str, Any]) -> Dict[str, Any]:
    student_name = (data.get("student_name") or "").strip()
    course_name = (data.get("course_name") or "").strip()
    try:
        amount_paid = float(data.get("amount_paid") or 0)
    except Exception:
        amount_paid = 0.0
    try:
        total_fee = float(data.get("total_fee") or 0)
    except Exception:
        total_fee = 0.0

    if not student_name or not course_name or amount_paid <= 0:
        return {"ok": False, "error": "Nom de l'apprenant, formation et montant payé (> 0) requis."}

    student_phone = wa_client.format_plus_phone(data.get("student_phone") or "") if data.get("student_phone") else ""
    student_email = (data.get("student_email") or "").strip().lower()
    method = (data.get("payment_method") or "TMoney").strip()
    ref = (data.get("payment_ref") or "").strip()
    notes = (data.get("notes") or "").strip()
    ws_id = int(data.get("workspace_id") or 1)
    paid_at = (data.get("paid_at") or datetime.now().strftime("%Y-%m-%d")).strip()
    now = _now()
    receipt_num = f"REC-{datetime.now().strftime('%Y%m%d')}-{secrets.randbelow(9000) + 1000}"

    cid = data.get("contact_id")
    with db.get_conn() as conn:
        if not cid and student_email:
            r = conn.execute("SELECT id FROM contacts WHERE email = ?", (student_email,)).fetchone()
            if r:
                cid = int(r["id"])
        if not cid and student_phone:
            cid = wa_client.find_contact_id_by_phone(conn, student_phone)

        cur = conn.execute(
            """
            INSERT INTO student_payments (
                receipt_num, contact_id, student_name, student_phone, student_email,
                course_name, total_fee, amount_paid, payment_method, payment_ref,
                notes, workspace_id, paid_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_num, cid, student_name, student_phone, student_email,
                course_name, total_fee, amount_paid, method, ref,
                notes, ws_id, paid_at, now,
            ),
        )
        pid = cur.lastrowid

        if cid:
            conn.execute(
                """
                UPDATE contacts SET
                    status = 'client',
                    deal_value = MAX(COALESCE(deal_value, 0), ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (max(total_fee, amount_paid), now, cid),
            )
            conn.execute(
                "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'note', ?, ?)",
                (cid, f"💰 Paiement reçu : {_fmt_money(amount_paid)} ({method}) — {course_name} · Reçu {receipt_num}", now),
            )

    return {"ok": True, "id": pid, "receipt_num": receipt_num}


def list_payments(search: str = "", workspace_id: int = 1) -> Dict[str, Any]:
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM student_payments
                WHERE COALESCE(workspace_id, 1) = ?
                ORDER BY paid_at DESC, id DESC
                LIMIT 300
                """,
                (workspace_id,),
            ).fetchall()
        ]
        # Compute cumulative paid per (student_name, course_name) to show accurate remaining balance
        cum_map: Dict[str, float] = {}
        fee_map: Dict[str, float] = {}
        for r in rows:
            key = f"{r['student_name'].lower()}::{r['course_name'].lower()}"
            cum_map[key] = cum_map.get(key, 0.0) + float(r["amount_paid"] or 0)
            fee_map[key] = max(fee_map.get(key, 0.0), float(r["total_fee"] or 0))

        q = search.strip().lower()
        items = []
        total_collected = 0.0
        by_method: Dict[str, float] = {}
        for r in rows:
            key = f"{r['student_name'].lower()}::{r['course_name'].lower()}"
            r["cumulative_paid"] = cum_map[key]
            r["effective_fee"] = fee_map[key]
            r["remaining_balance"] = max(0.0, fee_map[key] - cum_map[key])
            total_collected += float(r["amount_paid"] or 0)
            m = r["payment_method"] or "Autre"
            by_method[m] = by_method.get(m, 0.0) + float(r["amount_paid"] or 0)
            if q:
                hay = f"{r['receipt_num']} {r['student_name']} {r['student_phone']} {r['course_name']} {r['payment_method']} {r['payment_ref']}".lower()
                if q not in hay:
                    continue
            items.append(r)

        total_remaining = sum(max(0.0, fee_map[k] - cum_map[k]) for k in fee_map)
        return {
            "items": items,
            "summary": {
                "count": len(rows),
                "total_collected": total_collected,
                "total_remaining": total_remaining,
                "by_method": by_method,
            },
        }


def get_receipt_data(pid: int) -> Optional[Dict[str, Any]]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM student_payments WHERE id = ?", (pid,)).fetchone()
        if not row:
            return None
        p = dict(row)
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (p.get("workspace_id") or 1,)).fetchone()
        ws_dict = dict(ws) if ws else {"name": "MSF'S English Learning Academy", "tagline": "Centre de Formation", "currency": "FCFA", "primary_color": "#2563eb"}

        # Cumulative paid for same student + course
        cum = conn.execute(
            """
            SELECT SUM(amount_paid) AS s, MAX(total_fee) AS f
            FROM student_payments
            WHERE LOWER(student_name) = LOWER(?) AND LOWER(course_name) = LOWER(?)
            """,
            (p["student_name"], p["course_name"]),
        ).fetchone()
        cum_paid = float(cum["s"] or p["amount_paid"] or 0)
        eff_fee = float(cum["f"] or p["total_fee"] or 0)
        remaining = max(0.0, eff_fee - cum_paid)
        return {
            "payment": p,
            "workspace": ws_dict,
            "cumulative_paid": cum_paid,
            "effective_fee": eff_fee,
            "remaining_balance": remaining,
        }


def delete_payment(pid: int) -> Dict[str, Any]:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM student_payments WHERE id = ?", (pid,))
    return {"ok": True}


def send_receipt_via_whatsapp(pid: int, receipt_url: str) -> Dict[str, Any]:
    data = get_receipt_data(pid)
    if not data:
        return {"ok": False, "error": "Reçu introuvable."}
    p = data["payment"]
    ws = data["workspace"]
    currency = ws.get("currency") or "FCFA"
    phone = p.get("student_phone") or ""
    if not phone:
        return {"ok": False, "error": "Aucun numéro de téléphone enregistré pour cet apprenant."}

    rem_line = (
        f"• Reste à payer : {_fmt_money(data['remaining_balance'], currency)}"
        if data["effective_fee"] > 0
        else "• Statut : Soldé ✅"
    )
    msg = (
        f"🧾 *REÇU DE PAIEMENT — {ws.get('name', 'ContactsHub')}*\n"
        f"N° Reçu : *{p['receipt_num']}*\n"
        f"Apprenant : {p['student_name']}\n"
        f"Formation : {p['course_name']}\n"
        f"• Montant payé : *{_fmt_money(p['amount_paid'], currency)}*\n"
        f"• Mode : {p['payment_method']}" + (f" (Réf: {p['payment_ref']})" if p.get("payment_ref") else "") + "\n"
        f"• Date : {p['paid_at']}\n"
        f"{rem_line}\n\n"
        f"Merci pour votre confiance ! 🙏"
    )
    res = wa_client.bridge_post("/send", {"phone": phone, "text": msg}, timeout=15.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "WhatsApp non connecté. Scannez le QR Code d'abord."}
    return {"ok": True}
