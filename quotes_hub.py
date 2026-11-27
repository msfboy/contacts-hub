import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import db
import payments_hub
import wa_client


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fmt_money(amount: float, currency: str = "FCFA") -> str:
    return f"{int(round(float(amount or 0))):,}".replace(",", " ") + f" {currency}"


def _parse_items(raw_items: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except Exception:
            raw_items = []
    if not isinstance(raw_items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or it.get("description") or "").strip()
        if not label:
            continue
        try:
            qty = max(1.0, float(it.get("qty") or 1))
        except Exception:
            qty = 1.0
        try:
            unit_price = max(0.0, float(it.get("unit_price") or 0))
        except Exception:
            unit_price = 0.0
        line_total = round(qty * unit_price, 2)
        out.append({
            "label": label[:180],
            "qty": qty,
            "unit_price": unit_price,
            "line_total": line_total,
        })
    return out


def create_quote(data: Dict[str, Any]) -> Dict[str, Any]:
    client_name = (data.get("client_name") or "").strip()
    title = (data.get("title") or "").strip()
    items = _parse_items(data.get("items"))
    if not client_name or not title:
        return {"ok": False, "error": "Nom du client et objet du devis requis."}
    if not items:
        try:
            fallback_amt = float(data.get("unit_price") or data.get("total_amount") or 0)
        except Exception:
            fallback_amt = 0.0
        if fallback_amt <= 0:
            return {"ok": False, "error": "Ajoutez au moins une ligne chiffrée au devis."}
        items = [{"label": title, "qty": 1.0, "unit_price": fallback_amt, "line_total": fallback_amt}]

    doc_type = (data.get("doc_type") or "devis").strip().lower()
    if doc_type not in ("devis", "proforma"):
        doc_type = "devis"

    ws_id = int(data.get("workspace_id") or 1)
    ws = db.get_workspace(ws_id)
    currency = (data.get("currency") or ws.get("currency") or "FCFA").strip()

    subtotal = round(sum(i["line_total"] for i in items), 2)
    try:
        discount = max(0.0, float(data.get("discount") or 0))
    except Exception:
        discount = 0.0
    try:
        tax_rate = max(0.0, min(50.0, float(data.get("tax_rate") or 0)))
    except Exception:
        tax_rate = 0.0

    taxable = max(0.0, subtotal - discount)
    tax_amount = round(taxable * (tax_rate / 100.0), 2)
    total_amount = round(taxable + tax_amount, 2)

    valid_until = (data.get("valid_until") or (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")).strip()
    client_company = (data.get("client_company") or "").strip()
    client_phone = wa_client.format_plus_phone(data.get("client_phone") or "") if data.get("client_phone") else ""
    client_email = (data.get("client_email") or "").strip().lower()
    notes = (data.get("notes") or "").strip()
    prefix = "PRO" if doc_type == "proforma" else "DEV"
    quote_num = f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    now = _now()

    cid = data.get("contact_id")
    with db.get_conn() as conn:
        if not cid and client_email:
            r = conn.execute("SELECT id FROM contacts WHERE email = ?", (client_email,)).fetchone()
            if r:
                cid = int(r["id"])
        if not cid and client_phone:
            cid = wa_client.find_contact_id_by_phone(conn, client_phone)

        cur = conn.execute(
            """
            INSERT INTO quotes (
                quote_num, doc_type, contact_id, client_name, client_company,
                client_phone, client_email, title, items_json, subtotal,
                discount, tax_rate, total_amount, currency, valid_until,
                status, notes, payment_id, workspace_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, NULL, ?, ?)
            """,
            (
                quote_num, doc_type, cid, client_name, client_company,
                client_phone, client_email, title, json.dumps(items, ensure_ascii=False),
                subtotal, discount, tax_rate, total_amount, currency,
                valid_until, notes, ws_id, now,
            ),
        )
        qid = cur.lastrowid
        if cid:
            conn.execute(
                "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'note', ?, ?)",
                (cid, f"📄 {doc_type.upper()} {quote_num} créé : {title} ({_fmt_money(total_amount, currency)})", now),
            )
    return {"ok": True, "id": qid, "quote_num": quote_num, "total_amount": total_amount}


def list_quotes(workspace_id: int = 1, search: str = "") -> Dict[str, Any]:
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM quotes
                WHERE COALESCE(workspace_id, 1) = ?
                ORDER BY id DESC
                LIMIT 200
                """,
                (workspace_id,),
            ).fetchall()
        ]
    q = search.strip().lower()
    items = []
    pending_total = 0.0
    paid_total = 0.0
    for r in rows:
        r["items"] = _parse_items(r.get("items_json"))
        amt = float(r.get("total_amount") or 0)
        if r.get("status") == "paid":
            paid_total += amt
        elif r.get("status") not in ("cancelled",):
            pending_total += amt
        if q:
            hay = f"{r['quote_num']} {r['client_name']} {r['client_company']} {r['title']} {r['status']}".lower()
            if q not in hay:
                continue
        items.append(r)
    return {
        "items": items,
        "summary": {
            "count": len(rows),
            "pending_total": pending_total,
            "paid_total": paid_total,
        },
    }


def get_quote_data(qid: int) -> Optional[Dict[str, Any]]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE id = ?", (qid,)).fetchone()
        if not row:
            return None
        q = dict(row)
    ws_id = int(q.get("workspace_id") or 1)
    ws = db.get_workspace(ws_id)
    q["items"] = _parse_items(q.get("items_json"))
    taxable = max(0.0, float(q.get("subtotal") or 0) - float(q.get("discount") or 0))
    q["tax_amount"] = round(taxable * (float(q.get("tax_rate") or 0) / 100.0), 2)
    pay_link = payments_hub.build_payment_link(
        amount=float(q.get("total_amount") or 0),
        ref=q["quote_num"],
        customer_name=q["client_name"],
        workspace_id=ws_id,
    )
    return {
        "quote": q,
        "workspace": ws,
        "online_payment_url": pay_link,
    }


def convert_quote_to_payment(qid: int, payment_method: str = "Virement", payment_ref: str = "") -> Dict[str, Any]:
    data = get_quote_data(qid)
    if not data:
        return {"ok": False, "error": "Devis introuvable."}
    q = data["quote"]
    if q.get("status") == "paid" and q.get("payment_id"):
        return {"ok": False, "error": "Ce devis a déjà été converti en paiement."}

    res = payments_hub.create_payment({
        "contact_id": q.get("contact_id"),
        "student_name": q["client_name"] + (f" ({q['client_company']})" if q.get("client_company") else ""),
        "student_phone": q.get("client_phone") or "",
        "student_email": q.get("client_email") or "",
        "course_name": q["title"],
        "total_fee": float(q.get("total_amount") or 0),
        "amount_paid": float(q.get("total_amount") or 0),
        "payment_method": payment_method or "Virement",
        "payment_ref": payment_ref or q["quote_num"],
        "notes": f"Conversion depuis {q['doc_type'].upper()} {q['quote_num']}",
        "workspace_id": int(q.get("workspace_id") or 1),
    })
    if not res.get("ok"):
        return res

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE quotes SET status = 'paid', payment_id = ? WHERE id = ?",
            (int(res["id"]), qid),
        )
    return {
        "ok": True,
        "quote_id": qid,
        "payment_id": res["id"],
        "receipt_num": res["receipt_num"],
    }


def send_quote_via_whatsapp(qid: int, quote_url: str = "") -> Dict[str, Any]:
    data = get_quote_data(qid)
    if not data:
        return {"ok": False, "error": "Devis introuvable."}
    q = data["quote"]
    ws = data["workspace"]
    phone = q.get("client_phone") or ""
    if not phone:
        return {"ok": False, "error": "Aucun numéro WhatsApp renseigné sur ce devis."}
    doc_label = "FACTURE PROFORMA" if q.get("doc_type") == "proforma" else "DEVIS COMMERCIAL"
    lines = [
        f"📄 *{doc_label} — {ws.get('name', 'ContactsHub')}*",
        f"Réf : *{q['quote_num']}*",
        f"Client : {q['client_name']}" + (f" ({q['client_company']})" if q.get("client_company") else ""),
        f"Objet : {q['title']}",
        f"• Montant TTC : *{_fmt_money(q['total_amount'], q.get('currency') or 'FCFA')}*",
        f"• Valable jusqu'au : {q.get('valid_until') or '—'}",
    ]
    if quote_url:
        lines.append(f"\n🔗 Consulter / Télécharger en PDF :\n{quote_url}")
    if data.get("online_payment_url"):
        lines.append(f"\n💳 Régler en ligne directement :\n{data['online_payment_url']}")
    lines.append("\nRestons à votre disposition pour toute question ! 🙏")
    res = wa_client.bridge_post("/send", {"phone": phone, "text": "\n".join(lines)}, timeout=15.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "WhatsApp non connecté."}
    return {"ok": True}


def delete_quote(qid: int) -> Dict[str, Any]:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM quotes WHERE id = ?", (qid,))
    return {"ok": True}
