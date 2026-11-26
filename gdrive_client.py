import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

import db

GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/userinfo.email"
)


# ---------------- OAUTH2 (SAAS GOOGLE DRIVE API V3) ----------------

def _oauth_client_config() -> Tuple[str, str]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    return cid, csec


def is_oauth_configured() -> bool:
    cid, csec = _oauth_client_config()
    return bool(cid and csec)


def _load_oauth_tokens() -> Dict[str, Any]:
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'gdrive_oauth_tokens'").fetchone()
            if row and row["value"]:
                raw = db.decrypt_secret(row["value"])
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
    except Exception:
        pass
    return {}


def _save_oauth_tokens(tokens: Dict[str, Any]) -> None:
    enc = db.encrypt_secret(json.dumps(tokens, ensure_ascii=False))
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('gdrive_oauth_tokens', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (enc,),
        )


def disconnect_oauth() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'gdrive_oauth_tokens'")


def build_oauth_url(redirect_uri: str, state: str) -> str:
    cid, _ = _oauth_client_config()
    if not cid:
        raise ValueError("Variables GOOGLE_CLIENT_ID et GOOGLE_CLIENT_SECRET non configurées.")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_oauth_code(code: str, redirect_uri: str) -> Tuple[bool, str]:
    cid, csec = _oauth_client_config()
    if not cid or not csec:
        return False, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET manquants."
    try:
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        data = r.json()
        if r.status_code != 200 or not data.get("access_token"):
            return False, data.get("error_description") or data.get("error") or f"Erreur OAuth HTTP {r.status_code}"

        access_token = data["access_token"]
        expires_in = int(data.get("expires_in") or 3600)
        existing = _load_oauth_tokens()
        refresh_token = data.get("refresh_token") or existing.get("refresh_token") or ""

        email = existing.get("email") or ""
        try:
            u = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=8,
            )
            if u.status_code == 200:
                email = (u.json().get("email") or email).strip()
        except Exception:
            pass

        _save_oauth_tokens({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": int(time.time()) + expires_in - 60,
            "email": email,
        })
        return True, email
    except Exception as exc:
        return False, str(exc)


def _get_valid_access_token() -> Optional[str]:
    toks = _load_oauth_tokens()
    if not toks:
        return None
    if toks.get("access_token") and int(toks.get("expires_at") or 0) > int(time.time()):
        return toks["access_token"]

    refresh_token = toks.get("refresh_token") or ""
    cid, csec = _oauth_client_config()
    if not refresh_token or not cid or not csec:
        return None
    try:
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cid,
                "client_secret": csec,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        new_at = data.get("access_token")
        if not new_at:
            return None
        toks["access_token"] = new_at
        toks["expires_at"] = int(time.time()) + int(data.get("expires_in") or 3600) - 60
        _save_oauth_tokens(toks)
        return new_at
    except Exception:
        return None


# ---------------- LEGACY LOCAL CLI FALLBACK ----------------

def _run_gdrive(
    args: List[str],
    timeout: int = 45,
    account: Optional[str] = None,
) -> Tuple[bool, Any, str]:
    exe = shutil.which("gdrive")
    if not exe:
        return False, None, "Google Drive non connecté. Connectez votre compte Google ci-dessus ou importez par lien."
    cmd = [exe] + args
    env = os.environ.copy()
    if account and account.strip():
        env["AGY_ACCOUNT"] = account.strip()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except Exception as exc:
        return False, None, str(exc)

    if proc.returncode != 0:
        return False, None, (proc.stderr or proc.stdout or "Erreur Google Drive").strip()

    out = (proc.stdout or "").strip()
    if "--json" in args:
        try:
            return True, json.loads(out) if out else [], ""
        except json.JSONDecodeError:
            return True, out, ""
    return True, out, ""


def check_status(account: Optional[str] = None) -> Dict[str, Any]:
    oauth_cfg = is_oauth_configured()
    toks = _load_oauth_tokens()
    if toks and _get_valid_access_token():
        return {
            "connected": True,
            "mode": "oauth",
            "oauth_configured": oauth_cfg,
            "email": toks.get("email") or "Compte Google",
        }
    if shutil.which("gdrive"):
        ok, data, _ = _run_gdrive(["--version"], timeout=8, account=account)
        if ok:
            return {
                "connected": True,
                "mode": "cli",
                "oauth_configured": oauth_cfg,
                "version": str(data).strip(),
            }
    return {
        "connected": False,
        "mode": "none",
        "oauth_configured": oauth_cfg,
        "error": "Aucun compte Google Drive connecté.",
    }


