import email as email_lib
import html
import imaplib
import logging
import random
import re
import smtplib
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from typing import Any, Dict, List, Optional, Tuple

import dns.resolver

import db
import services

logger = logging.getLogger(__name__)
_smtp_lock = threading.Lock()
_seq_process_lock = threading.Lock()


def _clean_header(val: str) -> str:
    return re.sub(r"[\r\n]+", " ", str(val or "")).strip()


def _strip_html(raw: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.IGNORECASE)
    txt = re.sub(r"</p\s*>", "\n\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html.unescape(txt).strip()


def _looks_like_html(raw: str) -> bool:
    return bool(re.search(r"<(b|strong|i|em|u|p|br|div|a|ul|ol|li|h[1-6]|span)\b", raw or "", flags=re.IGNORECASE))


# ---------------- DELIVERABILITY CHECKER (SPF / DMARC / MX) ----------------


def check_deliverability(domain: str) -> Dict[str, Any]:
    dom = (domain or "").strip().lower()
    if "@" in dom:
        dom = dom.split("@", 1)[1]
    if not dom or "." not in dom:
        raise ValueError("Domaine invalide.")

    resolver = dns.resolver.Resolver()
    resolver.timeout = 3.0
    resolver.lifetime = 5.0

    result: Dict[str, Any] = {
        "domain": dom,
        "mx": {"ok": False, "records": []},
        "spf": {"ok": False, "record": ""},
        "dmarc": {"ok": False, "record": ""},
        "score": 0,
        "advice": [],
    }

    # MX
    try:
        ans = resolver.resolve(dom, "MX")
        result["mx"]["records"] = [str(r.exchange).rstrip(".") for r in ans][:5]
        result["mx"]["ok"] = len(result["mx"]["records"]) > 0
    except Exception:
        pass

    # SPF (TXT on root)
    try:
        ans = resolver.resolve(dom, "TXT")
        for r in ans:
            txt = "".join(s.decode("utf-8", errors="ignore") if isinstance(s, bytes) else str(s) for s in r.strings)
            if txt.lower().startswith("v=spf1"):
                result["spf"]["ok"] = True
                result["spf"]["record"] = txt
                break
    except Exception:
        pass

    # DMARC (TXT on _dmarc.domain)
    try:
        ans = resolver.resolve(f"_dmarc.{dom}", "TXT")
        for r in ans:
            txt = "".join(s.decode("utf-8", errors="ignore") if isinstance(s, bytes) else str(s) for s in r.strings)
            if txt.lower().startswith("v=dmarc1"):
                result["dmarc"]["ok"] = True
                result["dmarc"]["record"] = txt
                break
    except Exception:
        pass

    score = 0
    if result["mx"]["ok"]:
        score += 35
    else:
        result["advice"].append("Aucun enregistrement MX détecté sur ce domaine.")

    if result["spf"]["ok"]:
        score += 35
    else:
        result["advice"].append("Ajoutez un enregistrement TXT SPF (ex: v=spf1 include:_spf.google.com ~all) pour éviter les spams.")

    if result["dmarc"]["ok"]:
        score += 30
    else:
        result["advice"].append("Ajoutez un enregistrement TXT _dmarc (ex: v=DMARC1; p=quarantine;) pour protéger votre réputation.")

    if score == 100:
        result["advice"].append("Excellente configuration DNS ! Votre domaine est prêt pour l'envoi d'emails.")

    result["score"] = score
    return result


# ---------------- MULTI-SMTP ROTATION ----------------

_smtp_rr_index = 0


def _pick_smtp_account() -> Optional[Dict[str, Any]]:
    global _smtp_rr_index
    today = datetime.now().strftime("%Y-%m-%d")

    with db.get_conn() as conn:
        # Reset daily counters if date changed
        conn.execute(
            "UPDATE smtp_accounts SET sent_today = 0, last_sent_date = ? WHERE last_sent_date != ?",
            (today, today),
        )
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM smtp_accounts WHERE enabled = 1 AND sent_today < daily_limit ORDER BY id ASC"
            ).fetchall()
        ]
        if not rows:
            # Fallback to single legacy smtp_config if configured
            legacy = conn.execute("SELECT * FROM smtp_config WHERE id = 1").fetchone()
            if legacy and legacy["host"] and legacy["username"] and legacy["password"]:
                d = dict(legacy)
                d["id"] = 0
                return d
            return None

    with _smtp_lock:
        chosen = rows[_smtp_rr_index % len(rows)]
        _smtp_rr_index += 1
    return chosen


def _increment_smtp_usage(account_id: int) -> None:
    if not account_id:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE smtp_accounts SET sent_today = sent_today + 1, last_sent_date = ? WHERE id = ?",
            (today, account_id),
        )


