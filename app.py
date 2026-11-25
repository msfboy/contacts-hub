import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import string
import tempfile
from datetime import datetime
from typing import Any, Dict, List

import openpyxl
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, session, url_for

import ai_assistant
import booking_hub
import campaigns
import connectors
import db
import enrichment
import gdrive_client
import local_radar
import parsers
import payments_hub
import prospecting
import services
import social_hub
import wa_broadcast
import wa_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTS_DIR = os.path.join(BASE_DIR, "imports_cache")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(IMPORTS_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
db.init_db()


def _get_setting(key: str, default: str = "") -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def _set_setting(key: str, value: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return f"{salt}${dk}"


def _verify_password(password: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt, expected = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return hmac.compare_digest(dk, expected)


def _log_audit(action: str, detail: str = "") -> None:
    user = session.get("username") or "local"
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_logs (username, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (user, action, detail[:300], now),
            )
    except Exception:
        pass


_sk = _get_setting("flask_secret_key")
if not _sk:
    _sk = secrets.token_hex(32)
    _set_setting("flask_secret_key", _sk)
app.secret_key = _sk

services.start_background_watcher(interval_seconds=60)
campaigns.start_sequence_scheduler(interval_seconds=45)

PUBLIC_PREFIXES = ("/login", "/logout", "/static/", "/t/open/", "/unsubscribe/", "/api/webhooks/", "/f/", "/l/")


def _current_workspace_id() -> int:
    try:
        return max(1, int(session.get("workspace_id") or 1))
    except (TypeError, ValueError):
        return 1


@app.before_request
def _enforce_optional_auth():
    if _get_setting("auth_enabled", "0") != "1":
        return None
    path = request.path or ""
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return None
    if not session.get("user_id"):
        if path.startswith("/api/"):
            return jsonify({"error": "Authentification requise."}), 401
        return redirect(url_for("login_page"))
    if path.startswith("/api/admin/") and path != "/api/admin/status" and session.get("role") != "admin":
        return jsonify({"error": "Accès réservé aux administrateurs."}), 403
    if session.get("role") == "viewer" and request.method in ("POST", "PUT", "DELETE"):
        if not path.startswith("/api/search/natural"):
            return jsonify({"error": "Rôle Lecture seule : modification non autorisée."}), 403
    return None


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        with db.get_conn() as conn:
            u = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (username,)).fetchone()
        if u and _verify_password(password, u["password_hash"]):
            session["user_id"] = u["id"]
            session["username"] = u["username"]
            session["display_name"] = u["display_name"] or u["username"]
            session["role"] = u["role"]
            _log_audit("login", f"Connexion de {u['username']} ({u['role']})")
            return redirect("/")
        return render_template("login.html", error="Identifiant ou mot de passe incorrect.")
    return render_template("login.html", error=None)


@app.route("/logout", methods=["GET", "POST"])
def logout_page():
    _log_audit("logout", f"Déconnexion de {session.get('username', 'local')}")
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    return render_template("index.html")


# ---------------- STATS & CONTACTS ----------------


@app.route("/api/stats", methods=["GET"])
def api_stats():
    ws_id = _current_workspace_id()
    stats = db.get_stats(workspace_id=ws_id)
    ws = db.get_workspace(ws_id)
    stats["workspace"] = ws
    return jsonify(stats)


@app.route("/api/contacts", methods=["GET"])
def api_list_contacts():
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    sort_by = request.args.get("sort_by", "updated_desc")

    items, total = db.query_contacts(
        **_filter_args(),
        workspace_id=_current_workspace_id(),
        sort_by=sort_by,
        page=page,
        per_page=per_page,
    )
    return jsonify(
        {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }
    )


@app.route("/api/contacts", methods=["POST"])
def api_add_contact():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Adresse email invalide."}), 400

    res = db.upsert_contacts(
        records=[data],
        source_type="manual",
        source_name="Ajout manuel",
        extra_tag="",
        workspace_id=_current_workspace_id(),
    )
    return jsonify(res), 201


@app.route("/api/contacts/<int:contact_id>", methods=["PUT"])
def api_update_contact(contact_id: int):
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Adresse email invalide."}), 400

    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    full_name = (data.get("full_name") or "").strip() or f"{first_name} {last_name}".strip()
    status = (data.get("status") or "new").strip().lower()
    if status not in db.VALID_STATUSES:
        status = "new"
    try:
        deal_value = float(data.get("deal_value") or 0)
    except (TypeError, ValueError):
        deal_value = 0.0
    email_type = db.classify_email(email)
    now = datetime.now().isoformat(timespec="seconds")

    with db.get_conn() as conn:
        try:
            conn.execute(
                """
                UPDATE contacts SET
                    email = ?, first_name = ?, last_name = ?, full_name = ?,
                    phone = ?, company = ?, role = ?, tags = ?, notes = ?,
                    status = ?, deal_value = ?, email_type = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    email, first_name, last_name, full_name,
                    (data.get("phone") or "").strip(),
                    (data.get("company") or "").strip(),
                    (data.get("role") or "").strip(),
                    db.normalize_tags(data.get("tags") or ""),
                    (data.get("notes") or "").strip(),
                    status, deal_value, email_type, now, contact_id,
                ),
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/contacts/<int:contact_id>/deal", methods=["PUT"])
def api_update_contact_deal(contact_id: int):
    data = request.get_json(force=True) or {}
    try:
        deal_value = max(0.0, float(data.get("deal_value") or 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Montant invalide."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE contacts SET deal_value = ?, updated_at = ? WHERE id = ?",
            (deal_value, now, contact_id),
        )
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'note', ?, ?)",
            (contact_id, f"💰 Montant opportunité mis à jour : {deal_value:,.0f}", now),
        )
    return jsonify({"ok": True, "deal_value": deal_value})


@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
def api_delete_contact(contact_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    return jsonify({"ok": True})


@app.route("/api/contacts/bulk", methods=["POST"])
def api_bulk_contacts():
    data = request.get_json(force=True) or {}
    action = data.get("action")
    ids = data.get("ids") or []
    if not ids:
        return jsonify({"error": "Aucun contact sélectionné."}), 400

    placeholders = ",".join("?" for _ in ids)
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        if action == "delete":
            conn.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", ids)
            return jsonify({"ok": True, "affected": len(ids)})
        if action == "add_tag":
            tag = (data.get("tag") or "").strip()
            if not tag:
                return jsonify({"error": "Tag requis."}), 400
            rows = conn.execute(
                f"SELECT id, tags FROM contacts WHERE id IN ({placeholders})", ids
            ).fetchall()
            for r in rows:
                merged = db.merge_tags(r["tags"], tag)
                conn.execute(
                    "UPDATE contacts SET tags = ?, updated_at = ? WHERE id = ?",
                    (merged, now, r["id"]),
                )
            return jsonify({"ok": True, "affected": len(rows)})
        if action == "set_status":
            status = (data.get("status") or "").strip().lower()
            if status not in db.VALID_STATUSES:
                return jsonify({"error": "Statut invalide."}), 400
            conn.execute(
                f"UPDATE contacts SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                [status, now] + ids,
            )
            return jsonify({"ok": True, "affected": len(ids)})

    return jsonify({"error": "Action inconnue."}), 400


# ---------------- ENRICHMENT & SMART SEARCH ----------------


@app.route("/api/enrich/auto", methods=["POST"])
def api_enrich_auto():
    res = enrichment.run_auto_enrichment()
    return jsonify(res)


@app.route("/api/enrich/web", methods=["POST"])
def api_enrich_web():
    data = request.get_json(silent=True) or {}
    limit_domains = min(100, max(5, int(data.get("limit_domains", 40))))
    res = enrichment.run_web_enrichment(limit_domains=limit_domains)
    return jsonify(res)


@app.route("/api/search/natural", methods=["POST"])
def api_search_natural():
    data = request.get_json(force=True) or {}
    prompt = data.get("prompt") or ""
    parsed = enrichment.parse_natural_query(prompt)
    return jsonify(parsed)


# ---------------- QUALITY & CLEANING ----------------


@app.route("/api/quality/verify-mx", methods=["POST"])
def api_verify_mx():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or None
    limit = min(1000, int(data.get("limit", 500)))
    res = services.verify_contacts_mx(contact_ids=ids, limit=limit)
    return jsonify(res)


@app.route("/api/quality/reclassify", methods=["POST"])
def api_reclassify():
    updated = db.reclassify_all_contacts()
    return jsonify({"updated": updated})


@app.route("/api/quality/duplicates", methods=["GET"])
def api_duplicates():
    return jsonify({"groups": db.find_duplicate_groups()})


@app.route("/api/quality/merge", methods=["POST"])
def api_merge_duplicates():
    data = request.get_json(force=True) or {}
    primary_id = int(data.get("primary_id"))
    secondary_ids = [int(x) for x in (data.get("secondary_ids") or [])]
    try:
        res = db.merge_contacts(primary_id, secondary_ids)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ---------------- EXPORT & QUICK COPY ----------------


def _filter_args() -> Dict[str, Any]:
    min_score_raw = request.args.get("min_score", "0")
    try:
        min_score = int(min_score_raw) if min_score_raw else 0
    except ValueError:
        min_score = 0
    return {
        "q": request.args.get("q", ""),
        "tag": request.args.get("tag", ""),
        "source_type": request.args.get("source_type", ""),
        "domain": request.args.get("domain", ""),
        "status": request.args.get("status", ""),
        "email_type": request.args.get("email_type", ""),
        "mx_status": request.args.get("mx_status", ""),
        "country": request.args.get("country", ""),
        "min_score": min_score,
        "has_phone": request.args.get("has_phone", "").lower() in ("1", "true", "yes"),
    }


@app.route("/api/contacts/emails", methods=["GET"])
def api_contacts_emails():
    items, _ = db.query_contacts(**_filter_args(), all_rows=True)
    emails = [c["email"] for c in items if c.get("email") and c.get("status") != "unsubscribed"]
    return jsonify({"count": len(emails), "emails": emails})


@app.route("/api/export", methods=["GET"])
def api_export():
    fmt = request.args.get("format", "csv").lower()
    items, _ = db.query_contacts(**_filter_args(), all_rows=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")

    if fmt == "xlsx":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Contacts"
        ws.append([
            "Email", "Nom complet", "Prénom", "Nom", "Téléphone",
            "Entreprise", "Poste", "Statut", "Type", "MX", "Tags", "Source", "Notes"
        ])
        for c in items:
            ws.append([
                c["email"], c["full_name"], c["first_name"], c["last_name"],
                c["phone"], c["company"], c["role"], c["status"],
                c["email_type"], c["mx_status"], c["tags"],
                c["source_detail"] or c["source_type"], c["notes"]
            ])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"contacts_{stamp}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if fmt == "vcf":
        lines = []
        for c in items:
            lines.append("BEGIN:VCARD")
            lines.append("VERSION:3.0")
            if c["full_name"]:
                lines.append(f"FN:{c['full_name']}")
            lines.append(f"N:{c['last_name']};{c['first_name']};;;")
            lines.append(f"EMAIL;TYPE=INTERNET:{c['email']}")
            if c["phone"]:
                lines.append(f"TEL:{c['phone']}")
            if c["company"]:
                lines.append(f"ORG:{c['company']}")
            if c["role"]:
                lines.append(f"TITLE:{c['role']}")
            if c["notes"]:
                lines.append(f"NOTE:{c['notes']}")
            lines.append("END:VCARD")
        return Response(
            "\r\n".join(lines),
            mimetype="text/vcard; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="contacts_{stamp}.vcf"'},
        )

    out = io.StringIO()
    if fmt == "google_contacts":
        writer = csv.writer(out, delimiter=",")
        writer.writerow([
            "Name", "Given Name", "Family Name", "E-mail 1 - Value",
            "Phone 1 - Value", "Organization Name", "Organization Title",
            "Group Membership", "Notes"
        ])
        for c in items:
            writer.writerow([
                c["full_name"], c["first_name"], c["last_name"], c["email"],
                c["phone"], c["company"], c["role"], c["tags"], c["notes"]
            ])
        fname = f"google_contacts_{stamp}.csv"
    elif fmt == "brevo":
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["EMAIL", "PRENOM", "NOM", "SMS", "ENTREPRISE", "TAGS"])
        for c in items:
            if c["status"] == "unsubscribed":
                continue
            writer.writerow([
                c["email"], c["first_name"], c["last_name"],
                c["phone"], c["company"], c["tags"]
            ])
        fname = f"brevo_contacts_{stamp}.csv"
    elif fmt == "mailchimp":
        writer = csv.writer(out, delimiter=",")
        writer.writerow(["Email Address", "First Name", "Last Name", "Phone Number", "Company", "Tags"])
        for c in items:
            if c["status"] == "unsubscribed":
                continue
            writer.writerow([
                c["email"], c["first_name"], c["last_name"],
                c["phone"], c["company"], c["tags"]
            ])
        fname = f"mailchimp_contacts_{stamp}.csv"
    else:
        writer = csv.writer(out, delimiter=";")
        writer.writerow([
            "Email", "Nom complet", "Prénom", "Nom", "Téléphone",
            "Entreprise", "Poste", "Statut", "Type", "MX", "Tags", "Source", "Notes"
        ])
        for c in items:
            writer.writerow([
                c["email"], c["full_name"], c["first_name"], c["last_name"],
                c["phone"], c["company"], c["role"], c["status"],
                c["email_type"], c["mx_status"], c["tags"],
                c["source_detail"] or c["source_type"], c["notes"]
            ])
        fname = f"contacts_{stamp}.csv"

    csv_bytes = ("\ufeff" + out.getvalue()).encode("utf-8")
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------- GOOGLE DRIVE & BACKUP ----------------


@app.route("/api/gdrive/status", methods=["GET"])
def api_gdrive_status():
    account = request.args.get("account") or None
    return jsonify(gdrive_client.check_status(account=account))


@app.route("/api/gdrive/files", methods=["GET"])
def api_gdrive_files():
    folder_id = request.args.get("folder_id") or None
    q = request.args.get("q", "").strip()
    file_type = request.args.get("type", "").strip()
    account = request.args.get("account") or None
    ok, items, err = gdrive_client.list_files(
        folder_id=folder_id, query=q, file_type=file_type, max_results=100, account=account
    )
    if not ok:
        return jsonify({"error": err, "items": []}), 500
    return jsonify({"items": items})


@app.route("/api/gdrive/import", methods=["POST"])
def api_gdrive_import():
    data = request.get_json(force=True) or {}
    files = data.get("files") or []
    extra_tag = (data.get("tag") or "").strip()
    account = (data.get("account") or "").strip() or None
    if not files:
        return jsonify({"error": "Aucun fichier sélectionné."}), 400

    results = []
    errors = []
    dest_dir = os.path.join(IMPORTS_DIR, "gdrive")

    for f in files:
        fid = f.get("id")
        name = f.get("name") or fid
        mime = f.get("mimeType") or ""
        ok, local_path, err = gdrive_client.download_file(fid, name, mime, dest_dir, account=account)
        if not ok:
            errors.append({"name": name, "error": err})
            continue
        try:
            records = parsers.parse_file(local_path)
            summary = db.upsert_contacts(
                records=records,
                source_type="gdrive",
                source_name=name,
                source_ref=fid,
                extra_tag=extra_tag,
            )
            results.append(summary)
        except Exception as exc:
            errors.append({"name": name, "error": str(exc)})

    return jsonify({"results": results, "errors": errors})


@app.route("/api/backup/download", methods=["GET"])
def api_backup_download():
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return send_file(
        db.DB_PATH,
        as_attachment=True,
        download_name=f"contactshub_backup_{stamp}.db",
        mimetype="application/octet-stream",
    )


@app.route("/api/backup/gdrive", methods=["POST"])
def api_backup_gdrive():
    data = request.get_json(silent=True) or {}
    account = (data.get("account") or "").strip() or None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUPS_DIR, f"contactshub_backup_{stamp}.db")
    shutil.copy2(db.DB_PATH, backup_path)
    ok, out, err = gdrive_client.upload_backup(backup_path, account=account)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True, "filename": os.path.basename(backup_path), "output": out})


# ---------------- PREVIEW BEFORE IMPORT ----------------


@app.route("/api/preview/local", methods=["POST"])
def api_preview_local():
    data = request.get_json(force=True) or {}
    path = data.get("path") or ""
    if not os.path.isfile(path):
        return jsonify({"error": "Fichier introuvable."}), 404
    try:
        records = parsers.parse_file(path)
        return jsonify({
            "name": os.path.basename(path),
            "total": len(records),
            "sample": records[:25],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/preview/gdrive", methods=["POST"])
def api_preview_gdrive():
    data = request.get_json(force=True) or {}
    fid = data.get("id") or ""
    name = data.get("name") or fid
    mime = data.get("mimeType") or ""
    account = (data.get("account") or "").strip() or None
    with tempfile.TemporaryDirectory() as tmpdir:
        ok, local_path, err = gdrive_client.download_file(fid, name, mime, tmpdir, account=account)
        if not ok:
            return jsonify({"error": err}), 500
        try:
            records = parsers.parse_file(local_path)
            return jsonify({"name": name, "total": len(records), "sample": records[:25]})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400


# ---------------- LOCAL PC BROWSER & UPLOAD ----------------


@app.route("/api/local/shortcuts", methods=["GET"])
def api_local_shortcuts():
    home = os.path.expanduser("~")
    candidates = [
        ("Bureau", os.path.join(home, "Desktop")),
        ("Documents", os.path.join(home, "Documents")),
        ("Téléchargements", os.path.join(home, "Downloads")),
        ("Dossier utilisateur", home),
    ]
    shortcuts = [{"label": lbl, "path": p} for lbl, p in candidates if os.path.isdir(p)]
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.isdir(drive):
            shortcuts.append({"label": f"Disque {letter}:", "path": drive})
    return jsonify({"shortcuts": shortcuts})


@app.route("/api/local/browse", methods=["GET"])
def api_local_browse():
    path = request.args.get("path") or os.path.expanduser("~")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return jsonify({"error": f"Dossier introuvable : {path}"}), 404

    parent = os.path.dirname(path) if os.path.dirname(path) != path else ""
    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                    ext = os.path.splitext(entry.name)[1].lower()
                    supported = (not is_dir) and (ext in parsers.SUPPORTED_EXTENSIONS)
                    size = 0 if is_dir else entry.stat().st_size
                    entries.append({
                        "name": entry.name,
                        "path": entry.path,
                        "isDir": is_dir,
                        "ext": ext,
                        "supported": supported,
                        "size": size,
                    })
                except OSError:
                    continue
    except PermissionError:
        return jsonify({"error": "Accès refusé à ce dossier."}), 403

    entries.sort(key=lambda x: (not x["isDir"], not x["supported"], x["name"].lower()))
    return jsonify({"path": path, "parent": parent, "entries": entries})


SKIP_SCAN_DIRS = {
    "node_modules", ".git", "__pycache__", "AppData", "Windows",
    "Program Files", "Program Files (x86)", "$Recycle.Bin",
    "System Volume Information", "Recovery", "PerfLogs", "venv", ".venv",
}


def _scan_directory(root: str, exts: set, max_files: int) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_SCAN_DIRS and not d.startswith(".") and not d.startswith("$")
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in exts:
                full_path = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    size = 0
                found.append({"name": fn, "path": full_path, "ext": ext, "size": size})
                if len(found) >= max_files:
                    return found
    return found


@app.route("/api/local/scan", methods=["GET"])
def api_local_scan():
    root = request.args.get("path") or os.path.join(os.path.expanduser("~"), "Documents")
    root = os.path.abspath(root)
    max_files = min(400, int(request.args.get("max", 200)))
    mode = request.args.get("mode", "all")
    if not os.path.isdir(root):
        return jsonify({"error": f"Dossier introuvable : {root}"}), 404

    exts = (
        {".csv", ".tsv", ".xlsx", ".xls", ".vcf", ".json"}
        if mode == "structured"
        else {".csv", ".tsv", ".xlsx", ".xls", ".vcf", ".json", ".pdf", ".docx", ".txt"}
    )
    found = _scan_directory(root, exts, max_files)
    return jsonify({"root": root, "files": found, "truncated": len(found) >= max_files})


@app.route("/api/local/scan-all", methods=["GET"])
def api_local_scan_all():
    mode = request.args.get("mode", "structured")
    max_per_root = min(150, int(request.args.get("max_per_root", 80)))
    exts = (
        {".csv", ".tsv", ".xlsx", ".xls", ".vcf", ".json"}
        if mode == "structured"
        else {".csv", ".tsv", ".xlsx", ".xls", ".vcf", ".json", ".pdf", ".docx", ".txt"}
    )

    home = os.path.expanduser("~")
    roots = [
        ("Bureau", os.path.join(home, "Desktop")),
        ("Documents", os.path.join(home, "Documents")),
        ("Téléchargements", os.path.join(home, "Downloads")),
        ("Disque D:", "D:\\"),
    ]
    all_files: List[Dict[str, Any]] = []
    scanned_roots = []
    for label, rpath in roots:
        if os.path.isdir(rpath):
            scanned_roots.append(label)
            hits = _scan_directory(rpath, exts, max_per_root)
            for h in hits:
                h["zone"] = label
            all_files.extend(hits)

    return jsonify({"zones": scanned_roots, "files": all_files})


@app.route("/api/local/import", methods=["POST"])
def api_local_import():
    data = request.get_json(force=True) or {}
    paths = data.get("paths") or []
    extra_tag = (data.get("tag") or "").strip()
    if not paths:
        return jsonify({"error": "Aucun fichier sélectionné."}), 400

    results = []
    errors = []
    for p in paths:
        if not os.path.isfile(p):
            errors.append({"name": p, "error": "Fichier introuvable."})
            continue
        try:
            records = parsers.parse_file(p)
            summary = db.upsert_contacts(
                records=records,
                source_type="pc",
                source_name=os.path.basename(p),
                source_ref=p,
                extra_tag=extra_tag,
            )
            results.append(summary)
        except Exception as exc:
            errors.append({"name": os.path.basename(p), "error": str(exc)})

    return jsonify({"results": results, "errors": errors})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files")
    extra_tag = (request.form.get("tag") or "").strip()
    if not files:
        return jsonify({"error": "Aucun fichier envoyé."}), 400

    results = []
    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for f in files:
            filename = f.filename or "upload.txt"
            dest = os.path.join(tmpdir, os.path.basename(filename))
            f.save(dest)
            try:
                records = parsers.parse_file(dest)
                summary = db.upsert_contacts(
                    records=records,
                    source_type="upload",
                    source_name=filename,
                    source_ref=filename,
                    extra_tag=extra_tag,
                )
                results.append(summary)
            except Exception as exc:
                errors.append({"name": filename, "error": str(exc)})

    return jsonify({"results": results, "errors": errors})


# ---------------- WATCHED FOLDERS ----------------


@app.route("/api/watchers", methods=["GET"])
def api_list_watchers():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM watched_folders ORDER BY id DESC").fetchall()]
    return jsonify({"items": rows})


@app.route("/api/watchers", methods=["POST"])
def api_add_watcher():
    data = request.get_json(force=True) or {}
    path = os.path.abspath((data.get("path") or "").strip())
    if not os.path.isdir(path):
        return jsonify({"error": f"Dossier introuvable : {path}"}), 400
    tag = (data.get("tag") or "").strip()
    mode = data.get("mode") if data.get("mode") in ("structured", "all") else "structured"
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO watched_folders (path, tag, mode, enabled, last_scan_at, created_at)
            VALUES (?, ?, ?, 1, '', ?)
            ON CONFLICT(path) DO UPDATE SET tag = excluded.tag, mode = excluded.mode, enabled = 1
            """,
            (path, tag, mode, now),
        )
    return jsonify({"ok": True}), 201


@app.route("/api/watchers/<int:watcher_id>", methods=["DELETE"])
def api_delete_watcher(watcher_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM watched_folders WHERE id = ?", (watcher_id,))
    return jsonify({"ok": True})


@app.route("/api/watchers/run", methods=["POST"])
def api_run_watchers():
    res = services.run_watched_folders_once()
    return jsonify(res)


# ---------------- EMAIL TEMPLATES & CAMPAIGNS ----------------


@app.route("/api/templates", methods=["GET"])
def api_list_templates():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM email_templates ORDER BY id DESC").fetchall()]
    return jsonify({"items": rows})


@app.route("/api/templates", methods=["POST"])
def api_create_template():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not name or not subject:
        return jsonify({"error": "Nom et objet requis."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO email_templates (name, subject, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, subject, body, now, now),
        )
        tid = cur.lastrowid
    return jsonify({"id": tid, "ok": True}), 201


@app.route("/api/templates/<int:tid>", methods=["PUT"])
def api_update_template(tid: int):
    data = request.get_json(force=True) or {}
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE email_templates SET name = ?, subject = ?, body = ?, updated_at = ? WHERE id = ?",
            (
                (data.get("name") or "").strip(),
                (data.get("subject") or "").strip(),
                (data.get("body") or "").strip(),
                now,
                tid,
            ),
        )
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>", methods=["DELETE"])
def api_delete_template(tid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM email_templates WHERE id = ?", (tid,))
    return jsonify({"ok": True})


@app.route("/api/smtp", methods=["GET"])
def api_get_smtp():
    with db.get_conn() as conn:
        row = conn.execute("SELECT host, port, username, from_name, use_tls FROM smtp_config WHERE id = 1").fetchone()
    if not row:
        return jsonify({"configured": False, "host": "smtp.gmail.com", "port": 587, "username": "", "from_name": "", "use_tls": True})
    d = dict(row)
    d["configured"] = bool(d["host"] and d["username"])
    d["use_tls"] = bool(d["use_tls"])
    return jsonify(d)


@app.route("/api/smtp", methods=["POST"])
def api_save_smtp():
    data = request.get_json(force=True) or {}
    host = (data.get("host") or "").strip()
    port = int(data.get("port") or 587)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    from_name = (data.get("from_name") or "").strip()
    use_tls = 1 if data.get("use_tls", True) else 0

    with db.get_conn() as conn:
        existing = conn.execute("SELECT password FROM smtp_config WHERE id = 1").fetchone()
        if not password and existing:
            enc_password = existing["password"]
        else:
            enc_password = db.encrypt_secret(password)
        conn.execute(
            """
            INSERT INTO smtp_config (id, host, port, username, password, from_name, use_tls)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                host = excluded.host, port = excluded.port,
                username = excluded.username, password = excluded.password,
                from_name = excluded.from_name, use_tls = excluded.use_tls
            """,
            (host, port, username, enc_password, from_name, use_tls),
        )
    return jsonify({"ok": True})


@app.route("/api/campaigns/send", methods=["POST"])
def api_send_campaign():
    data = request.get_json(force=True) or {}
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    tag = (data.get("tag") or "").strip()
    status = (data.get("status") or "").strip()
    email_type = (data.get("email_type") or "").strip()
    max_recipients = min(200, int(data.get("max_recipients", 50)))

    if not subject or not body:
        return jsonify({"error": "Objet et message requis."}), 400

    with db.get_conn() as conn:
        cfg_row = conn.execute("SELECT * FROM smtp_config WHERE id = 1").fetchone()
    if not cfg_row:
        return jsonify({"error": "Configurez d'abord votre serveur SMTP."}), 400

    contacts, _ = db.query_contacts(
        tag=tag, status=status, email_type=email_type, all_rows=True
    )
    contacts = [c for c in contacts if c["status"] != "unsubscribed"][:max_recipients]
    if not contacts:
        return jsonify({"error": "Aucun contact éligible trouvé avec ces filtres."}), 400

    try:
        res = services.send_campaign_smtp(contacts, subject, body, dict(cfg_row))
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/imports", methods=["GET"])
def api_imports():
    return jsonify({"items": db.get_import_logs()})


# ---------------- CONNECTORS: IMAP, OCR & WEBHOOKS ----------------


def _get_webhook_token() -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'webhook_token'").fetchone()
        if row:
            return row["value"]
        tok = "ch_" + secrets.token_hex(16)
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('webhook_token', ?)", (tok,)
        )
        return tok


@app.route("/api/connectors/imap/import", methods=["POST"])
def api_connector_imap():
    data = request.get_json(force=True) or {}
    host = (data.get("host") or "").strip()
    port = int(data.get("port") or 993)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    folder = (data.get("folder") or "INBOX").strip()
    limit = min(500, max(10, int(data.get("limit") or 150)))
    parse_signatures = bool(data.get("parse_signatures", True))
    extra_tag = (data.get("tag") or "Boîte Email").strip()

    try:
        fetched = connectors.fetch_imap_contacts(
            host=host,
            port=port,
            username=username,
            password=password,
            folder=folder,
            limit=limit,
            parse_signatures=parse_signatures,
        )
        summary = db.upsert_contacts(
            records=fetched["contacts"],
            source_type="imap",
            source_name=f"{username} ({folder})",
            source_ref=f"{host}/{folder}",
            extra_tag=extra_tag,
        )
        summary["emails_scanned"] = fetched["emails_scanned"]
        return jsonify(summary)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/connectors/ocr/scan", methods=["POST"])
def api_connector_ocr():
    files = request.files.getlist("files")
    extra_tag = (request.form.get("tag") or "Carte de visite OCR").strip()
    auto_import = (request.form.get("auto_import") or "true").lower() in ("1", "true", "yes")
    if not files:
        return jsonify({"error": "Aucune image envoyée."}), 400

    all_contacts: List[Dict[str, Any]] = []
    previews: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for f in files:
            fn = f.filename or "image.png"
            dest = os.path.join(tmpdir, os.path.basename(fn))
            f.save(dest)
            try:
                res = connectors.extract_contacts_from_image(dest)
                previews.append({
                    "filename": fn,
                    "raw_text": res["raw_text"],
                    "contacts": res["contacts"],
                })
                all_contacts.extend(res["contacts"])
            except Exception as exc:
                errors.append({"filename": fn, "error": str(exc)})

    summary = None
    if auto_import and all_contacts:
        summary = db.upsert_contacts(
            records=all_contacts,
            source_type="ocr",
            source_name=f"OCR ({len(files)} image(s))",
            source_ref="ocr",
            extra_tag=extra_tag,
        )

    return jsonify({
        "previews": previews,
        "total_contacts": len(all_contacts),
        "import_summary": summary,
        "errors": errors,
    })


@app.route("/api/webhooks/info", methods=["GET"])
def api_webhook_info():
    token = _get_webhook_token()
    url = f"{request.host_url.rstrip('/')}/api/webhooks/contacts"
    return jsonify({"url": url, "token": token})


@app.route("/api/webhooks/regenerate", methods=["POST"])
def api_webhook_regenerate():
    tok = "ch_" + secrets.token_hex(16)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('webhook_token', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (tok,),
        )
    return jsonify({"token": tok})


@app.route("/api/webhooks/contacts", methods=["POST"])
def api_webhook_receive():
    expected = _get_webhook_token()
    provided = request.headers.get("X-API-Key") or request.args.get("token") or ""
    if provided != expected:
        return jsonify({"error": "Clé API invalide (header X-API-Key ou ?token= requis)."}), 401

    data = request.get_json(force=True, silent=True) or {}
    if isinstance(data, list):
        records = data
        source_name = "Webhook API"
        tag = "Webhook"
    elif isinstance(data, dict) and isinstance(data.get("contacts"), list):
        records = data["contacts"]
        source_name = (data.get("source") or "Webhook API").strip()
        tag = (data.get("tag") or "Webhook").strip()
    elif isinstance(data, dict) and data.get("email"):
        records = [data]
        source_name = (data.get("source") or "Webhook API").strip()
        tag = (data.get("tag") or "Webhook").strip()
    else:
        return jsonify({"error": "JSON invalide : fournissez un objet contact avec 'email' ou une liste 'contacts'."}), 400

    summary = db.upsert_contacts(
        records=records,
        source_type="webhook",
        source_name=source_name,
        source_ref="webhook",
        extra_tag=tag,
    )
    return jsonify(summary), 201


# ---------------- SEQUENCES, MULTI-SMTP, DELIVERABILITY & TRACKING ----------------

TRANSPARENT_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


@app.route("/t/open/<tracking_id>.gif", methods=["GET"])
def track_email_open(tracking_id: str):
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE email_logs SET
                opened_count = opened_count + 1,
                opened_at = CASE WHEN opened_at = '' THEN ? ELSE opened_at END
            WHERE tracking_id = ?
            """,
            (now, tracking_id),
        )
    return Response(TRANSPARENT_GIF, mimetype="image/gif")


@app.route("/unsubscribe/<tracking_id>", methods=["GET"])
def public_unsubscribe(tracking_id: str):
    now = datetime.now().isoformat(timespec="seconds")
    email_addr = ""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT contact_id, email FROM email_logs WHERE tracking_id = ?",
            (tracking_id,),
        ).fetchone()
        if row:
            email_addr = row["email"]
            conn.execute(
                "UPDATE contacts SET status = 'unsubscribed', updated_at = ? WHERE id = ?",
                (now, row["contact_id"]),
            )
            conn.execute(
                "UPDATE sequence_enrollments SET state = 'stopped_unsubscribed' WHERE contact_id = ? AND state = 'active'",
                (row["contact_id"],),
            )
    return (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>Désinscription</title>"
        "<style>body{font-family:Arial,sans-serif;display:grid;place-items:center;min-height:100vh;background:#f8fafc;color:#0f172a;}"
        ".box{background:#fff;padding:32px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center;max-width:440px;}</style></head>"
        f"<body><div class='box'><h2>✓ Désinscription confirmée</h2><p>L'adresse <strong>{email_addr or 'votre email'}</strong> a bien été retirée de nos listes d'envoi.</p></div></body></html>"
    )


@app.route("/api/emailing/deliverability", methods=["POST"])
def api_deliverability():
    data = request.get_json(force=True) or {}
    try:
        res = campaigns.check_deliverability(data.get("domain") or "")
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/smtp-accounts", methods=["GET"])
def api_list_smtp_accounts():
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, label, host, port, username, from_name, use_tls, daily_limit, sent_today, last_sent_date, enabled FROM smtp_accounts ORDER BY id DESC"
            ).fetchall()
        ]
    return jsonify({"items": rows})


@app.route("/api/smtp-accounts", methods=["POST"])
def api_add_smtp_account():
    data = request.get_json(force=True) or {}
    label = (data.get("label") or data.get("username") or "").strip()
    host = (data.get("host") or "").strip()
    port = int(data.get("port") or 587)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    from_name = (data.get("from_name") or "").strip()
    daily_limit = max(5, min(2000, int(data.get("daily_limit") or 100)))
    if not host or not username or not password:
        return jsonify({"error": "Serveur SMTP, email et mot de passe requis."}), 400

    now = datetime.now().isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y-%m-%d")
    enc_password = db.encrypt_secret(password)
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO smtp_accounts (label, host, port, username, password, from_name, use_tls, daily_limit, sent_today, last_sent_date, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, 1, ?)
            """,
            (label, host, port, username, enc_password, from_name, daily_limit, today, now),
        )
    return jsonify({"ok": True}), 201


@app.route("/api/smtp-accounts/<int:aid>", methods=["DELETE"])
def api_delete_smtp_account(aid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM smtp_accounts WHERE id = ?", (aid,))
    return jsonify({"ok": True})


@app.route("/api/sequences", methods=["GET"])
def api_list_sequences():
    with db.get_conn() as conn:
        seqs = [dict(r) for r in conn.execute("SELECT * FROM sequences ORDER BY id DESC").fetchall()]
        for s in seqs:
            s["steps"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY step_order ASC",
                    (s["id"],),
                ).fetchall()
            ]
            counts = {
                r["state"]: r["cnt"]
                for r in conn.execute(
                    "SELECT state, COUNT(*) AS cnt FROM sequence_enrollments WHERE sequence_id = ? GROUP BY state",
                    (s["id"],),
                ).fetchall()
            }
            s["enrollments"] = {
                "active": counts.get("active", 0),
                "completed": counts.get("completed", 0),
                "replied": counts.get("stopped_replied", 0),
                "bounced": counts.get("stopped_bounced", 0),
                "stopped": counts.get("stopped_unsubscribed", 0) + counts.get("stopped_error", 0),
            }
    return jsonify({"items": seqs})


@app.route("/api/sequences", methods=["POST"])
def api_create_sequence():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    steps = data.get("steps") or []
    delay_min = max(5, int(data.get("delay_min_sec") or 20))
    delay_max = max(delay_min, int(data.get("delay_max_sec") or 45))
    include_unsub = 1 if data.get("include_unsubscribe", True) else 0
    if not name or not steps:
        return jsonify({"error": "Nom de séquence et au moins 1 étape requis."}), 400

    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sequences (name, status, delay_min_sec, delay_max_sec, include_unsubscribe, created_at)
            VALUES (?, 'active', ?, ?, ?, ?)
            """,
            (name, delay_min, delay_max, include_unsub, now),
        )
        sid = cur.lastrowid
        for idx, st in enumerate(steps, start=1):
            conn.execute(
                """
                INSERT INTO sequence_steps (sequence_id, step_order, delay_days, subject, body)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    idx,
                    max(0, int(st.get("delay_days") or 0)),
                    (st.get("subject") or "").strip(),
                    (st.get("body") or "").strip(),
                ),
            )
    return jsonify({"id": sid, "ok": True}), 201


