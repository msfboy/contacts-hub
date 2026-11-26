import email
import imaplib
import os
import re
import subprocess
from email.header import decode_header
from email.utils import getaddresses
from typing import Any, Dict, List, Optional

import parsers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WIN_OCR_SCRIPT = os.path.join(BASE_DIR, "win_ocr.ps1")

PHONE_REGEX = re.compile(
    r"(?:\+|00)\d{1,3}[\s.\-]?(?:\(?\d{1,4}\)?[\s.\-]?){2,5}\d{2,4}|\b0[1-9](?:[\s.\-]?\d{2}){4}\b"
)

ROLE_HINTS = (
    "ceo", "cto", "cfo", "dg", "pdg", "directeur", "directrice", "director",
    "manager", "fondateur", "founder", "président", "president", "responsable",
    "consultant", "ingénieur", "engineer", "commercial", "avocat", "gérant",
    "chef de", "head of", "vp ", "lead ",
)

def _run_windows_ocr(image_path: str) -> List[str]:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            WIN_OCR_SCRIPT,
            "-ImagePath",
            os.path.abspath(image_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Erreur OCR Windows").strip()
        raise RuntimeError(err)
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(enc or "utf-8", errors="ignore"))
        else:
            decoded.append(str(chunk))
    return "".join(decoded).strip()


def _parse_signature_block(text: str) -> Dict[str, str]:
    """Extract phone, role, and company hints from the bottom lines of an email."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    tail = lines[-15:] if len(lines) > 15 else lines
    joined = "\n".join(tail)

    phone = ""
    pm = PHONE_REGEX.search(joined)
    if pm:
        phone = pm.group(0).strip()

    role = ""
    for ln in tail:
        low = ln.lower()
        if len(ln) <= 75 and any(h in low for h in ROLE_HINTS):
            role = ln
            break

    return {"phone": phone, "role": role}


def _extract_plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="ignore")
    return ""


def fetch_imap_contacts(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = "INBOX",
    limit: int = 150,
    parse_signatures: bool = True,
) -> Dict[str, Any]:
    host = (host or "").strip()
    username = (username or "").strip()
    folder = (folder or "INBOX").strip()
    if not host or not username or not password:
        raise ValueError("Hôte IMAP, email et mot de passe requis.")

    import net_guard
    net_guard.validate_public_host(host)
    port_num = int(port or 993)
    if port_num not in (993, 143, 1993):
        raise ValueError("Port IMAP non autorisé (ports acceptés : 993, 143).")
    if not re.fullmatch(r"[a-zA-Z0-9_./ -]{1,128}", folder):
        raise ValueError("Nom de dossier IMAP invalide.")

    imap = imaplib.IMAP4_SSL(host, port_num, timeout=15)
    try:
        imap.login(username, password)
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            raise ValueError(f"Impossible d'ouvrir le dossier IMAP : {folder}")

        typ, data = imap.search(None, "ALL")
        if typ != "OK":
            return {"emails_scanned": 0, "contacts": []}

        msg_ids = data[0].split()
        target_ids = list(reversed(msg_ids[-max(1, min(500, limit)) :]))
        contacts_map: Dict[str, Dict[str, str]] = {}

        for mid in target_ids:
            fetch_spec = "(RFC822)" if parse_signatures else "(BODY.PEEK[HEADER.FIELDS (FROM TO CC REPLY-TO)])"
            t, msg_data = imap.fetch(mid, fetch_spec)
            if t != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # Parse signature for the primary From sender
            sig_info: Dict[str, str] = {}
            if parse_signatures:
                body = _extract_plain_body(msg)
                if body:
                    sig_info = _parse_signature_block(body)

            from_addrs = getaddresses([_decode_mime_header(msg.get("From"))])
            other_headers = [
                _decode_mime_header(msg.get("To")),
                _decode_mime_header(msg.get("Cc")),
                _decode_mime_header(msg.get("Reply-To")),
            ]
            other_addrs = getaddresses(other_headers)

            for idx_group, group in enumerate((from_addrs, other_addrs)):
                for raw_name, raw_email in group:
                    em = (raw_email or "").strip().lower()
                    if not parsers._is_valid_email(em) or em == username.lower():
                        continue
                    clean_name = raw_name.strip(" \"'<>")
                    rec = contacts_map.get(em) or {
                        "email": em,
                        "full_name": "",
                        "phone": "",
                        "role": "",
                        "company": "",
                    }
                    if not rec["full_name"] and clean_name and "@" not in clean_name:
                        rec["full_name"] = clean_name
                    if idx_group == 0 and sig_info:
                        if not rec["phone"] and sig_info.get("phone"):
                            rec["phone"] = sig_info["phone"]
                        if not rec["role"] and sig_info.get("role"):
                            rec["role"] = sig_info["role"]
                    contacts_map[em] = rec

        return {
            "emails_scanned": len(target_ids),
            "contacts": list(contacts_map.values()),
        }
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------- OCR (IMAGES & BUSINESS CARDS) ----------------


def extract_contacts_from_image(image_path: str) -> Dict[str, Any]:
    if not os.path.isfile(image_path):
        raise FileNotFoundError("Image introuvable.")

    lines = _run_windows_ocr(image_path)
    full_text = "\n".join(lines)

    contacts = parsers._extract_emails_from_text(full_text)
    phone_m = PHONE_REGEX.search(full_text)
    phone = phone_m.group(0).strip() if phone_m else ""

    # Heuristic: first non-email, non-phone, short line is often Name or Company
    non_meta = [
        ln for ln in lines
        if "@" not in ln and not PHONE_REGEX.search(ln) and 3 <= len(ln) <= 55
    ]
    guessed_name = non_meta[0] if non_meta else ""
    guessed_role = ""
    guessed_company = ""
    for ln in non_meta[1:5]:
        if not guessed_role and any(h in ln.lower() for h in ROLE_HINTS):
            guessed_role = ln
        elif not guessed_company:
            guessed_company = ln

    for c in contacts:
        if not c.get("full_name") and guessed_name:
            c["full_name"] = guessed_name
        if phone:
            c["phone"] = phone
        if guessed_role:
            c["role"] = guessed_role
        if guessed_company:
            c["company"] = guessed_company

    return {
        "raw_text": full_text,
        "lines": lines,
        "contacts": contacts,
    }
