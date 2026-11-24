import html
import random
import smtplib
import threading
import time
import uuid
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List, Optional, Tuple

import dns.resolver

import db
import services


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
    body_text = services.render_merge_text(body_tpl, contact)
    unsub_url = f"{base_url.rstrip('/')}/unsubscribe/{tracking_id}"
    pixel_url = f"{base_url.rstrip('/')}/t/open/{tracking_id}.gif"

    plain_full = body_text
    if include_unsubscribe:
        plain_full += f"\n\n---\nPour ne plus recevoir nos messages : {unsub_url}"

    html_body = "<br>".join(html.escape(ln) for ln in body_text.splitlines())
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
    msg["Subject"] = subj
    from_name = (smtp_acc.get("from_name") or "").strip()
    username = smtp_acc["username"].strip()
    msg["From"] = formataddr((from_name, username)) if from_name else username
    msg["To"] = contact["email"]
    if include_unsubscribe:
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg.attach(MIMEText(plain_full, "plain", "utf-8"))
    msg.attach(MIMEText(html_full, "html", "utf-8"))

    now = datetime.now().isoformat(timespec="seconds")
    try:
        server = smtplib.SMTP(smtp_acc["host"], int(smtp_acc.get("port") or 587), timeout=20)
        try:
            if smtp_acc.get("use_tls", 1):
                server.starttls()
            server.login(username, smtp_acc["password"])
            server.sendmail(username, [contact["email"]], msg.as_string())
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


_seq_scheduler_started = False


def start_sequence_scheduler(interval_seconds: int = 45) -> None:
    global _seq_scheduler_started
    if _seq_scheduler_started:
        return
    _seq_scheduler_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                process_due_sequences(max_batch=5, apply_delay=True)
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