@app.route("/api/sequences/<int:sid>/toggle", methods=["POST"])
def api_toggle_sequence(sid: int):
    with db.get_conn() as conn:
        row = conn.execute("SELECT status FROM sequences WHERE id = ?", (sid,)).fetchone()
        if not row:
            return jsonify({"error": "Séquence introuvable."}), 404
        new_st = "paused" if row["status"] == "active" else "active"
        conn.execute("UPDATE sequences SET status = ? WHERE id = ?", (new_st, sid))
    return jsonify({"status": new_st})


@app.route("/api/sequences/<int:sid>", methods=["DELETE"])
def api_delete_sequence(sid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sequence_enrollments WHERE sequence_id = ?", (sid,))
        conn.execute("DELETE FROM sequence_steps WHERE sequence_id = ?", (sid,))
        conn.execute("DELETE FROM sequences WHERE id = ?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/sequences/<int:sid>/enroll", methods=["POST"])
def api_enroll_sequence(sid: int):
    data = request.get_json(force=True) or {}
    tag = (data.get("tag") or "").strip()
    status = (data.get("status") or "").strip()
    email_type = (data.get("email_type") or "").strip()
    min_score = int(data.get("min_score") or 0)
    max_count = min(500, max(1, int(data.get("max_count") or 50)))

    contacts, _ = db.query_contacts(
        tag=tag, status=status, email_type=email_type, min_score=min_score, all_rows=True
    )
    eligible = [c for c in contacts if c["status"] != "unsubscribed"][:max_count]
    now = datetime.now().isoformat(timespec="seconds")
    enrolled = 0

    with db.get_conn() as conn:
        for c in eligible:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO sequence_enrollments (sequence_id, contact_id, current_step, next_send_at, state, enrolled_at)
                VALUES (?, ?, 0, ?, 'active', ?)
                """,
                (sid, c["id"], now, now),
            )
            if cur.rowcount:
                enrolled += 1

    return jsonify({"enrolled": enrolled, "eligible_found": len(eligible)})


@app.route("/api/sequences/run-now", methods=["POST"])
def api_run_sequences_now():
    res = campaigns.process_due_sequences(
        max_batch=15, apply_delay=False, base_url=request.host_url.rstrip("/")
    )
    return jsonify(res)


@app.route("/api/sequences/check-replies", methods=["POST"])
def api_check_sequence_replies():
    data = request.get_json(silent=True) or {}
    ws_id = _current_workspace_id()
    try:
        res = campaigns.check_replies_and_bounces(
            workspace_id=ws_id,
            host=(data.get("host") or "").strip(),
            port=int(data.get("port") or 993),
            username=(data.get("username") or "").strip(),
            password=data.get("password") or "",
        )
        _log_audit(
            "check_replies",
            f"Scan IMAP: {res['scanned']} lus, {res['replies_stopped']} réponses, {res['bounces_stopped']} bounces",
        )
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/emailing/logs", methods=["GET"])
def api_emailing_logs():
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT 150").fetchall()
        ]
        total_sent = conn.execute("SELECT COUNT(*) FROM email_logs WHERE status = 'sent'").fetchone()[0]
        total_opened = conn.execute("SELECT COUNT(*) FROM email_logs WHERE opened_count > 0").fetchone()[0]
        total_failed = conn.execute("SELECT COUNT(*) FROM email_logs WHERE status = 'failed'").fetchone()[0]
    open_rate = round((total_opened / total_sent) * 100, 1) if total_sent > 0 else 0.0
    return jsonify({
        "summary": {
            "sent": total_sent,
            "opened": total_opened,
            "failed": total_failed,
            "open_rate": open_rate,
        },
        "items": rows,
    })


# ---------------- CRM: PIPELINE KANBAN, 360 PROFILE, TASKS & SEGMENTS ----------------


@app.route("/api/crm/pipeline", methods=["GET"])
def api_crm_pipeline():
    q = request.args.get("q", "")
    tag = request.args.get("tag", "")
    min_score = int(request.args.get("min_score") or 0)
    limit = min(60, max(10, int(request.args.get("limit") or 25)))
    ws_id = _current_workspace_id()
    ws = db.get_workspace(ws_id)

    stages = ["new", "contacted", "followup", "client"]
    columns: Dict[str, Any] = {}
    pipeline_value_total = 0.0
    with db.get_conn() as conn:
        for st in stages:
            items, total = db.query_contacts(
                q=q, tag=tag, status=st, min_score=min_score,
                workspace_id=ws_id,
                sort_by="score_desc", page=1, per_page=limit
            )
            val_row = conn.execute(
                "SELECT COALESCE(SUM(deal_value), 0) AS val FROM contacts WHERE status = ? AND COALESCE(workspace_id, 1) = ?",
                (st, ws_id),
            ).fetchone()
            stage_val = float(val_row["val"] or 0) if val_row else 0.0
            pipeline_value_total += stage_val
            columns[st] = {"items": items, "total": total, "value_total": stage_val}
    return jsonify({
        "columns": columns,
        "pipeline_value_total": pipeline_value_total,
        "currency": ws.get("currency") or "FCFA",
    })


@app.route("/api/crm/pipeline/move", methods=["POST"])
def api_crm_pipeline_move():
    data = request.get_json(force=True) or {}
    cid = int(data.get("contact_id"))
    new_status = (data.get("status") or "").strip().lower()
    if new_status not in db.VALID_STATUSES:
        return jsonify({"error": "Statut invalide."}), 400

    now = datetime.now().isoformat(timespec="seconds")
    labels = {
        "new": "Nouveau", "contacted": "Contacté",
        "followup": "À relancer", "client": "Client",
        "unsubscribed": "Ne plus contacter",
    }
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, cid),
        )
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'status_change', ?, ?)",
            (cid, f"Déplacé vers l'étape : {labels.get(new_status, new_status)}", now),
        )
    return jsonify({"ok": True})


@app.route("/api/contacts/<int:cid>/profile", methods=["GET"])
def api_contact_profile(cid: int):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Contact introuvable."}), 404
        activities = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contact_activities WHERE contact_id = ? ORDER BY id DESC LIMIT 50",
                (cid,),
            ).fetchall()
        ]
        tasks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contact_tasks WHERE contact_id = ? ORDER BY done ASC, due_date ASC",
                (cid,),
            ).fetchall()
        ]
        emails = [
            dict(r)
            for r in conn.execute(
                "SELECT subject, status, opened_count, sent_at FROM email_logs WHERE contact_id = ? ORDER BY id DESC LIMIT 20",
                (cid,),
            ).fetchall()
        ]
    return jsonify({
        "contact": dict(row),
        "activities": activities,
        "tasks": tasks,
        "emails": emails,
    })


@app.route("/api/contacts/<int:cid>/activities", methods=["POST"])
def api_add_activity(cid: int):
    data = request.get_json(force=True) or {}
    kind = (data.get("kind") or "note").strip().lower()
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Contenu requis."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
            (cid, kind, content, now),
        )
        conn.execute("UPDATE contacts SET last_contacted_at = ?, updated_at = ? WHERE id = ?", (now, now, cid))
    return jsonify({"ok": True}), 201


@app.route("/api/crm/tasks", methods=["GET"])
def api_list_tasks():
    only_open = request.args.get("only_open", "").lower() in ("1", "true")
    where = "WHERE t.done = 0" if only_open else ""
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT t.*, c.email AS contact_email, c.full_name AS contact_name, c.company AS contact_company
                FROM contact_tasks t
                LEFT JOIN contacts c ON c.id = t.contact_id
                {where}
                ORDER BY t.done ASC, t.due_date ASC, t.id DESC
                LIMIT 150
                """
            ).fetchall()
        ]
    return jsonify({"items": rows})


