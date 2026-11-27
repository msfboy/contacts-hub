import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

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
    receipt_num = f"REC-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"

    cid = data.get("contact_id")
    with db.get_conn() as conn:
        if ref:
            dup = conn.execute(
                """
                SELECT id, receipt_num FROM student_payments
                WHERE COALESCE(workspace_id, 1) = ? AND LOWER(payment_method) = LOWER(?) AND payment_ref = ?
                LIMIT 1
                """,
                (ws_id, method, ref),
            ).fetchone()
            if dup:
                return {"ok": False, "error": f"Référence de paiement déjà enregistrée (reçu {dup['receipt_num']}).", "duplicate": True, "receipt_num": dup["receipt_num"]}

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

    try:
        import workflows_engine
        workflows_engine.fire_event("payment_received", {
            "contact_id": cid,
            "full_name": student_name,
            "email": student_email,
            "phone": student_phone,
            "course_name": course_name,
        })
    except Exception:
        pass

    return {"ok": True, "id": pid, "receipt_num": receipt_num}


def _student_course_key(r: Dict[str, Any]) -> str:
    ident = f"cid:{r['contact_id']}" if r.get("contact_id") else f"name:{(r.get('student_name') or '').strip().lower()}"
    return f"{ident}::{(r.get('course_name') or '').strip().lower()}"


