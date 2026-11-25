import json
import random
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import db
import wa_client

_stop_flags: Dict[int, threading.Event] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def render_wa_template(tpl: str, contact: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> str:
    full_name = (contact.get("full_name") or "").strip()
    first_name = (contact.get("first_name") or (full_name.split(" ")[0] if full_name else "")).strip()
    last_name = (contact.get("last_name") or (full_name.split(" ", 1)[1] if " " in full_name else "")).strip()
    out = (tpl or "")
    replacements = {
        "{prenom}": first_name,
        "{prénom}": first_name,
        "{nom}": last_name,
        "{nom_complet}": full_name,
        "{entreprise}": contact.get("company") or "votre entreprise",
    }
    if extra:
        for k, v in extra.items():
            replacements[f"{{{k}}}"] = str(v or "")
    for k, v in replacements.items():
        out = out.replace(k, v).replace(k.upper(), v)
    return out.strip()


def get_auto_welcome_config() -> Dict[str, Any]:
    with db.get_conn() as conn:
        en = conn.execute("SELECT value FROM app_settings WHERE key = 'wa_auto_welcome_enabled'").fetchone()
        tpl = conn.execute("SELECT value FROM app_settings WHERE key = 'wa_auto_welcome_tpl'").fetchone()
    default_tpl = (
        "Bonjour {prenom} 👋\n"
        "Merci pour votre inscription / demande auprès de MSF'S English Learning Academy ({source}).\n"
        "Un conseiller vient de recevoir votre dossier et vous répond ici très rapidement !"
    )
    return {
        "enabled": (en["value"] == "1") if en else True,
        "template": tpl["value"] if tpl and tpl["value"].strip() else default_tpl,
    }


def save_auto_welcome_config(enabled: bool, template: str) -> Dict[str, Any]:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('wa_auto_welcome_enabled', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('wa_auto_welcome_tpl', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (template.strip(),),
        )
    return get_auto_welcome_config()


def trigger_auto_welcome_wa(
    contact_id: Optional[int],
    phone: str,
    full_name: str = "",
    source_label: str = "ContactsHub",
    extra_vars: Optional[Dict[str, str]] = None,
) -> None:
    cfg = get_auto_welcome_config()
    if not cfg.get("enabled"):
        return
    norm_phone = wa_client.format_plus_phone(phone)
    if not norm_phone:
        return

    extra = {"source": source_label}
    if extra_vars:
        extra.update(extra_vars)
    msg = render_wa_template(cfg["template"], {"full_name": full_name}, extra=extra)
    if not msg:
        return

    def _bg():
        try:
            if not wa_client.is_bridge_alive():
                return
            st = wa_client.bridge_get("/status", timeout=2.0)
            if st.get("state") != "connected":
                return
            wa_client.bridge_post("/send", {"phone": norm_phone, "text": msg}, timeout=12.0)
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True).start()


def query_audience(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    clauses = ["phone != ''"]
    params: List[Any] = []
    if filters.get("workspace_id"):
        clauses.append("COALESCE(workspace_id, 1) = ?")
        params.append(int(filters["workspace_id"]))
    if filters.get("status"):
        clauses.append("status = ?")
        params.append(filters["status"])
    if filters.get("tag"):
        clauses.append("LOWER(tags) LIKE ?")
        params.append(f"%{filters['tag'].strip().lower()}%")
    if filters.get("source_type"):
        clauses.append("source_type = ?")
        params.append(filters["source_type"])
    if filters.get("min_score"):
        clauses.append("lead_score >= ?")
        params.append(int(filters["min_score"]))
    clauses.append("status != 'unsubscribed'")

    sql = f"""
        SELECT id, full_name, first_name, last_name, email, phone, company, status, lead_score, tags
        FROM contacts
        WHERE {' AND '.join(clauses)}
        ORDER BY lead_score DESC, id DESC
        LIMIT 250
    """
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    # Deduplicate by normalized phone digits
    seen = set()
    unique = []
    for r in rows:
        d = wa_client.normalize_digits(r["phone"])
        if len(d) < 6 or d in seen:
            continue
        seen.add(d)
        unique.append(r)
    return unique


def create_and_start_broadcast(
    name: str,
    message_tpl: str,
    filters: Dict[str, Any],
    delay_min: int = 18,
    delay_max: int = 40,
) -> Dict[str, Any]:
    st = wa_client.bridge_get("/status", timeout=3.0)
    if st.get("state") != "connected":
        return {"ok": False, "error": "WhatsApp n'est pas connecté. Scannez d'abord le QR Code dans l'onglet WhatsApp Interne."}

    audience = query_audience(filters)
    if not audience:
        return {"ok": False, "error": "Aucun contact avec numéro de téléphone ne correspond à ce filtre."}

    d_min = max(8, min(int(delay_min or 18), 180))
    d_max = max(d_min, min(int(delay_max or 40), 300))
    now = _now()

    with db.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO wa_broadcasts (name, message_tpl, filter_json, delay_min_sec, delay_max_sec, status, total_count, created_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (name.strip() or f"Campagne WA {now[:10]}", message_tpl.strip(), json.dumps(filters), d_min, d_max, len(audience), now),
        )
        bid = cur.lastrowid
        for c in audience:
            conn.execute(
                """
                INSERT INTO wa_broadcast_recipients (broadcast_id, contact_id, phone, full_name, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (bid, c["id"], wa_client.format_plus_phone(c["phone"]), c.get("full_name") or c.get("email") or c["phone"]),
            )

    stop_ev = threading.Event()
    _stop_flags[bid] = stop_ev
    threading.Thread(target=_run_broadcast_worker, args=(bid, message_tpl, audience, d_min, d_max, stop_ev), daemon=True).start()
    return {"ok": True, "broadcast_id": bid, "total_count": len(audience)}


def _run_broadcast_worker(
    bid: int,
    message_tpl: str,
    audience: List[Dict[str, Any]],
    d_min: int,
    d_max: int,
    stop_ev: threading.Event,
) -> None:
    sent = 0
    failed = 0
    for idx, c in enumerate(audience):
        if stop_ev.is_set():
            with db.get_conn() as conn:
                conn.execute("UPDATE wa_broadcasts SET status = 'stopped', completed_at = ? WHERE id = ?", (_now(), bid))
            return

        phone = wa_client.format_plus_phone(c["phone"])
        text = render_wa_template(message_tpl, c)
        res = wa_client.bridge_post("/send", {"phone": phone, "text": text}, timeout=15.0)
        now = _now()
        with db.get_conn() as conn:
            if res.get("ok"):
                sent += 1
                conn.execute(
                    "UPDATE wa_broadcast_recipients SET status = 'sent', sent_at = ? WHERE broadcast_id = ? AND contact_id = ?",
                    (now, bid, c["id"]),
                )
            else:
                failed += 1
                conn.execute(
                    "UPDATE wa_broadcast_recipients SET status = 'failed', error = ?, sent_at = ? WHERE broadcast_id = ? AND contact_id = ?",
                    (str(res.get("error") or "Échec")[:200], now, bid, c["id"]),
                )
            conn.execute(
                "UPDATE wa_broadcasts SET sent_count = ?, failed_count = ? WHERE id = ?",
                (sent, failed, bid),
            )

        if idx < len(audience) - 1:
            wait_s = random.uniform(d_min, d_max)
            if stop_ev.wait(wait_s):
                with db.get_conn() as conn:
                    conn.execute("UPDATE wa_broadcasts SET status = 'stopped', completed_at = ? WHERE id = ?", (_now(), bid))
                return

    with db.get_conn() as conn:
        conn.execute("UPDATE wa_broadcasts SET status = 'completed', completed_at = ? WHERE id = ?", (_now(), bid))
    _stop_flags.pop(bid, None)


def stop_broadcast(bid: int) -> Dict[str, Any]:
    ev = _stop_flags.get(bid)
    if ev:
        ev.set()
    with db.get_conn() as conn:
        conn.execute("UPDATE wa_broadcasts SET status = 'stopped', completed_at = ? WHERE id = ? AND status = 'running'", (_now(), bid))
    return {"ok": True}


def list_broadcasts() -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM wa_broadcasts ORDER BY id DESC LIMIT 30").fetchall()
        return [dict(r) for r in rows]