@app.route("/api/crm/tasks", methods=["POST"])
def api_create_task():
    data = request.get_json(force=True) or {}
    cid = int(data.get("contact_id"))
    title = (data.get("title") or "").strip()
    due_date = (data.get("due_date") or datetime.now().strftime("%Y-%m-%d")).strip()
    if not title:
        return jsonify({"error": "Titre du rappel requis."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO contact_tasks (contact_id, title, due_date, done, created_at) VALUES (?, ?, ?, 0, ?)",
            (cid, title, due_date, now),
        )
    return jsonify({"ok": True}), 201


@app.route("/api/crm/tasks/<int:tid>/toggle", methods=["POST"])
def api_toggle_task(tid: int):
    with db.get_conn() as conn:
        conn.execute("UPDATE contact_tasks SET done = CASE WHEN done = 1 THEN 0 ELSE 1 END WHERE id = ?", (tid,))
    return jsonify({"ok": True})


@app.route("/api/crm/tasks/<int:tid>", methods=["DELETE"])
def api_delete_task(tid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM contact_tasks WHERE id = ?", (tid,))
    return jsonify({"ok": True})


@app.route("/api/crm/segments", methods=["GET"])
def api_list_segments():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM smart_segments ORDER BY id DESC").fetchall()]
    items = []
    for r in rows:
        try:
            flt = json.loads(r["filters_json"])
        except Exception:
            flt = {}
        _, count = db.query_contacts(
            q=flt.get("q", ""),
            tag=flt.get("tag", ""),
            source_type=flt.get("source_type", ""),
            domain=flt.get("domain", ""),
            status=flt.get("status", ""),
            email_type=flt.get("email_type", ""),
            mx_status=flt.get("mx_status", ""),
            country=flt.get("country", ""),
            min_score=int(flt.get("min_score") or 0),
            has_phone=bool(flt.get("has_phone", False)),
            page=1,
            per_page=1,
        )
        items.append({
            "id": r["id"],
            "name": r["name"],
            "filters": flt,
            "count": count,
            "created_at": r["created_at"],
        })
    return jsonify({"items": items})


@app.route("/api/crm/segments", methods=["POST"])
def api_create_segment():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    filters = data.get("filters") or {}
    if not name:
        return jsonify({"error": "Nom du segment requis."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO smart_segments (name, filters_json, created_at) VALUES (?, ?, ?)",
            (name, json.dumps(filters, ensure_ascii=False), now),
        )
    return jsonify({"ok": True}), 201


@app.route("/api/crm/segments/<int:sid>", methods=["DELETE"])
def api_delete_segment(sid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM smart_segments WHERE id = ?", (sid,))
    return jsonify({"ok": True})


# ---------------- SAAS WORKSPACES, WHITE-LABEL & ONBOARDING ----------------


@app.route("/api/workspaces", methods=["GET"])
def api_list_workspaces():
    ws_id = _current_workspace_id()
    return jsonify({
        "current": db.get_workspace(ws_id),
        "items": db.list_workspaces(),
    })


@app.route("/api/workspaces", methods=["POST"])
def api_create_workspace():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nom de l'espace client requis."}), 400
    ws = db.create_workspace(
        name=name,
        tagline=(data.get("tagline") or "CRM & Croissance").strip(),
        primary_color=(data.get("primary_color") or "#2563eb").strip(),
        currency=(data.get("currency") or "FCFA").strip(),
        plan=(data.get("plan") or "pro").strip(),
    )
    session["workspace_id"] = ws["id"]
    _log_audit("workspace_create", f"Nouvel espace SaaS créé : {ws['name']} ({ws['slug']})")
    return jsonify({"ok": True, "workspace": ws}), 201


@app.route("/api/workspaces/switch", methods=["POST"])
def api_switch_workspace():
    data = request.get_json(force=True) or {}
    try:
        ws_id = int(data.get("workspace_id") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "ID d'espace invalide."}), 400
    ws = db.get_workspace(ws_id)
    session["workspace_id"] = ws["id"]
    _log_audit("workspace_switch", f"Changement d'espace actif : {ws['name']}")
    return jsonify({"ok": True, "workspace": ws})


@app.route("/api/workspaces/current", methods=["PUT"])
def api_update_current_workspace():
    data = request.get_json(force=True) or {}
    ws_id = _current_workspace_id()
    ws = db.update_workspace(ws_id, data)
    _log_audit("workspace_update", f"Marque blanche / config mise à jour pour {ws['name']}")
    return jsonify({"ok": True, "workspace": ws})


# ---------------- ADMIN, USERS, SECURITY, AUDIT & GDPR ----------------


@app.route("/api/admin/status", methods=["GET"])
def api_admin_status():
    with db.get_conn() as conn:
        users = [
            dict(r)
            for r in conn.execute(
                "SELECT id, username, display_name, role, created_at FROM users ORDER BY id ASC"
            ).fetchall()
        ]
        audits = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 60"
            ).fetchall()
        ]
    return jsonify({
        "auth_enabled": _get_setting("auth_enabled", "0") == "1",
        "current_user": {
            "username": session.get("username") or "local",
            "display_name": session.get("display_name") or "Mode Local",
            "role": session.get("role") or "admin",
        },
        "users": users,
        "audits": audits,
    })


@app.route("/api/admin/users", methods=["POST"])
def api_admin_add_user():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip().lower()
    display_name = (data.get("display_name") or username).strip()
    role = (data.get("role") or "commercial").strip().lower()
    password = data.get("password") or ""
    if role not in ("admin", "commercial", "viewer"):
        role = "commercial"
    if not username or len(password) < 6:
        return jsonify({"error": "Identifiant requis et mot de passe de 6 caractères minimum."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, display_name, role, _hash_password(password), now),
            )
        _log_audit("user_create", f"Utilisateur créé : {username} ({role})")
        return jsonify({"ok": True}), 201
    except Exception:
        return jsonify({"error": "Cet identifiant existe déjà."}), 400


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
def api_admin_del_user(uid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    _log_audit("user_delete", f"Utilisateur #{uid} supprimé")
    return jsonify({"ok": True})


@app.route("/api/admin/auth-toggle", methods=["POST"])
def api_admin_auth_toggle():
    data = request.get_json(force=True) or {}
    enable = bool(data.get("enabled"))
    if enable:
        with db.get_conn() as conn:
            admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        if admins == 0:
            return jsonify({"error": "Créez d'abord au moins un compte Administrateur avant d'activer la protection."}), 400
    _set_setting("auth_enabled", "1" if enable else "0")
    _log_audit("auth_toggle", f"Protection par mot de passe : {'activée' if enable else 'désactivée'}")
    return jsonify({"auth_enabled": enable})


@app.route("/api/gdpr/export", methods=["GET"])
def api_gdpr_export():
    em = (request.args.get("email") or "").strip().lower()
    if not em:
        return jsonify({"error": "Adresse email requise."}), 400
    with db.get_conn() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE LOWER(email) = ?", (em,)).fetchone()
        if not c:
            return jsonify({"error": "Aucun contact trouvé pour cette adresse."}), 404
        cid = c["id"]
        activities = [dict(r) for r in conn.execute("SELECT * FROM contact_activities WHERE contact_id = ?", (cid,)).fetchall()]
        tasks = [dict(r) for r in conn.execute("SELECT * FROM contact_tasks WHERE contact_id = ?", (cid,)).fetchall()]
        emails = [dict(r) for r in conn.execute("SELECT subject, status, sent_at, opened_count FROM email_logs WHERE contact_id = ?", (cid,)).fetchall()]
    dossier = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "contact": dict(c),
        "activities": activities,
        "tasks": tasks,
        "email_history": emails,
    }
    _log_audit("gdpr_export", f"Export RGPD pour {em}")
    return Response(
        json.dumps(dossier, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="gdpr_{em}.json"'},
    )


@app.route("/api/gdpr/forget", methods=["POST"])
def api_gdpr_forget():
    data = request.get_json(force=True) or {}
    em = (data.get("email") or "").strip().lower()
    if not em:
        return jsonify({"error": "Adresse email requise."}), 400
    with db.get_conn() as conn:
        c = conn.execute("SELECT id FROM contacts WHERE LOWER(email) = ?", (em,)).fetchone()
        if not c:
            return jsonify({"error": "Aucun contact trouvé pour cette adresse."}), 404
        cid = c["id"]
        conn.execute("DELETE FROM contact_activities WHERE contact_id = ?", (cid,))
        conn.execute("DELETE FROM contact_tasks WHERE contact_id = ?", (cid,))
        conn.execute("DELETE FROM sequence_enrollments WHERE contact_id = ?", (cid,))
        conn.execute("UPDATE email_logs SET email = '[effacé RGPD]' WHERE contact_id = ?", (cid,))
        conn.execute("DELETE FROM contacts WHERE id = ?", (cid,))
    _log_audit("gdpr_forget", f"Droit à l'oubli appliqué pour {em}")
    return jsonify({"ok": True, "erased": em})


# ---------------- PACK CONVERSION: WHATSAPP, CLICK TRACKING & LEAD FORMS ----------------


@app.route("/api/whatsapp/templates", methods=["GET"])
def api_wa_templates():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM whatsapp_templates ORDER BY id DESC").fetchall()]
    return jsonify({"items": rows})


@app.route("/api/whatsapp/templates", methods=["POST"])
def api_wa_create_template():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    body = (data.get("body") or "").strip()
    if not name or not body:
        return jsonify({"error": "Nom et message requis."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO whatsapp_templates (name, body, created_at) VALUES (?, ?, ?)",
            (name, body, now),
        )
    return jsonify({"ok": True}), 201


@app.route("/api/whatsapp/templates/<int:tid>", methods=["DELETE"])
def api_wa_delete_template(tid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM whatsapp_templates WHERE id = ?", (tid,))
    return jsonify({"ok": True})


@app.route("/api/whatsapp/log", methods=["POST"])
def api_wa_log():
    data = request.get_json(force=True) or {}
    cid = int(data.get("contact_id"))
    preview = (data.get("message") or "Message WhatsApp envoyé").strip()[:220]
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'whatsapp', ?, ?)",
            (cid, f"💬 WhatsApp : {preview}", now),
        )
        conn.execute(
            """
            UPDATE contacts SET
                status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                last_contacted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, cid),
        )
    return jsonify({"ok": True})


# ---------------- INTERNAL WHATSAPP BRIDGE & INBOX ----------------

@app.route("/api/wa-bridge/webhook", methods=["POST"])
def api_wa_bridge_webhook():
    payload = request.get_json(force=True) or {}
    res = wa_client.process_webhook_event(payload)
    return jsonify(res)


@app.route("/api/wa/status", methods=["GET"])
def api_wa_status():
    st = wa_client.bridge_get("/status")
    with db.get_conn() as conn:
        db_contacts = conn.execute("SELECT COUNT(*) FROM whatsapp_contacts").fetchone()[0]
        db_messages = conn.execute("SELECT COUNT(*) FROM whatsapp_messages").fetchone()[0]
    st["db_counts"] = {"contacts": db_contacts, "messages": db_messages}
    return jsonify(st)


@app.route("/api/wa/connect", methods=["POST"])
def api_wa_connect():
    res = wa_client.bridge_post("/connect")
    return jsonify(res)


@app.route("/api/wa/logout", methods=["POST"])
def api_wa_logout():
    res = wa_client.bridge_post("/logout")
    return jsonify(res)


@app.route("/api/wa/sync", methods=["POST"])
def api_wa_sync():
    res = wa_client.bridge_post("/sync")
    return jsonify(res)


@app.route("/api/wa/import-contacts", methods=["POST"])
def api_wa_import_contacts():
    data = request.get_json(silent=True) or {}
    ws_id = int(data.get("workspace_id") or 1)
    only_with_msgs = bool(data.get("only_with_messages", False))
    res = wa_client.import_wa_contacts_to_crm(workspace_id=ws_id, only_with_messages=only_with_msgs)
    return jsonify(res)


@app.route("/api/wa/conversations", methods=["GET"])
def api_wa_conversations():
    q = (request.args.get("q") or "").strip()
    items = wa_client.list_conversations(search=q)
    return jsonify({"items": items})


@app.route("/api/wa/messages", methods=["GET"])
def api_wa_messages():
    phone = (request.args.get("phone") or "").strip()
    jid = (request.args.get("jid") or "").strip()
    cid_raw = request.args.get("contact_id")
    cid = int(cid_raw) if cid_raw and cid_raw.isdigit() else None
    items = wa_client.list_messages(phone=phone, jid=jid, contact_id=cid)
    return jsonify({"items": items})


@app.route("/api/wa/send", methods=["POST"])
def api_wa_send():
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    phone = (data.get("phone") or "").strip()
    jid = (data.get("jid") or "").strip()
    cid = data.get("contact_id")
    if cid is not None and not phone and not jid:
        with db.get_conn() as conn:
            row = conn.execute("SELECT phone FROM contacts WHERE id = ?", (int(cid),)).fetchone()
            if row:
                phone = row["phone"]
    if not text or (not phone and not jid):
        return jsonify({"error": "Numéro de téléphone et message requis."}), 400

    res = wa_client.bridge_post("/send", {"phone": phone, "jid": jid, "text": text})
    if not res.get("ok"):
        return jsonify({"error": res.get("error") or "Échec de l'envoi WhatsApp."}), 400
    return jsonify(res)


# ---------------- WHATSAPP BROADCASTS & AUTO-WELCOME ----------------

@app.route("/api/wa/auto-welcome", methods=["GET", "POST"])
def api_wa_auto_welcome():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        cfg = wa_broadcast.save_auto_welcome_config(
            bool(data.get("enabled", True)),
            data.get("template") or "",
        )
        return jsonify(cfg)
    return jsonify(wa_broadcast.get_auto_welcome_config())


@app.route("/api/wa/broadcasts", methods=["GET"])
def api_wa_broadcasts_list():
    return jsonify({"items": wa_broadcast.list_broadcasts()})


@app.route("/api/wa/broadcasts/preview", methods=["POST"])
def api_wa_broadcasts_preview():
    filters = request.get_json(force=True) or {}
    aud = wa_broadcast.query_audience(filters)
    return jsonify({"count": len(aud), "sample": aud[:8]})


@app.route("/api/wa/broadcasts", methods=["POST"])
def api_wa_broadcasts_create():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    tpl = (data.get("message_tpl") or "").strip()
    if not tpl:
        return jsonify({"error": "Message de campagne requis."}), 400
    res = wa_broadcast.create_and_start_broadcast(
        name=name,
        message_tpl=tpl,
        filters=data.get("filters") or {},
        delay_min=int(data.get("delay_min_sec") or 18),
        delay_max=int(data.get("delay_max_sec") or 40),
    )
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


@app.route("/api/wa/broadcasts/<int:bid>/stop", methods=["POST"])
def api_wa_broadcasts_stop(bid: int):
    return jsonify(wa_broadcast.stop_broadcast(bid))


# ---------------- FACEBOOK (MESSENGER + LEAD ADS) & TIKTOK ----------------

@app.route("/api/social/config", methods=["GET"])
def api_social_config_get():
    base = request.host_url.rstrip("/")
    fb = social_hub.get_channel_config("facebook")
    tt = social_hub.get_channel_config("tiktok")
    fb["webhook_url"] = f"{base}/webhooks/facebook"
    tt["webhook_url"] = f"{base}/webhooks/tiktok"
    return jsonify({"facebook": fb, "tiktok": tt})


@app.route("/api/social/config/<platform>", methods=["POST"])
def api_social_config_save(platform: str):
    if platform not in ("facebook", "tiktok"):
        return jsonify({"error": "Plateforme invalide"}), 400
    data = request.get_json(force=True) or {}
    cfg = social_hub.save_channel_config(platform, data)
    return jsonify({"ok": True, "config": cfg})


@app.route("/webhooks/facebook", methods=["GET", "POST"])
def webhook_facebook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        cfg = social_hub.get_channel_config("facebook")
        if mode == "subscribe" and token and token == cfg.get("verify_token"):
            return Response(challenge, status=200, mimetype="text/plain")
        return Response("Forbidden", status=403)
    payload = request.get_json(silent=True) or {}
    return jsonify(social_hub.process_facebook_webhook(payload))


@app.route("/webhooks/tiktok", methods=["GET", "POST"])
def webhook_tiktok():
    if request.method == "GET":
        challenge = request.args.get("challenge") or request.args.get("hub.challenge") or "ok"
        return Response(challenge, status=200, mimetype="text/plain")
    payload = request.get_json(silent=True) or {}
    return jsonify(social_hub.process_tiktok_webhook(payload))


@app.route("/api/social/conversations", methods=["GET"])
def api_social_conversations():
    platform = (request.args.get("platform") or "").strip()
    return jsonify({"items": social_hub.list_social_conversations(platform)})


@app.route("/api/social/messages", methods=["GET"])
def api_social_messages():
    platform = (request.args.get("platform") or "facebook").strip()
    sender_id = (request.args.get("sender_id") or "").strip()
    return jsonify({"items": social_hub.list_social_messages(platform, sender_id)})


@app.route("/api/social/send", methods=["POST"])
def api_social_send():
    data = request.get_json(force=True) or {}
    sender_id = (data.get("sender_id") or "").strip()
    text = (data.get("text") or "").strip()
    if not sender_id or not text:
        return jsonify({"error": "Destinataire et message requis."}), 400
    res = social_hub.send_facebook_messenger_reply(sender_id, text)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


@app.route("/api/social/convert", methods=["POST"])
def api_social_convert():
    data = request.get_json(force=True) or {}
    res = social_hub.convert_social_sender_to_crm(
        platform=data.get("platform") or "facebook",
        sender_id=data.get("sender_id") or "",
        phone=data.get("phone") or "",
        email=data.get("email") or "",
        full_name=data.get("full_name") or "",
    )
    return jsonify(res)


@app.route("/api/social/import-csv/<platform>", methods=["POST"])
def api_social_import_csv(platform: str):
    if platform not in ("facebook", "tiktok"):
        return jsonify({"error": "Plateforme invalide"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Fichier CSV requis."}), 400
    tag = (request.form.get("tag") or "").strip()
    ws_id = int(request.form.get("workspace_id") or 1)
    res = social_hub.import_social_leads_csv(platform, f.read(), tag=tag, workspace_id=ws_id)
    return jsonify(res)


# ---------------- STUDENT PAYMENTS & RECEIPTS (TMONEY / FLOOZ / CASH) ----------------

@app.route("/api/payments", methods=["GET"])
def api_payments_list():
    q = (request.args.get("q") or "").strip()
    ws_id = int(request.args.get("workspace_id") or 1)
    return jsonify(payments_hub.list_payments(search=q, workspace_id=ws_id))


@app.route("/api/payments", methods=["POST"])
def api_payments_create():
    data = request.get_json(force=True) or {}
    res = payments_hub.create_payment(data)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


@app.route("/api/payments/<int:pid>", methods=["DELETE"])
def api_payments_delete(pid: int):
    return jsonify(payments_hub.delete_payment(pid))


@app.route("/receipt/<int:pid>", methods=["GET"])
def view_payment_receipt(pid: int):
    data = payments_hub.get_receipt_data(pid)
    if not data:
        return "Reçu introuvable", 404
    return render_template("receipt.html", **data)


@app.route("/api/payments/<int:pid>/send-whatsapp", methods=["POST"])
def api_payment_send_wa(pid: int):
    url = f"{request.host_url.rstrip('/')}/receipt/{pid}"
    res = payments_hub.send_receipt_via_whatsapp(pid, url)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


# ---------------- LOCAL RADAR (OPENSTREETMAP FREE + GOOGLE MAPS) ----------------

@app.route("/api/radar/config", methods=["GET", "POST"])
def api_radar_config():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        local_radar.save_gmaps_key(data.get("google_maps_api_key") or "")
        return jsonify({"ok": True, "has_gmaps_key": bool(local_radar.get_gmaps_key())})
    return jsonify({"has_gmaps_key": bool(local_radar.get_gmaps_key())})


@app.route("/api/radar/search", methods=["POST"])
def api_radar_search():
    data = request.get_json(force=True) or {}
    keyword = (data.get("keyword") or "hotel").strip()
    city = (data.get("city") or "Lomé, Togo").strip()
    engine = (data.get("engine") or "osm").strip()
    enrich = bool(data.get("enrich_emails", False))
    res = local_radar.search_local_businesses(keyword=keyword, city=city, engine=engine, enrich_emails=enrich)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


@app.route("/api/radar/import", methods=["POST"])
def api_radar_import():
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    tag = (data.get("tag") or "Radar B2B").strip()
    ws_id = int(data.get("workspace_id") or 1)
    res = local_radar.import_radar_businesses(items, tag=tag, workspace_id=ws_id)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


# ---------------- AI SALES ASSISTANT (WHATSAPP & CRM) ----------------

@app.route("/api/ai/config", methods=["GET", "POST"])
def api_ai_config():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        cfg = ai_assistant.save_ai_config(
            data.get("provider") or "builtin",
            data.get("api_key") or "",
            data.get("business_context") or "",
        )
        return jsonify({"ok": True, "config": cfg})
    return jsonify(ai_assistant.get_ai_config(include_secret=False))


@app.route("/api/ai/suggest-wa", methods=["POST"])
def api_ai_suggest_wa():
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    jid = (data.get("jid") or "").strip()
    cid = data.get("contact_id")
    name = (data.get("contact_name") or "").strip()
    goal = (data.get("goal") or "repondre").strip()
    msgs = wa_client.list_messages(phone=phone, jid=jid, contact_id=int(cid) if cid else None)
    booking_url = f"{request.host_url.rstrip('/')}/rdv"
    res = ai_assistant.suggest_reply(contact_name=name, messages=msgs, goal=goal, booking_url=booking_url)
    return jsonify(res)


@app.route("/api/ai/summarize-wa", methods=["POST"])
def api_ai_summarize_wa():
    data = request.get_json(force=True) or {}
    cid = data.get("contact_id")
    phone = (data.get("phone") or "").strip()
    jid = (data.get("jid") or "").strip()
    name = (data.get("contact_name") or "").strip()
    if not cid:
        return jsonify({"error": "Contact non lié au CRM."}), 400
    msgs = wa_client.list_messages(phone=phone, jid=jid, contact_id=int(cid))
    res = ai_assistant.summarize_chat_to_crm(int(cid), name, msgs)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


# ---------------- PUBLIC APPOINTMENT BOOKING (/rdv) & AGENDA ----------------

@app.route("/rdv", methods=["GET", "POST"])
def public_booking_page():
    cfg = booking_hub.get_booking_settings()
    today = datetime.now().strftime("%Y-%m-%d")
    if request.method == "POST":
        res = booking_hub.create_appointment({
            "full_name": request.form.get("full_name"),
            "phone": request.form.get("phone"),
            "email": request.form.get("email"),
            "topic": request.form.get("topic"),
            "mode": request.form.get("mode"),
            "appointment_date": request.form.get("appointment_date"),
            "appointment_time": request.form.get("appointment_time"),
            "notes": request.form.get("notes"),
        })
        if not res.get("ok"):
            return render_template(
                "booking.html",
                workspace=cfg["workspace"],
                location=cfg["location"],
                slots=cfg["slots"],
                topics=cfg["topics"],
                today=today,
                default_date=request.form.get("appointment_date") or today,
                confirmed=False,
                error=res.get("error"),
            )
        return render_template(
            "booking.html",
            workspace=cfg["workspace"],
            location=cfg["location"],
            slots=cfg["slots"],
            topics=cfg["topics"],
            today=today,
            default_date=today,
            confirmed=True,
            booking_ref=res["booking_ref"],
            full_name=request.form.get("full_name"),
            appt_date=request.form.get("appointment_date"),
            appt_time=request.form.get("appointment_time"),
            mode=request.form.get("mode"),
        )
    return render_template(
        "booking.html",
        workspace=cfg["workspace"],
        location=cfg["location"],
        slots=cfg["slots"],
        topics=cfg["topics"],
        today=today,
        default_date=today,
        confirmed=False,
        error=None,
    )


@app.route("/api/booking/slots", methods=["GET"])
def api_booking_slots():
    d = (request.args.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()
    return jsonify({"date": d, "booked": booking_hub.get_booked_slots(d)})


@app.route("/api/booking/settings", methods=["GET", "POST"])
def api_booking_settings():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        cfg = booking_hub.save_booking_settings(
            data.get("slots") or "",
            data.get("location") or "",
            data.get("topics") or "",
        )
        cfg["public_url"] = f"{request.host_url.rstrip('/')}/rdv"
        return jsonify(cfg)
    cfg = booking_hub.get_booking_settings()
    cfg["public_url"] = f"{request.host_url.rstrip('/')}/rdv"
    return jsonify(cfg)


@app.route("/api/appointments", methods=["GET"])
def api_appointments_list():
    st = (request.args.get("status") or "").strip()
    return jsonify({"items": booking_hub.list_appointments(status=st)})


@app.route("/api/appointments/<int:aid>/status", methods=["POST"])
def api_appointment_status(aid: int):
    data = request.get_json(force=True) or {}
    res = booking_hub.update_appointment_status(aid, data.get("status") or "confirmed")
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


@app.route("/api/appointments/<int:aid>/remind-wa", methods=["POST"])
def api_appointment_remind_wa(aid: int):
    res = booking_hub.send_appointment_reminder_wa(aid)
    if not res.get("ok"):
        return jsonify({"error": res.get("error")}), 400
    return jsonify(res)


@app.route("/api/call/log", methods=["POST"])
def api_call_log():
    data = request.get_json(force=True) or {}
    cid = int(data.get("contact_id"))
    phone = (data.get("phone") or "").strip()[:60]
    note = (data.get("note") or "").strip()[:220]
    content = f"Appel téléphonique vers {phone}" if phone else "Appel téléphonique lancé"
    if note:
        content = f"{content} — {note}"
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'call', ?, ?)",
            (cid, content, now),
        )
        conn.execute(
            """
            UPDATE contacts SET
                status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                last_contacted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, cid),
        )
    return jsonify({"ok": True})


@app.route("/api/links", methods=["GET"])
def api_list_links():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM tracked_links ORDER BY id DESC").fetchall()]
    base = request.host_url.rstrip("/")
    for r in rows:
        r["short_url"] = f"{base}/l/{r['code']}"
    return jsonify({"items": rows})


@app.route("/api/links", methods=["POST"])
def api_create_link():
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    target_url = (data.get("target_url") or "").strip()
    if not title or not target_url.startswith(("http://", "https://")):
        return jsonify({"error": "Titre et URL valide (http:// ou https://) requis."}), 400
    code = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:7]
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO tracked_links (code, title, target_url, clicks_count, created_at) VALUES (?, ?, ?, 0, ?)",
            (code, title, target_url, now),
        )
    return jsonify({"ok": True, "code": code, "short_url": f"{request.host_url.rstrip('/')}/l/{code}"}), 201


