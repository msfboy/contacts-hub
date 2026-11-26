import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _run_gdrive(
    args: List[str],
    timeout: int = 45,
    account: Optional[str] = None,
) -> Tuple[bool, Any, str]:
    exe = shutil.which("gdrive") or "gdrive"
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
    except FileNotFoundError:
        return False, None, "CLI gdrive introuvable dans le PATH."
    except subprocess.TimeoutExpired:
        return False, None, "Délai dépassé lors de la communication avec Google Drive."
    except Exception as exc:
        return False, None, str(exc)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Erreur Google Drive").strip()
        return False, None, err

    out = (proc.stdout or "").strip()
    if "--json" in args:
        try:
            return True, json.loads(out) if out else [], ""
        except json.JSONDecodeError:
            return True, out, ""
    return True, out, ""


def check_status(account: Optional[str] = None) -> Dict[str, Any]:
    ok, data, err = _run_gdrive(["--version"], timeout=10, account=account)
    if not ok:
        return {"connected": False, "error": err}
    return {"connected": True, "version": str(data).strip()}


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


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "")).strip(" .")
    if not cleaned or cleaned in (".", "..") or cleaned.upper() in ("CON", "PRN", "AUX", "NUL"):
        return "drive_file"
    return cleaned[:150]


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
    os.makedirs(dest_dir, exist_ok=True)
    base = _safe_filename(name)
    abs_dest = os.path.abspath(dest_dir)

    def _contained(fname: str) -> str:
        p = os.path.abspath(os.path.join(abs_dest, fname))
        if os.path.commonpath([abs_dest, p]) != abs_dest:
            raise ValueError("Chemin de destination invalide.")
        return p

    if mime_type == GOOGLE_SHEET_MIME or mime_type == "spreadsheet":
        out_path = _contained(f"{base}.xlsx")
        ok, _, err = _run_gdrive(
            ["readonly", "export", file_id, out_path, "--format", "xlsx"],
            timeout=90,
            account=account,
        )
        return ok, out_path, err

    if mime_type == GOOGLE_DOC_MIME or mime_type == "document":
        out_path = _contained(f"{base}.txt")
        ok, _, err = _run_gdrive(
            ["readonly", "export", file_id, out_path, "--format", "txt"],
            timeout=90,
            account=account,
        )
        return ok, out_path, err

    out_path = _contained(base)
    ok, _, err = _run_gdrive(
        ["readonly", "download", file_id, "--out", out_path],
        timeout=90,
        account=account,
    )
    return ok, out_path, err


def upload_backup(
    local_file_path: str,
    account: Optional[str] = None,
) -> Tuple[bool, str, str]:
    ok, out, err = _run_gdrive(
        ["mutate", "upload", local_file_path],
        timeout=90,
        account=account,
    )
    return ok, str(out), err