def _normalize_items(raw: Any) -> List[Dict[str, Any]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("files") or raw.get("items") or []
    if not isinstance(items, list):
        return []

    result = []
    for f in items:
        if not isinstance(f, dict):
            continue
        mime = f.get("mimeType") or f.get("mime_type") or f.get("type") or ""
        name = f.get("name") or ""
        is_folder = mime == GOOGLE_FOLDER_MIME or mime == "folder"
        result.append(
            {
                "id": f.get("id") or "",
                "name": name,
                "mimeType": mime,
                "isFolder": is_folder,
                "size": f.get("size") or "",
                "modifiedTime": f.get("modifiedTime") or f.get("modified_time") or "",
            }
        )
    result.sort(key=lambda x: (not x["isFolder"], x["name"].lower()))
    return result


def _validate_drive_id(raw_id: Optional[str], label: str = "ID") -> Optional[str]:
    if not raw_id:
        return None
    s = str(raw_id).strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{5,128}", s) or s.startswith("-"):
        raise ValueError(f"{label} Google Drive invalide.")
    return s


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "")).strip(" .")
    if not cleaned or cleaned in (".", "..") or cleaned.upper() in ("CON", "PRN", "AUX", "NUL"):
        return "drive_file"
    return cleaned[:150]


def _contained_path(dest_dir: str, fname: str) -> str:
    abs_dest = os.path.abspath(dest_dir)
    os.makedirs(abs_dest, exist_ok=True)
    p = os.path.abspath(os.path.join(abs_dest, fname))
    if os.path.commonpath([abs_dest, p]) != abs_dest:
        raise ValueError("Chemin de destination invalide.")
    return p


def list_files(
    folder_id: Optional[str] = None,
    query: str = "",
    file_type: str = "",
    max_results: int = 80,
    account: Optional[str] = None,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    try:
        folder_id = _validate_drive_id(folder_id, "folder_id")
    except ValueError as exc:
        return False, [], str(exc)
    if file_type and file_type not in ("folder", "spreadsheet", "document", "pdf", "csv"):
        file_type = ""
    max_results = max(1, min(200, int(max_results or 80)))

    # 1. Google Drive REST API v3 (OAuth2)
    token = _get_valid_access_token()
    if token:
        clauses = ["trashed = false"]
        if folder_id:
            clauses.append(f"'{folder_id}' in parents")
        elif not query and not file_type:
            clauses.append("'root' in parents")
        if query:
            safe_q = query.replace("\\", "").replace("'", "\\'")[:120]
            clauses.append(f"name contains '{safe_q}'")
        mime_map = {
            "folder": GOOGLE_FOLDER_MIME,
            "spreadsheet": GOOGLE_SHEET_MIME,
            "document": GOOGLE_DOC_MIME,
            "pdf": "application/pdf",
            "csv": "text/csv",
        }
        if file_type in mime_map:
            clauses.append(f"mimeType = '{mime_map[file_type]}'")
        try:
            r = requests.get(
                "https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "q": " and ".join(clauses),
                    "pageSize": max_results,
                    "fields": "files(id,name,mimeType,size,modifiedTime)",
                    "orderBy": "folder,modifiedTime desc",
                },
                timeout=20,
            )
            if r.status_code == 200:
                return True, _normalize_items(r.json()), ""
            return False, [], f"Erreur Google Drive API (HTTP {r.status_code}). Reconnectez votre compte."
        except Exception as exc:
            return False, [], f"Erreur réseau Google Drive : {exc}"

    # 2. Local CLI fallback
    if query or file_type:
        args = ["--json", "readonly", "search", "--max", str(max_results)]
        if query:
            args += ["--name-contains", query.lstrip("-")[:120]]
        if folder_id:
            args += ["--parent-id", folder_id]
        if file_type:
            args += ["--type", file_type]
    else:
        args = ["--json", "readonly", "ls"]
        if folder_id:
            args.append(folder_id)
        args += ["--max", str(max_results)]

    ok, data, err = _run_gdrive(args, account=account)
    if not ok:
        return False, [], err
    return True, _normalize_items(data), ""


def download_file(
    file_id: str,
    name: str,
    mime_type: str,
    dest_dir: str,
    account: Optional[str] = None,
) -> Tuple[bool, str, str]:
    try:
        file_id = _validate_drive_id(file_id, "file_id") or ""
    except ValueError as exc:
        return False, "", str(exc)
    if not file_id:
        return False, "", "file_id requis."
    base = _safe_filename(name)

    # 1. Google Drive REST API v3 (OAuth2)
    token = _get_valid_access_token()
    if token:
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if mime_type == GOOGLE_SHEET_MIME or mime_type == "spreadsheet":
                out_path = _contained_path(dest_dir, f"{base}.xlsx")
                r = requests.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                    headers=headers,
                    params={"mimeType": XLSX_MIME},
                    timeout=60,
                )
            elif mime_type == GOOGLE_DOC_MIME or mime_type == "document":
                out_path = _contained_path(dest_dir, f"{base}.txt")
                r = requests.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                    headers=headers,
                    params={"mimeType": "text/plain"},
                    timeout=60,
                )
            else:
                out_path = _contained_path(dest_dir, base)
                r = requests.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}",
                    headers=headers,
                    params={"alt": "media"},
                    timeout=60,
                )
            if r.status_code != 200:
                return False, "", f"Échec téléchargement Drive (HTTP {r.status_code})."
            with open(out_path, "wb") as f:
                f.write(r.content)
            return True, out_path, ""
        except Exception as exc:
            return False, "", str(exc)

    # 2. Local CLI fallback
    if mime_type == GOOGLE_SHEET_MIME or mime_type == "spreadsheet":
        out_path = _contained_path(dest_dir, f"{base}.xlsx")
        ok, _, err = _run_gdrive(["readonly", "export", file_id, out_path, "--format", "xlsx"], timeout=90, account=account)
        return ok, out_path, err

    if mime_type == GOOGLE_DOC_MIME or mime_type == "document":
        out_path = _contained_path(dest_dir, f"{base}.txt")
        ok, _, err = _run_gdrive(["readonly", "export", file_id, out_path, "--format", "txt"], timeout=90, account=account)
        return ok, out_path, err

    out_path = _contained_path(dest_dir, base)
    ok, _, err = _run_gdrive(["readonly", "download", file_id, "--out", out_path], timeout=90, account=account)
    return ok, out_path, err