@app.route("/api/links/<int:lid>", methods=["DELETE"])
def api_delete_link(lid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM click_events WHERE link_id = ?", (lid,))
        conn.execute("DELETE FROM tracked_links WHERE id = ?", (lid,))
    return jsonify({"ok": True})


@app.route("/l/<code>", methods=["GET"])
def public_tracked_link(code: str):
    cid_raw = request.args.get("cid")
    cid = int(cid_raw) if cid_raw and cid_raw.isdigit() else None
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        link = conn.execute("SELECT * FROM tracked_links WHERE code = ?", (code,)).fetchone()
        if not link:
            return "Lien introuvable.", 404
        conn.execute("UPDATE tracked_links SET clicks_count = clicks_count + 1 WHERE id = ?", (link["id"],))
        conn.execute(
            "INSERT INTO click_events (link_id, contact_id, clicked_at) VALUES (?, ?, ?)",
            (link["id"], cid, now),
        )
        if cid:
            conn.execute(
                """
                UPDATE contacts SET
                    lead_score = MIN(100, lead_score + 30),
                    status = CASE WHEN status IN ('new', 'contacted') THEN 'followup' ELSE status END,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, cid),
            )
            conn.execute(
                "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'click', ?, ?)",
                (cid, f"🔥 A cliqué sur le lien tracké : {link['title']}", now),
            )
    return redirect(link["target_url"])


@app.route("/api/conversion/hot-signals", methods=["GET"])
def api_hot_signals():
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT c.id, c.email, c.full_name, c.phone, c.company, c.lead_score, c.status,
                       a.kind AS signal_kind, a.content AS signal_text, a.created_at AS signal_at
                FROM contact_activities a
                JOIN contacts c ON c.id = a.contact_id
                WHERE a.kind IN ('click', 'form_submit', 'whatsapp')
                ORDER BY a.id DESC
                LIMIT 40
                """
            ).fetchall()
        ]
        # Also include top high-score leads with phone if no signals yet
        top_ready = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, email, full_name, phone, company, country, lead_score, status
                FROM contacts
                WHERE phone != '' AND status != 'unsubscribed'
                ORDER BY lead_score DESC, updated_at DESC
                LIMIT 40
                """
            ).fetchall()
        ]
    return jsonify({"signals": rows, "whatsapp_ready": top_ready})


@app.route("/api/forms", methods=["GET"])
def api_list_forms():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM lead_forms ORDER BY id DESC").fetchall()]
    base = request.host_url.rstrip("/")
    for r in rows:
        r["public_url"] = f"{base}/f/{r['slug']}"
        try:
            r["config"] = json.loads(r.get("config_json") or "{}")
        except Exception:
            r["config"] = {}
    return jsonify({"items": rows})


@app.route("/api/forms", methods=["POST"])
def api_create_form():
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    raw_slug = (data.get("slug") or title).strip().lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in raw_slug).strip("-")
    slug = "-".join(p for p in slug.split("-") if p)[:45] or secrets.token_hex(4)
    form_kind = data.get("form_kind") if data.get("form_kind") in ("simple", "training") else "simple"
    config = data.get("config") or {}
    if not title:
        return jsonify({"error": "Titre du formulaire requis."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO lead_forms (slug, title, subtitle, cta_text, tag, redirect_url, form_kind, config_json, submissions_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    slug,
                    title,
                    (data.get("subtitle") or "").strip(),
                    (data.get("cta_text") or "✅ Valider mon inscription").strip(),
                    (data.get("tag") or "Inscrit Formation").strip(),
                    (data.get("redirect_url") or "").strip(),
                    form_kind,
                    json.dumps(config, ensure_ascii=False),
                    now,
                ),
            )
        return jsonify({"ok": True, "slug": slug, "public_url": f"{request.host_url.rstrip('/')}/f/{slug}"}), 201
    except Exception:
        return jsonify({"error": "Ce slug existe déjà. Choisissez un autre nom."}), 400


@app.route("/api/forms/<int:fid>", methods=["PUT"])
def api_update_form(fid: int):
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Titre requis."}), 400
    config = data.get("config") or {}
    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE lead_forms SET
                title = ?, subtitle = ?, cta_text = ?, tag = ?,
                redirect_url = ?, form_kind = ?, config_json = ?
            WHERE id = ?
            """,
            (
                title,
                (data.get("subtitle") or "").strip(),
                (data.get("cta_text") or "✅ Valider mon inscription").strip(),
                (data.get("tag") or "Inscrit Formation").strip(),
                (data.get("redirect_url") or "").strip(),
                data.get("form_kind") if data.get("form_kind") in ("simple", "training") else "training",
                json.dumps(config, ensure_ascii=False),
                fid,
            ),
        )
    return jsonify({"ok": True})


