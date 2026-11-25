import concurrent.futures
import os
import re
import smtplib
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List, Optional, Tuple

import dns.resolver

import db
import parsers

_MX_CACHE: Dict[str, str] = {}


def check_domain_mx(domain: str) -> str:
    dom = (domain or "").strip().lower()
    if not dom or "." not in dom:
        return "invalid"
    if dom in _MX_CACHE:
        return _MX_CACHE[dom]

    resolver = dns.resolver.Resolver()
    resolver.timeout = 3.0
    resolver.lifetime = 4.0
    try:
        answers = resolver.resolve(dom, "MX")
        status = "valid" if len(answers) > 0 else "invalid"
    except Exception:
        # Fallback: check A record (some small domains accept mail on A record)
        try:
            answers_a = resolver.resolve(dom, "A")
            status = "valid" if len(answers_a) > 0 else "invalid"
        except Exception:
            status = "invalid"

    _MX_CACHE[dom] = status
    return status


def verify_contacts_mx(contact_ids: Optional[List[int]] = None, limit: int = 500) -> Dict[str, Any]:
    with db.get_conn() as conn:
        if contact_ids:
            placeholders = ",".join("?" for _ in contact_ids)
            rows = conn.execute(
                f"SELECT id, email FROM contacts WHERE id IN ({placeholders})",
                contact_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, email FROM contacts WHERE mx_status = 'unchecked' LIMIT ?",
                (limit,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT id, email FROM contacts LIMIT ?", (limit,)
                ).fetchall()

    domains = sorted(
        {
            r["email"].split("@", 1)[1].lower()
            for r in rows
            if "@" in r["email"]
        }
    )

    domain_results: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(check_domain_mx, d): d for d in domains}
        for fut in concurrent.futures.as_completed(futs):
            d = futs[fut]
            try:
                domain_results[d] = fut.result()
            except Exception:
                domain_results[d] = "invalid"

    valid_cnt = 0
    invalid_cnt = 0
    with db.get_conn() as conn:
        for r in rows:
            dom = r["email"].split("@", 1)[1].lower() if "@" in r["email"] else ""
            st = domain_results.get(dom, "invalid")
            conn.execute("UPDATE contacts SET mx_status = ? WHERE id = ?", (st, r["id"]))
            if st == "valid":
                valid_cnt += 1
            else:
                invalid_cnt += 1

    return {
        "checked_contacts": len(rows),
        "checked_domains": len(domains),
        "valid": valid_cnt,
        "invalid": invalid_cnt,
    }


# ---------------- WATCHED FOLDERS ----------------

SKIP_WATCH_DIRS = {
    "node_modules", ".git", "__pycache__", "AppData", "Windows",
    "Program Files", "Program Files (x86)", "$Recycle.Bin",
}