def list_payments(search: str = "", workspace_id: int = 1) -> Dict[str, Any]:
    with db.get_conn() as conn:
        # Full SQL aggregation across ALL workspace payments (no LIMIT 300 truncation)
        agg_rows = conn.execute(
            """
            SELECT
                CASE WHEN contact_id IS NOT NULL THEN 'cid:' || contact_id
                     ELSE 'name:' || LOWER(TRIM(student_name)) END AS skey,
                LOWER(TRIM(course_name)) AS ckey,
                SUM(amount_paid) AS cum_paid,
                MAX(total_fee) AS eff_fee
            FROM student_payments
            WHERE COALESCE(workspace_id, 1) = ?
            GROUP BY skey, ckey
            """,
            (workspace_id,),
        ).fetchall()
        cum_map: Dict[str, float] = {}
        fee_map: Dict[str, float] = {}
        for a in agg_rows:
            k = f"{a['skey']}::{a['ckey']}"
            cum_map[k] = float(a["cum_paid"] or 0.0)
            fee_map[k] = float(a["eff_fee"] or 0.0)

        method_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(payment_method, ''), 'Autre') AS m, SUM(amount_paid) AS s, COUNT(*) AS cnt
            FROM student_payments
            WHERE COALESCE(workspace_id, 1) = ?
            GROUP BY m
            """,
            (workspace_id,),
        ).fetchall()
        by_method: Dict[str, float] = {}
        total_collected = 0.0
        total_count = 0
        for mr in method_rows:
            amt = float(mr["s"] or 0.0)
            by_method[mr["m"]] = amt
            total_collected += amt
            total_count += int(mr["cnt"] or 0)

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

        q = search.strip().lower()
        items = []
        for r in rows:
            key = _student_course_key(r)
            r["cumulative_paid"] = cum_map.get(key, float(r["amount_paid"] or 0))
            r["effective_fee"] = fee_map.get(key, float(r["total_fee"] or 0))
            r["remaining_balance"] = max(0.0, r["effective_fee"] - r["cumulative_paid"])
            if q:
                hay = f"{r['receipt_num']} {r['student_name']} {r['student_phone']} {r['course_name']} {r['payment_method']} {r['payment_ref']}".lower()
                if q not in hay:
                    continue
            items.append(r)

        total_remaining = sum(max(0.0, fee_map[k] - cum_map[k]) for k in fee_map)
        return {
            "items": items,
            "summary": {
                "count": total_count,
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
        ws_id = p.get("workspace_id") or 1
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        ws_dict = dict(ws) if ws else {"name": "MSF'S English Learning Academy", "tagline": "Centre de Formation", "currency": "FCFA", "primary_color": "#2563eb"}

        # Cumulative paid scoped to workspace + contact_id (if known) + course
        if p.get("contact_id"):
            cum = conn.execute(
                """
                SELECT SUM(amount_paid) AS s, MAX(total_fee) AS f
                FROM student_payments
                WHERE COALESCE(workspace_id, 1) = ? AND contact_id = ? AND LOWER(TRIM(course_name)) = LOWER(TRIM(?))
                """,
                (ws_id, p["contact_id"], p["course_name"]),
            ).fetchone()
        else:
            cum = conn.execute(
                """
                SELECT SUM(amount_paid) AS s, MAX(total_fee) AS f
                FROM student_payments
                WHERE COALESCE(workspace_id, 1) = ? AND contact_id IS NULL
                  AND LOWER(TRIM(student_name)) = LOWER(TRIM(?)) AND LOWER(TRIM(course_name)) = LOWER(TRIM(?))
                """,
                (ws_id, p["student_name"], p["course_name"]),
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


def list_unpaid_balances(workspace_id: int = 1) -> List[Dict[str, Any]]:
    data = list_payments(search="", workspace_id=workspace_id)
    seen = set()
    unpaid: List[Dict[str, Any]] = []
    for r in data.get("items") or []:
        rem = float(r.get("remaining_balance") or 0)
        if rem <= 0:
            continue
        key = _student_course_key(r)
        if key in seen:
            continue
        seen.add(key)
        unpaid.append({
            "payment_id": r["id"],
            "student_name": r["student_name"],
            "student_phone": r.get("student_phone") or "",
            "student_email": r.get("student_email") or "",
            "course_name": r["course_name"],
            "effective_fee": float(r.get("effective_fee") or 0),
            "cumulative_paid": float(r.get("cumulative_paid") or 0),
            "remaining_balance": rem,
            "last_paid_at": r.get("paid_at") or "",
        })
    unpaid.sort(key=lambda x: x["remaining_balance"], reverse=True)
    return unpaid


def send_unpaid_reminder_wa(
    student_name: str,
    phone: str,
    course_name: str,
    remaining_balance: float,
    workspace_id: int = 1,
) -> Dict[str, Any]:
    if not phone:
        return {"ok": False, "error": "Aucun numéro WhatsApp pour cet apprenant."}
    ws = db.get_workspace(workspace_id)
    currency = ws.get("currency") or "FCFA"
    first = (student_name or "").strip().split(" ")[0] or "Cher apprenant"
    pay_url = build_payment_link(
        amount=remaining_balance,
        ref=f"SOLDE-{datetime.now().strftime('%m%d')}",
        customer_name=student_name,
        workspace_id=workspace_id,
    )
    pay_line = f"\n💳 Régler en ligne en 1 clic : {pay_url}\n" if pay_url else ""
    msg = (
        f"Bonjour {first} 👋\n"
        f"Petit rappel amical de *{ws['name']}* concernant votre formation *{course_name}*.\n\n"
        f"• Solde restant à régler : *{_fmt_money(remaining_balance, currency)}*\n"
        f"• Moyens acceptés : TMoney, Flooz, Espèces ou Virement.{pay_line}\n"
        f"Dès réception de votre règlement, nous vous transmettrons immédiatement votre reçu officiel signé ici sur WhatsApp. Merci ! 🙏"
    )
    res = wa_client.bridge_post("/send", {"phone": phone, "text": msg}, timeout=15.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "WhatsApp non connecté."}
    return {"ok": True}


def export_cashbook_xlsx(workspace_id: int = 1, month: str = "") -> bytes:
    import io
    import openpyxl

    ws_info = db.get_workspace(workspace_id)
    currency = ws_info.get("currency") or "FCFA"
    with db.get_conn() as conn:
        if month and len(month) == 7:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM student_payments WHERE COALESCE(workspace_id, 1) = ? AND paid_at LIKE ? ORDER BY paid_at ASC, id ASC",
                    (workspace_id, f"{month}%"),
                ).fetchall()
            ]
        else:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM student_payments WHERE COALESCE(workspace_id, 1) = ? ORDER BY paid_at ASC, id ASC",
                    (workspace_id,),
                ).fetchall()
            ]

    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Journal de Caisse"
    sheet.append([
        "Date", "N° Reçu", "Apprenant", "Téléphone", "Formation",
        "Mode de paiement", "Référence", f"Montant versé ({currency})", f"Frais total ({currency})", "Notes",
    ])
    by_method: Dict[str, float] = {}
    total_amt = 0.0
    for r in rows:
        amt = float(r.get("amount_paid") or 0)
        total_amt += amt
        m = r.get("payment_method") or "Autre"
        by_method[m] = by_method.get(m, 0.0) + amt
        sheet.append([
            r.get("paid_at") or "",
            r.get("receipt_num") or "",
            r.get("student_name") or "",
            r.get("student_phone") or "",
            r.get("course_name") or "",
            m,
            r.get("payment_ref") or "",
            amt,
            float(r.get("total_fee") or 0),
            r.get("notes") or "",
        ])
    sheet.append([])
    sheet.append(["TOTAL ENCAISSÉ", "", "", "", "", "", "", total_amt, "", ""])
    for m, val in by_method.items():
        sheet.append([f"Sous-total {m}", "", "", "", "", m, "", val, "", ""])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# #5 Online Payment Gateway (FedaPay / CinetPay / PayGate / Stripe / Custom)
# ---------------------------------------------------------------------------

VALID_GATEWAY_PROVIDERS = {"fedapay", "cinetpay", "paygate", "stripe", "custom_link"}


def _gw_key(workspace_id: int) -> str:
    return f"payment_gateway_ws_{int(workspace_id or 1)}"


def get_gateway_config(workspace_id: int = 1, include_secrets: bool = False) -> Dict[str, Any]:
    raw = db.get_app_setting(_gw_key(workspace_id), "")
    cfg: Dict[str, Any] = {}
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception:
            cfg = {}
    secret_plain = db.decrypt_secret(cfg.get("secret_key_enc") or "") if cfg.get("secret_key_enc") else ""
    webhook_plain = db.decrypt_secret(cfg.get("webhook_secret_enc") or "") if cfg.get("webhook_secret_enc") else ""
    out = {
        "enabled": bool(cfg.get("enabled", False)),
        "provider": cfg.get("provider") or "fedapay",
        "public_key": cfg.get("public_key") or "",
        "checkout_url_template": cfg.get("checkout_url_template") or "",
        "instructions": cfg.get("instructions") or "",
        "secret_configured": bool(secret_plain),
        "webhook_secret_configured": bool(webhook_plain),
    }
    if include_secrets:
        out["secret_key"] = secret_plain
        out["webhook_secret"] = webhook_plain
    return out


def save_gateway_config(workspace_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    prev = get_gateway_config(workspace_id, include_secrets=True)
    provider = str(data.get("provider") or prev.get("provider") or "fedapay").strip().lower()
    if provider not in VALID_GATEWAY_PROVIDERS:
        provider = "custom_link"
    secret_key = str(data.get("secret_key") or "").strip() or prev.get("secret_key", "")
    webhook_secret = str(data.get("webhook_secret") or "").strip() or prev.get("webhook_secret", "")
    if not webhook_secret:
        webhook_secret = secrets.token_hex(16)
    payload = {
        "enabled": bool(data.get("enabled", prev.get("enabled", False))),
        "provider": provider,
        "public_key": str(data.get("public_key") if data.get("public_key") is not None else prev.get("public_key", "")).strip(),
        "checkout_url_template": str(
            data.get("checkout_url_template")
            if data.get("checkout_url_template") is not None
            else prev.get("checkout_url_template", "")
        ).strip(),
        "instructions": str(data.get("instructions") if data.get("instructions") is not None else prev.get("instructions", "")).strip(),
        "secret_key_enc": db.encrypt_secret(secret_key) if secret_key else "",
        "webhook_secret_enc": db.encrypt_secret(webhook_secret) if webhook_secret else "",
    }
    db.set_app_setting(_gw_key(workspace_id), json.dumps(payload, ensure_ascii=False))
    res = get_gateway_config(workspace_id, include_secrets=False)
    res["webhook_secret_preview"] = webhook_secret[:6] + "…" + webhook_secret[-4:] if len(webhook_secret) >= 10 else ""
    return res


def build_payment_link(
    amount: float,
    ref: str = "",
    customer_name: str = "",
    workspace_id: int = 1,
) -> str:
    cfg = get_gateway_config(workspace_id, include_secrets=False)
    if not cfg.get("enabled"):
        return ""
    tpl = (cfg.get("checkout_url_template") or "").strip()
    if not tpl or not (tpl.startswith("https://") or tpl.startswith("http://")):
        return ""
    amt_str = str(int(round(float(amount or 0))))
    url = (
        tpl.replace("{amount}", quote_plus(amt_str))
        .replace("{ref}", quote_plus(str(ref or "")))
        .replace("{name}", quote_plus(str(customer_name or "")))
    )
    return url


def process_gateway_webhook(
    provider: str,
    payload: Dict[str, Any],
    raw_body: bytes,
    signature: str = "",
    workspace_id: int = 1,
) -> Dict[str, Any]:
    cfg = get_gateway_config(workspace_id, include_secrets=True)
    expected_secret = (cfg.get("webhook_secret") or "").strip()
    if not expected_secret:
        return {"ok": False, "error": "Passerelle non configurée (secret webhook manquant).", "status": 403}

    sig = (signature or "").strip()
    expected_hmac = hmac.new(expected_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    valid = (
        hmac.compare_digest(sig, expected_secret)
        or hmac.compare_digest(sig.lower(), expected_hmac.lower())
        or hmac.compare_digest(sig.lower(), f"sha256={expected_hmac.lower()}")
    )
    if not valid:
        return {"ok": False, "error": "Signature webhook invalide.", "status": 401}

    # Normalize common fields across FedaPay / CinetPay / PayGate / Stripe / Custom
    entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else payload
    status_val = str(entity.get("status") or payload.get("status") or "approved").strip().lower()
    if status_val not in ("approved", "paid", "succeeded", "completed", "accepted", "success", "00"):
        return {"ok": True, "ignored": True, "reason": f"Statut non payé ({status_val})"}

    customer = entity.get("customer") if isinstance(entity.get("customer"), dict) else {}
    student_name = (
        entity.get("student_name")
        or entity.get("customer_name")
        or f"{customer.get('firstname', '')} {customer.get('lastname', '')}".strip()
        or "Client En Ligne"
    )
    student_phone = entity.get("student_phone") or entity.get("phone") or customer.get("phone_number") or ""
    if isinstance(student_phone, dict):
        student_phone = student_phone.get("number") or ""
    student_email = entity.get("student_email") or entity.get("email") or customer.get("email") or ""
    course_name = entity.get("course_name") or entity.get("description") or entity.get("title") or "Paiement en ligne"
    try:
        amount = float(entity.get("amount") or entity.get("amount_paid") or 0)
    except Exception:
        amount = 0.0
    tx_ref = str(
        entity.get("reference")
        or entity.get("transaction_id")
        or entity.get("tx_reference")
        or entity.get("id")
        or f"{provider.upper()}-{secrets.token_hex(4).upper()}"
    ).strip()

    prov_label = {
        "fedapay": "FedaPay (En ligne)",
        "cinetpay": "CinetPay (En ligne)",
        "paygate": "PayGate (Mobile Money)",
        "stripe": "Stripe (Carte)",
        "custom_link": "Paiement en ligne",
    }.get(provider.lower(), f"{provider} (En ligne)")

    res = create_payment({
        "student_name": str(student_name),
        "student_phone": str(student_phone),
        "student_email": str(student_email),
        "course_name": str(course_name),
        "total_fee": float(entity.get("total_fee") or amount),
        "amount_paid": amount,
        "payment_method": prov_label,
        "payment_ref": tx_ref,
        "notes": f"Encaissement automatique via {prov_label}",
        "workspace_id": workspace_id,
    })
    return res