@app.route("/api/forms/<int:fid>/duplicate", methods=["POST"])
def api_duplicate_form(fid: int):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM lead_forms WHERE id = ?", (fid,)).fetchone()
        if not row:
            return jsonify({"error": "Formulaire introuvable."}), 404
        r = dict(row)
        new_slug = f"{r['slug']}-{secrets.token_hex(2)}"
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO lead_forms (slug, title, subtitle, cta_text, tag, redirect_url, form_kind, config_json, submissions_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                new_slug,
                f"{r['title']} (Copie)",
                r["subtitle"],
                r["cta_text"],
                r["tag"],
                r["redirect_url"],
                r["form_kind"],
                r["config_json"],
                now,
            ),
        )
    return jsonify({"ok": True, "slug": new_slug}), 201


@app.route("/api/forms/<int:fid>/submissions", methods=["GET"])
def api_form_submissions(fid: int):
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM form_submissions WHERE form_id = ? ORDER BY id DESC LIMIT 300",
                (fid,),
            ).fetchall()
        ]
    return jsonify({"items": rows})


@app.route("/api/forms/<int:fid>/submissions/export", methods=["GET"])
def api_export_form_submissions(fid: int):
    with db.get_conn() as conn:
        form = conn.execute("SELECT slug, title FROM lead_forms WHERE id = ?", (fid,)).fetchone()
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM form_submissions WHERE form_id = ? ORDER BY id DESC",
                (fid,),
            ).fetchall()
        ]
    slug = form["slug"] if form else f"form_{fid}"
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow([
        "N° Dossier", "Nom complet", "Email", "Téléphone",
        "Session", "Niveau", "Formule", "Mode règlement", "Réf. TX", "Date inscription"
    ])
    for r in rows:
        writer.writerow([
            r["dossier_num"], r["full_name"], r["email"], r["phone"],
            r["session_choice"], r["level_choice"], r["payment_plan"],
            r["payment_method"], r["payment_ref"], r["submitted_at"],
        ])
    return Response(
        ("\ufeff" + out.getvalue()).encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="inscrits_{slug}.csv"'},
    )