def run_watched_folders_once() -> Dict[str, Any]:
    with db.get_conn() as conn:
        watchers = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM watched_folders WHERE enabled = 1"
            ).fetchall()
        ]
        known_files = {
            r["file_path"]: r["mtime"]
            for r in conn.execute("SELECT file_path, mtime FROM watched_file_state").fetchall()
        }

    total_files = 0
    total_inserted = 0
    total_updated = 0
    now = datetime.now().isoformat(timespec="seconds")

    for w in watchers:
        root = w["path"]
        if not os.path.isdir(root):
            continue
        exts = (
            {".csv", ".tsv", ".xlsx", ".xls", ".vcf", ".json"}
            if w["mode"] == "structured"
            else {".csv", ".tsv", ".xlsx", ".xls", ".vcf", ".json", ".pdf", ".docx", ".txt"}
        )

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_WATCH_DIRS and not d.startswith(".")
            ]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in exts:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue

                if fp in known_files and abs(known_files[fp] - mtime) < 1.0:
                    continue

                try:
                    records = parsers.parse_file(fp)
                    if records:
                        res = db.upsert_contacts(
                            records=records,
                            source_type="pc",
                            source_name=f"[Auto] {fn}",
                            source_ref=fp,
                            extra_tag=w["tag"] or "Auto-Import",
                        )
                        total_files += 1
                        total_inserted += res["inserted"]
                        total_updated += res["updated"]
                except Exception:
                    pass

                with db.get_conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO watched_file_state (file_path, mtime, imported_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(file_path) DO UPDATE SET mtime = excluded.mtime, imported_at = excluded.imported_at
                        """,
                        (fp, mtime, now),
                    )
                known_files[fp] = mtime

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE watched_folders SET last_scan_at = ? WHERE id = ?",
                (now, w["id"]),
            )

    return {
        "watchers_checked": len(watchers),
        "files_imported": total_files,
        "inserted": total_inserted,
        "updated": total_updated,
    }


_watcher_started = False


def start_background_watcher(interval_seconds: int = 60) -> None:
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                run_watched_folders_once()
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# ---------------- SMTP MAIL MERGE ----------------


def render_merge_text(template_str: str, contact: Dict[str, Any]) -> str:
    first = contact.get("first_name") or (contact.get("full_name") or "").split(" ")[0] or ""
    last = contact.get("last_name") or ""
    full = contact.get("full_name") or first or contact.get("email") or ""
    company = contact.get("company") or ""
    email = contact.get("email") or ""

    replacements = {
        "{prenom}": first,
        "{prénom}": first,
        "{nom}": last,
        "{nom_complet}": full,
        "{entreprise}": company,
        "{email}": email,
    }
    out = template_str
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


def send_campaign_smtp(
    contacts: List[Dict[str, Any]],
    subject_tpl: str,
    body_tpl: str,
    smtp_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    host = (smtp_cfg.get("host") or "").strip()
    port = int(smtp_cfg.get("port") or 587)
    username = (smtp_cfg.get("username") or "").strip()
    password = db.decrypt_secret(smtp_cfg.get("password") or "")
    from_name = (smtp_cfg.get("from_name") or "").strip()
    use_tls = bool(smtp_cfg.get("use_tls", True))

    if not host or not username or not password:
        raise ValueError("Configuration SMTP incomplète (hôte, email et mot de passe requis).")

    eligible = [c for c in contacts if c.get("status") != "unsubscribed"]
    skipped_unsub = len(contacts) - len(eligible)

    sent = 0
    errors: List[Dict[str, str]] = []
    now = datetime.now().isoformat(timespec="seconds")

    server = smtplib.SMTP(host, port, timeout=20)
    try:
        if use_tls:
            server.starttls()
        server.login(username, password)

        with db.get_conn() as conn:
            for c in eligible:
                try:
                    subj = render_merge_text(subject_tpl, c)
                    body = render_merge_text(body_tpl, c)
                    is_html = bool(re.search(r"<(b|strong|i|em|u|p|br|div|a|ul|ol|li|h[1-6]|span)\b", body, flags=re.IGNORECASE))
                    if is_html:
                        from email.mime.multipart import MIMEMultipart
                        msg = MIMEMultipart("alternative")
                        plain = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE))
                        msg.attach(MIMEText(plain, "plain", "utf-8"))
                        msg.attach(MIMEText(f'<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#0f172a;line-height:1.5;">{body}</body></html>', "html", "utf-8"))
                    else:
                        msg = MIMEText(body, "plain", "utf-8")
                    msg["Subject"] = subj
                    msg["From"] = formataddr((from_name, username)) if from_name else username
                    msg["To"] = c["email"]
                    server.sendmail(username, [c["email"]], msg.as_string())
                    sent += 1
                    conn.execute(
                        """
                        UPDATE contacts SET
                            status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                            last_contacted_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, c["id"]),
                    )
                except Exception as exc:
                    errors.append({"email": c["email"], "error": str(exc)})
    finally:
        try:
            server.quit()
        except Exception:
            pass

    return {
        "sent": sent,
        "skipped_unsubscribed": skipped_unsub,
        "errors": errors,
    }