def send_single_tracked_email(
    contact: Dict[str, Any],
    subject_tpl: str,
    body_tpl: str,
    smtp_acc: Dict[str, Any],
    base_url: str = "http://127.0.0.1:5050",
    include_unsubscribe: bool = True,
    sequence_id: Optional[int] = None,
    step_order: int = 0,
) -> Tuple[bool, str, str]:
    tracking_id = uuid.uuid4().hex
    subj = services.render_merge_text(subject_tpl, contact)
    body_rendered = services.render_merge_text(body_tpl, contact)
    ws = db.get_workspace(int(contact.get("workspace_id") or 1))
    if ws.get("public_base_url"):
        base_url = ws["public_base_url"].rstrip("/")
    sig = (ws.get("email_signature") or "").strip()

    unsub_url = f"{base_url.rstrip('/')}/unsubscribe/{tracking_id}"
    pixel_url = f"{base_url.rstrip('/')}/t/open/{tracking_id}.gif"

    if _looks_like_html(body_rendered):
        plain_core = _strip_html(body_rendered)
        html_body = body_rendered
    else:
        plain_core = body_rendered
        html_body = "<br>".join(html.escape(ln) for ln in body_rendered.splitlines())

    if sig:
        plain_core = f"{plain_core}\n\n--\n{sig}"
        sig_html = "<br>".join(html.escape(ln) for ln in sig.splitlines())
        html_body += f'<div style="margin-top:16px;padding-top:10px;border-top:1px solid #e2e8f0;color:#475569;font-size:13px;">{sig_html}</div>'

    plain_full = plain_core
    if include_unsubscribe:
        plain_full += f"\n\n---\nPour ne plus recevoir nos messages : {unsub_url}"

    unsub_html = (
        f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">'
        f'<p style="font-size:11px;color:#94a3b8;">Pour ne plus recevoir nos emails, '
        f'<a href="{unsub_url}" style="color:#64748b;">cliquez ici pour vous désinscrire</a>.</p>'
        if include_unsubscribe
        else ""
    )
    html_full = (
        f'<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#0f172a;line-height:1.5;">'
        f"{html_body}{unsub_html}"
        f'<img src="{pixel_url}" width="1" height="1" alt="" style="display:none;" />'
        f"</body></html>"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = _clean_header(subj)
    from_name = _clean_header(smtp_acc.get("from_name") or "")
    username = _clean_header(smtp_acc["username"])
    to_email = _clean_header(contact["email"])
    smtp_password = db.decrypt_secret(smtp_acc.get("password") or "")
    msg["From"] = formataddr((from_name, username)) if from_name else username
    msg["To"] = to_email
    if include_unsubscribe:
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(plain_full, "plain", "utf-8"))
    msg.attach(MIMEText(html_full, "html", "utf-8"))

    now = datetime.now().isoformat(timespec="seconds")
    try:
        server = smtplib.SMTP(smtp_acc["host"], int(smtp_acc.get("port") or 587), timeout=20)
        try:
            if smtp_acc.get("use_tls", 1):
                server.starttls(context=ssl.create_default_context())
            server.login(username, smtp_password)
            server.sendmail(username, [to_email], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

        _increment_smtp_usage(smtp_acc.get("id") or 0)
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO email_logs (
                    tracking_id, contact_id, email, sequence_id, step_order,
                    smtp_account_id, subject, status, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?)
                """,
                (tracking_id, contact["id"], contact["email"], sequence_id, step_order, smtp_acc.get("id") or 0, subj, now),
            )
            conn.execute(
                """
                UPDATE contacts SET
                    status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                    last_contacted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, contact["id"]),
            )
        return True, tracking_id, ""
    except Exception as exc:
        err = str(exc)
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO email_logs (
                    tracking_id, contact_id, email, sequence_id, step_order,
                    smtp_account_id, subject, status, sent_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?)
                """,
                (tracking_id, contact["id"], contact["email"], sequence_id, step_order, smtp_acc.get("id") or 0, subj, now, err),
            )
        return False, tracking_id, err


# ---------------- SEQUENCE SCHEDULER ----------------


def process_due_sequences(max_batch: int = 10, apply_delay: bool = False, base_url: str = "http://127.0.0.1:5050") -> Dict[str, Any]:
    if not _seq_process_lock.acquire(blocking=False):
        return {"processed": 0, "sent": 0, "errors": 0, "completed": 0, "skipped_busy": True}
    try:
        return _process_due_sequences_locked(max_batch=max_batch, apply_delay=apply_delay, base_url=base_url)
    finally:
        _seq_process_lock.release()


def _process_due_sequences_locked(max_batch: int = 10, apply_delay: bool = False, base_url: str = "http://127.0.0.1:5050") -> Dict[str, Any]:
    now_iso = datetime.now().isoformat(timespec="seconds")
    sent = 0
    errors = 0
    completed = 0

    with db.get_conn() as conn:
        due = [
            dict(r)
            for r in conn.execute(
                """
                SELECT e.*, s.status AS seq_status, s.delay_min_sec, s.delay_max_sec, s.include_unsubscribe
                FROM sequence_enrollments e
                JOIN sequences s ON s.id = e.sequence_id
                WHERE e.state = 'active' AND s.status = 'active' AND e.next_send_at <= ?
                ORDER BY e.next_send_at ASC
                LIMIT ?
                """,
                (now_iso, max_batch),
            ).fetchall()
        ]

    for enr in due:
        with db.get_conn() as conn:
            contact_row = conn.execute("SELECT * FROM contacts WHERE id = ?", (enr["contact_id"],)).fetchone()
            steps = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY step_order ASC",
                    (enr["sequence_id"],),
                ).fetchall()
            ]

        if not contact_row or contact_row["status"] == "unsubscribed":
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE sequence_enrollments SET state = 'stopped_unsubscribed' WHERE id = ?",
                    (enr["id"],),
                )
            continue

        contact = dict(contact_row)
        next_step_idx = enr["current_step"]
        if next_step_idx >= len(steps):
            with db.get_conn() as conn:
                conn.execute("UPDATE sequence_enrollments SET state = 'completed' WHERE id = ?", (enr["id"],))
            completed += 1
            continue

        step = steps[next_step_idx]
        smtp_acc = _pick_smtp_account()
        if not smtp_acc:
            break  # No available SMTP account under daily limit

        ok, _, err = send_single_tracked_email(
            contact=contact,
            subject_tpl=step["subject"],
            body_tpl=step["body"],
            smtp_acc=smtp_acc,
            base_url=base_url,
            include_unsubscribe=bool(enr["include_unsubscribe"]),
            sequence_id=enr["sequence_id"],
            step_order=step["step_order"],
        )

        if ok:
            sent += 1
            new_step = next_step_idx + 1
            if new_step >= len(steps):
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE sequence_enrollments SET current_step = ?, state = 'completed', last_error = '' WHERE id = ?",
                        (new_step, enr["id"]),
                    )
                completed += 1
            else:
                delay_days = max(0, int(steps[new_step].get("delay_days") or 1))
                next_dt = (datetime.now() + timedelta(days=delay_days)).isoformat(timespec="seconds")
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE sequence_enrollments SET current_step = ?, next_send_at = ?, last_error = '' WHERE id = ?",
                        (new_step, next_dt, enr["id"]),
                    )
        else:
            errors += 1
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE sequence_enrollments SET state = 'stopped_error', last_error = ? WHERE id = ?",
                    (err[:300], enr["id"]),
                )

        if apply_delay and sent < len(due):
            dmin = max(1, int(enr.get("delay_min_sec") or 15))
            dmax = max(dmin, int(enr.get("delay_max_sec") or 40))
            time.sleep(random.uniform(dmin, dmax))

    return {"due_checked": len(due), "sent": sent, "errors": errors, "completed": completed}


# ---------------- IMAP REPLY & BOUNCE AUTO-STOP ----------------

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem", "mail-daemon")
BOUNCE_SUBJECT_HINTS = (
    "undelivered", "delivery status notification", "failure notice",
    "returned mail", "mail delivery failed", "address not found",
    "undeliverable", "delivery has failed", "non remis",
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _decode_hdr(val: str) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(text))
    return "".join(out)


def check_replies_and_bounces(
    workspace_id: int = 1,
    host: str = "",
    port: int = 993,
    username: str = "",
    password: str = "",
    max_messages: int = 80,
) -> Dict[str, Any]:
    if not host or not username or not password:
        ws_imap = db.get_workspace_imap(workspace_id)
        host = host or ws_imap.get("host", "")
        port = port or ws_imap.get("port", 993)
        username = username or ws_imap.get("username", "")
        password = password or ws_imap.get("password", "")

    if not host or not username or not password:
        raise ValueError("Configuration IMAP manquante. Configurez votre boîte IMAP dans Admin / Marque blanche ou Séquences.")

    with db.get_conn() as conn:
        active_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT e.id AS enrollment_id, e.sequence_id, c.id AS contact_id, LOWER(c.email) AS email
                FROM sequence_enrollments e
                JOIN contacts c ON c.id = e.contact_id
                WHERE e.state = 'active'
                """
            ).fetchall()
        ]

    active_by_email: Dict[str, List[Dict[str, Any]]] = {}
    for r in active_rows:
        active_by_email.setdefault(r["email"], []).append(r)

    if not active_by_email:
        return {"scanned": 0, "replies_stopped": 0, "bounces_stopped": 0, "details": []}

    imap = imaplib.IMAP4_SSL(host, int(port or 993), timeout=20)
    replies_stopped = 0
    bounces_stopped = 0
    scanned = 0
    details: List[Dict[str, str]] = []
    now = datetime.now().isoformat(timespec="seconds")

    try:
        imap.login(username, password)
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return {"scanned": 0, "replies_stopped": 0, "bounces_stopped": 0, "details": []}

        msg_ids = data[0].split()[-max_messages:]
        for mid in reversed(msg_ids):
            scanned += 1
            st, msg_data = imap.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)] BODY.PEEK[TEXT]<0.2048>)")
            if st != "OK" or not msg_data:
                continue

            raw_bytes = b""
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                    raw_bytes += part[1] + b"\n"

            msg = email_lib.message_from_bytes(raw_bytes)
            from_hdr = _decode_hdr(msg.get("From", ""))
            subj_hdr = _decode_hdr(msg.get("Subject", ""))
            _, from_addr = parseaddr(from_hdr)
            from_addr = (from_addr or "").strip().lower()
            from_lower = from_hdr.lower()
            subj_lower = subj_hdr.lower()

            is_bounce = any(k in from_lower for k in BOUNCE_SENDERS) or any(k in subj_lower for k in BOUNCE_SUBJECT_HINTS)

            if is_bounce:
                body_text = raw_bytes.decode("utf-8", errors="ignore").lower()
                found_emails = set(EMAIL_RE.findall(body_text))
                for bounced_em in found_emails:
                    if bounced_em in active_by_email and bounced_em != username.lower():
                        targets = active_by_email.pop(bounced_em, [])
                        with db.get_conn() as conn:
                            for t in targets:
                                conn.execute(
                                    "UPDATE sequence_enrollments SET state = 'stopped_bounced', last_error = ? WHERE id = ?",
                                    (f"Bounce détecté: {subj_hdr[:120]}", t["enrollment_id"]),
                                )
                                conn.execute(
                                    "UPDATE contacts SET mx_status = 'invalid', updated_at = ? WHERE id = ?",
                                    (now, t["contact_id"]),
                                )
                                conn.execute(
                                    "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'email', ?, ?)",
                                    (t["contact_id"], f"⚠️ Bounce détecté ({subj_hdr[:80]}) — séquence arrêtée automatiquement.", now),
                                )
                                bounces_stopped += 1
                                details.append({"email": bounced_em, "type": "bounce", "subject": subj_hdr[:80]})
            elif from_addr and from_addr in active_by_email:
                targets = active_by_email.pop(from_addr, [])
                with db.get_conn() as conn:
                    for t in targets:
                        conn.execute(
                            "UPDATE sequence_enrollments SET state = 'stopped_replied', last_error = '' WHERE id = ?",
                            (t["enrollment_id"],),
                        )
                        conn.execute(
                            "UPDATE contacts SET status = CASE WHEN status IN ('new','contacted') THEN 'followup' ELSE status END, lead_score = MIN(100, COALESCE(lead_score, 0) + 15), last_contacted_at = ?, updated_at = ? WHERE id = ?",
                            (now, now, t["contact_id"]),
                        )
                        conn.execute(
                            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'email', ?, ?)",
                            (t["contact_id"], f"💬 Réponse email détectée ({subj_hdr[:80]}) — séquence arrêtée automatiquement.", now),
                        )
                        replies_stopped += 1
                        details.append({"email": from_addr, "type": "reply", "subject": subj_hdr[:80]})
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return {
        "scanned": scanned,
        "replies_stopped": replies_stopped,
        "bounces_stopped": bounces_stopped,
        "details": details,
    }


_seq_scheduler_started = False


def start_sequence_scheduler(interval_seconds: int = 45) -> None:
    global _seq_scheduler_started
    if _seq_scheduler_started:
        return
    _seq_scheduler_started = True

    def _loop() -> None:
        tick = 0
        while True:
            time.sleep(interval_seconds)
            tick += 1
            try:
                # Every ~5 minutes, auto-check IMAP replies & bounces if configured
                if tick % 7 == 1:
                    ws_imap = db.get_workspace_imap(1)
                    if ws_imap.get("host") and ws_imap.get("username") and ws_imap.get("password"):
                        check_replies_and_bounces(workspace_id=1)
                process_due_sequences(max_batch=5, apply_delay=True)
            except Exception:
                logger.exception("Erreur dans la boucle du scheduler de séquences")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