@app.route("/api/forms/<int:fid>", methods=["DELETE"])
def api_delete_form(fid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM form_submissions WHERE form_id = ?", (fid,))
        conn.execute("DELETE FROM lead_forms WHERE id = ?", (fid,))
    return jsonify({"ok": True})


@app.route("/f/<slug>", methods=["GET", "POST"])
def public_capture_form(slug: str):
    with db.get_conn() as conn:
        form = conn.execute("SELECT * FROM lead_forms WHERE slug = ?", (slug,)).fetchone()
    if not form:
        return "Formulaire introuvable.", 404
    form = dict(form)
    try:
        cfg = json.loads(form.get("config_json") or "{}")
    except Exception:
        cfg = {}

    is_training = (form.get("form_kind") == "training")
    today = datetime.now().strftime("%d/%m/%Y")
    prefix = cfg.get("dossier_prefix") or "INS"
    dossier_num = f"{prefix}-{datetime.now().strftime('%m%d')}-{secrets.token_hex(2).upper()}"

    if request.method == "POST":
        email_addr = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        if not email_addr or "@" not in email_addr:
            tpl = "training_form.html" if is_training else "capture_form.html"
            return render_template(tpl, form=form, cfg=cfg, dossier_num=dossier_num, today=today, submitted=False, error="Adresse email invalide.")

        if is_training:
            civilite = (request.form.get("civilite") or "").strip()
            last_name = (request.form.get("last_name") or "").strip().upper()
            first_name = (request.form.get("first_name") or "").strip()
            full_name = f"{civilite} {first_name} {last_name}".strip()
            dossier_used = (request.form.get("dossier_num") or dossier_num).strip()
            session_choice = (request.form.get("session_choice") or "").strip()
            level_choice = (request.form.get("level_choice") or "").strip()
            payment_plan = (request.form.get("payment_plan") or "").strip()
            payment_method = (request.form.get("payment_method") or "").strip()
            payment_ref = (request.form.get("payment_ref") or "").strip()
            profession = (request.form.get("profession") or "").strip()
            adresse = (request.form.get("adresse") or "").strip()
            objectif = (request.form.get("objectif") or "").strip()

            extra_data = dict(request.form)
            notes_summary = f"Dossier {dossier_used} | {session_choice} | Niveau: {level_choice} | {payment_plan} ({payment_method}) | Adresse: {adresse}"

            db.upsert_contacts(
                records=[{
                    "email": email_addr,
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": f"{first_name} {last_name}".strip(),
                    "phone": phone,
                    "role": profession,
                    "company": adresse,
                    "notes": notes_summary,
                    "status": "followup",
                }],
                source_type="form",
                source_name=form["title"],
                source_ref=form["slug"],
                extra_tag=form["tag"],
            )
            now = datetime.now().isoformat(timespec="seconds")
            with db.get_conn() as conn:
                conn.execute("UPDATE lead_forms SET submissions_count = submissions_count + 1 WHERE id = ?", (form["id"],))
                c = conn.execute("SELECT id FROM contacts WHERE email = ?", (email_addr,)).fetchone()
                cid = c["id"] if c else None
                if cid:
                    conn.execute(
                        "UPDATE contacts SET lead_score = MAX(lead_score, 95), status = 'followup', updated_at = ? WHERE id = ?",
                        (now, cid),
                    )
                    conn.execute(
                        "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'form_submit', ?, ?)",
                        (cid, f"🎓 Inscription {dossier_used} : {form['title']} ({session_choice}, {level_choice}, {payment_plan})", now),
                    )
                conn.execute(
                    """
                    INSERT INTO form_submissions (
                        form_id, contact_id, dossier_num, full_name, email, phone,
                        session_choice, level_choice, payment_plan, payment_method, payment_ref,
                        data_json, submitted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        form["id"], cid, dossier_used, full_name, email_addr, phone,
                        session_choice, level_choice, payment_plan, payment_method, payment_ref,
                        json.dumps(extra_data, ensure_ascii=False), now,
                    ),
                )

            if phone:
                wa_broadcast.trigger_auto_welcome_wa(
                    contact_id=cid,
                    phone=phone,
                    full_name=full_name,
                    source_label=form["title"],
                    extra_vars={"dossier": dossier_used, "formation": form["title"]},
                )

            wa_num = "".join(ch for ch in (cfg.get("whatsapp_number") or "") if ch.isdigit())
            wa_url = ""
            if wa_num:
                import urllib.parse as _up
                wa_msg = f"Bonjour, je viens de valider mon inscription ({dossier_used}) pour {form['title']}. Nom : {full_name}, Session : {session_choice}, Niveau : {level_choice}, Formule : {payment_plan}."
                wa_url = f"https://wa.me/{wa_num}?text={_up.quote(wa_msg)}"

            return render_template(
                "training_form.html",
                form=form,
                cfg=cfg,
                dossier_num=dossier_used,
                today=today,
                submitted=True,
                name=full_name,
                wa_confirm_url=wa_url,
            )

        # Simple capture form
        full_name = (request.form.get("full_name") or "").strip()
        company = (request.form.get("company") or "").strip()
        db.upsert_contacts(
            records=[{
                "email": email_addr,
                "full_name": full_name,
                "phone": phone,
                "company": company,
                "status": "followup",
            }],
            source_type="form",
            source_name=form["title"],
            source_ref=form["slug"],
            extra_tag=form["tag"],
        )
        now = datetime.now().isoformat(timespec="seconds")
        with db.get_conn() as conn:
            conn.execute("UPDATE lead_forms SET submissions_count = submissions_count + 1 WHERE id = ?", (form["id"],))
            c = conn.execute("SELECT id FROM contacts WHERE email = ?", (email_addr,)).fetchone()
            cid = c["id"] if c else None
            if cid:
                conn.execute(
                    "UPDATE contacts SET lead_score = MAX(lead_score, 90), status = 'followup', updated_at = ? WHERE id = ?",
                    (now, cid),
                )
                conn.execute(
                    "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'form_submit', ?, ?)",
                    (cid, f"🔥 Inscrit via la page : {form['title']}", now),
                )
            conn.execute(
                """
                INSERT INTO form_submissions (form_id, contact_id, dossier_num, full_name, email, phone, data_json, submitted_at)
                VALUES (?, ?, '', ?, ?, ?, ?, ?)
                """,
                (form["id"], cid, full_name, email_addr, phone, json.dumps(dict(request.form), ensure_ascii=False), now),
            )
        if phone:
            wa_broadcast.trigger_auto_welcome_wa(
                contact_id=cid,
                phone=phone,
                full_name=full_name,
                source_label=form["title"],
            )
        if form.get("redirect_url"):
            return redirect(form["redirect_url"])
        return render_template("capture_form.html", form=form, submitted=True, name=full_name or email_addr)

    if is_training:
        return render_template("training_form.html", form=form, cfg=cfg, dossier_num=dossier_num, today=today, submitted=False, error=None)
    return render_template("capture_form.html", form=form, submitted=False, error=None)


# ---------------- B2B PROSPECTING, SALES COPY & CLOSING LINKS ----------------


@app.route("/api/prospecting/crawl", methods=["POST"])
def api_prospecting_crawl():
    data = request.get_json(force=True) or {}
    raw_urls = data.get("urls") or []
    if isinstance(raw_urls, str):
        raw_urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    if not raw_urls:
        return jsonify({"error": "Fournissez au moins une URL ou un domaine d'entreprise."}), 400
    res = prospecting.crawl_company_websites(raw_urls, max_sites=25)
    return jsonify(res)


@app.route("/api/prospecting/import", methods=["POST"])
def api_prospecting_import():
    data = request.get_json(force=True) or {}
    prospects = data.get("prospects") or []
    tag = (data.get("tag") or "Prospection B2B").strip()
    if not prospects:
        return jsonify({"error": "Aucun prospect à importer."}), 400
    summary = db.upsert_contacts(
        records=prospects,
        source_type="b2b_web",
        source_name="Prospection Web B2B",
        source_ref="b2b_web",
        extra_tag=tag,
    )
    enrichment.run_auto_enrichment()
    return jsonify(summary), 201


@app.route("/api/closing/settings", methods=["GET"])
def api_get_closing():
    return jsonify({
        "business_name": _get_setting("business_name", "Mon Centre de Formation en Anglais"),
        "booking_url": _get_setting("booking_url", ""),
        "payment_url": _get_setting("payment_url", ""),
        "payment_webhook_url": f"{request.host_url.rstrip('/')}/api/webhooks/payment",
        "webhook_token": _get_webhook_token(),
    })


@app.route("/api/closing/settings", methods=["POST"])
def api_save_closing():
    data = request.get_json(force=True) or {}
    _set_setting("business_name", (data.get("business_name") or "").strip())
    _set_setting("booking_url", (data.get("booking_url") or "").strip())
    _set_setting("payment_url", (data.get("payment_url") or "").strip())
    return jsonify({"ok": True})


@app.route("/api/closing/generate-copy", methods=["POST"])
def api_generate_copy():
    data = request.get_json(force=True) or {}
    res = prospecting.generate_sales_copy(
        offer_type=data.get("offer_type") or "b2b_english",
        channel=data.get("channel") or "email",
        tone=data.get("tone") or "pro",
        business_name=_get_setting("business_name", "Mon Centre de Formation en Anglais"),
        booking_url=_get_setting("booking_url", ""),
        payment_url=_get_setting("payment_url", ""),
    )
    return jsonify(res)


@app.route("/api/webhooks/payment", methods=["POST"])
def api_webhook_payment():
    expected = _get_webhook_token()
    provided = request.headers.get("X-API-Key") or request.args.get("token") or ""
    if provided != expected:
        return jsonify({"error": "Clé API invalide."}), 401
    data = request.get_json(force=True, silent=True) or {}
    em = (data.get("email") or "").strip().lower()
    amount = str(data.get("amount") or "").strip()
    currency = str(data.get("currency") or "FCFA").strip()
    if not em or "@" not in em:
        return jsonify({"error": "Email client requis."}), 400

    db.upsert_contacts(
        records=[{
            "email": em,
            "full_name": data.get("full_name") or "",
            "phone": data.get("phone") or "",
            "status": "client",
        }],
        source_type="payment",
        source_name="Paiement confirmé",
        extra_tag="Client Payant",
    )
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        c = conn.execute("SELECT id FROM contacts WHERE email = ?", (em,)).fetchone()
        if c:
            conn.execute(
                "UPDATE contacts SET status = 'client', lead_score = 100, updated_at = ? WHERE id = ?",
                (now, c["id"]),
            )
            conn.execute(
                "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'payment', ?, ?)",
                (c["id"], f"💳 Paiement reçu : {amount} {currency}".strip(), now),
            )
    _log_audit("payment_received", f"Client {em} ({amount} {currency})")
    return jsonify({"ok": True, "client": em}), 201


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