def download_from_link(url: str, dest_dir: str) -> Tuple[bool, str, str, str]:
    """Download a Google Sheets or Google Drive shared link (works public OR private if OAuth2 connected)."""
    raw = (url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ("docs.google.com", "drive.google.com"):
        return False, "", "", "Lien invalide : collez une URL docs.google.com ou drive.google.com."

    m_sheet = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]{10,128})", parsed.path)
    m_doc = re.search(r"/document/d/([a-zA-Z0-9_-]{10,128})", parsed.path)
    m_file = re.search(r"/file/d/([a-zA-Z0-9_-]{10,128})", parsed.path)
    qs_id = (urllib.parse.parse_qs(parsed.query).get("id") or [""])[0]
    gid = (urllib.parse.parse_qs(parsed.query).get("gid") or [""])[0]

    file_id = ""
    kind = "file"
    if m_sheet:
        file_id = m_sheet.group(1)
        kind = "sheet"
    elif m_doc:
        file_id = m_doc.group(1)
        kind = "doc"
    elif m_file:
        file_id = m_file.group(1)
        kind = "file"
    elif qs_id and re.fullmatch(r"[a-zA-Z0-9_-]{10,128}", qs_id):
        file_id = qs_id
        kind = "file"
    else:
        return False, "", "", "Impossible d'extraire l'identifiant du fichier depuis ce lien Google."

    # If OAuth2 is connected, use Drive API directly (supports private files!)
    if _get_valid_access_token():
        mime = GOOGLE_SHEET_MIME if kind == "sheet" else (GOOGLE_DOC_MIME if kind == "doc" else "")
        fname = f"google_{kind}_{file_id[:8]}"
        ok, path, err = download_file(file_id, fname, mime, dest_dir)
        return ok, path, fname, err

    # Otherwise fetch public export URL
    try:
        if kind == "sheet":
            exp_url = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"
            if gid and gid.isdigit():
                exp_url += f"&gid={gid}"
            fname = f"gsheet_{file_id[:8]}.csv"
        elif kind == "doc":
            exp_url = f"https://docs.google.com/document/d/{file_id}/export?format=txt"
            fname = f"gdoc_{file_id[:8]}.txt"
        else:
            exp_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            fname = f"gdrive_{file_id[:8]}.csv"

        r = requests.get(exp_url, headers={"User-Agent": "ContactsHub/1.0"}, timeout=30)
        if r.status_code != 200:
            return False, "", "", (
                "Accès refusé (HTTP %s). Partagez le fichier en « Tous les utilisateurs disposant du lien (Lecteur) » "
                "ou connectez votre compte Google Drive." % r.status_code
            )
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype and b"<html" in r.content[:400].lower():
            return False, "", "", (
                "Ce fichier Google est privé. Partagez-le en « Tous les utilisateurs disposant du lien » "
                "ou connectez votre compte Google Drive."
            )
        # Detect extension from Content-Disposition if available
        cd = r.headers.get("Content-Disposition") or ""
        m_fn = re.search(r'filename="?([^";]+)"?', cd)
        if m_fn:
            fname = _safe_filename(m_fn.group(1))
        out_path = _contained_path(dest_dir, fname)
        with open(out_path, "wb") as f:
            f.write(r.content)
        return True, out_path, fname, ""
    except Exception as exc:
        return False, "", "", str(exc)


def upload_backup(
    local_file_path: str,
    account: Optional[str] = None,
) -> Tuple[bool, str, str]:
    token = _get_valid_access_token()
    if token:
        try:
            fname = os.path.basename(local_file_path)
            meta = json.dumps({"name": fname}).encode("utf-8")
            with open(local_file_path, "rb") as f:
                raw_db = f.read()
            files = {
                "metadata": ("metadata.json", meta, "application/json; charset=UTF-8"),
                "file": (fname, raw_db, "application/octet-stream"),
            }
            r = requests.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                timeout=90,
            )
            if r.status_code in (200, 201):
                fid = r.json().get("id", "")
                return True, f"Sauvegarde envoyée sur Google Drive (ID: {fid})", ""
            return False, "", f"Échec upload Drive (HTTP {r.status_code})."
        except Exception as exc:
            return False, "", str(exc)

    ok, out, err = _run_gdrive(["mutate", "upload", local_file_path], timeout=90, account=account)
    return ok, str(out), err
