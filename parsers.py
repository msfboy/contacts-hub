import csv
import io
import json
import os
import re
from typing import Any, Dict, List

import openpyxl
from docx import Document
from pypdf import PdfReader

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)
NAMED_EMAIL_REGEX = re.compile(
    r'["\']?([^<"\'\n,;]{1,80})["\']?\s*<\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\s*>'
)

INVALID_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp",
    "css", "js", "map", "woff", "woff2", "ttf", "eot", "mp4", "mp3", "pdf",
}

SUPPORTED_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".vcf",
    ".json",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".docx",
    ".pdf",
    ".eml",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

HEADER_MAP = {
    "email": {
        "email", "e-mail", "mail", "courriel", "adresse email", "adresse e-mail",
        "adresse mail", "email address", "e-mail address", "email 1 - value",
        "e-mail 1 - value", "mail professionnel", "email pro", "adresse électronique",
    },
    "first_name": {
        "first name", "firstname", "first_name", "prénom", "prenom", "given name",
    },
    "last_name": {
        "last name", "lastname", "last_name", "nom", "nom de famille", "family name", "surname",
    },
    "full_name": {
        "name", "full name", "fullname", "full_name", "nom complet", "nom et prénom",
        "nom et prenom", "prénom et nom", "prenom et nom", "contact", "display name",
        "nom / prénom", "participant", "client",
    },
    "phone": {
        "phone", "telephone", "téléphone", "tel", "tél", "mobile", "gsm",
        "whatsapp", "numéro", "numero", "phone 1 - value", "numéro de téléphone",
        "numero de telephone", "portable",
    },
    "company": {
        "company", "entreprise", "société", "societe", "organization", "organisation",
        "organization name", "org", "structure", "établissement", "etablissement",
    },
    "role": {
        "role", "rôle", "poste", "fonction", "title", "job title", "titre",
        "organization title", "profession", "métier", "metier", "position",
    },
    "tags": {
        "tags", "tag", "group", "groupe", "groupes", "groups", "category",
        "catégorie", "categorie", "label", "labels", "segment", "liste",
    },
    "notes": {
        "notes", "note", "commentaire", "commentaires", "remarque", "remarques",
        "description", "observations",
    },
}


def _norm_header(h: Any) -> str:
    if h is None:
        return ""
    return re.sub(r"\s+", " ", str(h).strip().lower())


def _map_columns(headers: List[Any]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    normed = [_norm_header(h) for h in headers]
    for field, aliases in HEADER_MAP.items():
        for idx, h in enumerate(normed):
            if h in aliases and field not in mapping:
                mapping[field] = idx
                break
    # Partial fallback for email column
    if "email" not in mapping:
        for idx, h in enumerate(normed):
            if "mail" in h or "courriel" in h:
                mapping["email"] = idx
                break
    return mapping


def _is_valid_email(email: str) -> bool:
    em = email.strip().lower()
    if "@" not in em or len(em) > 254:
        return False
    domain = em.rsplit("@", 1)[1]
    if "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[1]
    if tld in INVALID_TLDS:
        return False
    return True


def _extract_emails_from_text(text: str) -> List[Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    for name, email in NAMED_EMAIL_REGEX.findall(text):
        em = email.strip().lower()
        if not _is_valid_email(em):
            continue
        clean_name = name.strip(" \t\r\n-:;,|\"'")
        found[em] = {"email": em, "full_name": clean_name}

    for email in EMAIL_REGEX.findall(text):
        em = email.strip().lower()
        if not _is_valid_email(em):
            continue
        if em not in found:
            found[em] = {"email": em}

    return list(found.values())


def _rows_to_contacts(rows: List[List[Any]]) -> List[Dict[str, str]]:
    if not rows:
        return []

    # Find best header row in first 5 non-empty rows
    header_idx = None
    col_map: Dict[str, int] = {}
    for i, row in enumerate(rows[:10]):
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue
        candidate = _map_columns(row)
        if "email" in candidate:
            header_idx = i
            col_map = candidate
            break

    contacts: List[Dict[str, str]] = []
    if header_idx is not None:
        for row in rows[header_idx + 1 :]:
            if not row:
                continue
            email_idx = col_map["email"]
            raw_email = str(row[email_idx]).strip() if email_idx < len(row) and row[email_idx] is not None else ""
            emails = EMAIL_REGEX.findall(raw_email)
            if not emails:
                # Try scanning whole row if email column empty
                row_text = " ".join(str(c) for c in row if c is not None)
                emails = EMAIL_REGEX.findall(row_text)
            if not emails:
                continue

            def _get(field: str) -> str:
                idx = col_map.get(field)
                if idx is not None and idx < len(row) and row[idx] is not None:
                    return str(row[idx]).strip()
                return ""

            for em in emails:
                if not _is_valid_email(em):
                    continue
                contacts.append(
                    {
                        "email": em.lower(),
                        "first_name": _get("first_name"),
                        "last_name": _get("last_name"),
                        "full_name": _get("full_name"),
                        "phone": _get("phone"),
                        "company": _get("company"),
                        "role": _get("role"),
                        "tags": _get("tags"),
                        "notes": _get("notes"),
                    }
                )
    else:
        # No clear header: scan every row for emails
        for row in rows:
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if not cells:
                continue
            row_text = " | ".join(cells)
            emails = [e for e in EMAIL_REGEX.findall(row_text) if _is_valid_email(e)]
            if not emails:
                continue
            non_email_cells = [c for c in cells if not EMAIL_REGEX.search(c)]
            guessed_name = non_email_cells[0] if non_email_cells and len(non_email_cells[0]) <= 60 else ""
            for em in emails:
                contacts.append({"email": em.lower(), "full_name": guessed_name})

    return contacts


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


MAX_PARSED_ROWS = 50000
MAX_PDF_PAGES = 200
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 Mo anti-Zip-Bomb


def _check_zip_bomb(path: str) -> None:
    import zipfile
    if not zipfile.is_zipfile(path):
        return
    total_size = 0
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            total_size += info.file_size
            if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise ValueError("Archive trop volumineuse après décompression (protection anti-Zip-Bomb).")


def _parse_csv_text(text: str) -> List[Dict[str, str]]:
    # Handle LinkedIn Connections.csv preamble ("Notes: ...")
    lines = text.splitlines()
    for idx, ln in enumerate(lines[:6]):
        if "first name" in ln.lower() and "email address" in ln.lower():
            text = "\n".join(lines[idx:])
            break

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";" if sample.count(";") > sample.count(",") else ","

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = []
    for r in reader:
        if any(c.strip() for c in r):
            rows.append(r)
            if len(rows) >= MAX_PARSED_ROWS:
                break
    return _rows_to_contacts(rows)


def _parse_xlsx(path: str) -> List[Dict[str, str]]:
    _check_zip_bomb(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    all_contacts: List[Dict[str, str]] = []
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) >= MAX_PARSED_ROWS:
                    break
            all_contacts.extend(_rows_to_contacts(rows))
    finally:
        wb.close()
    return all_contacts


def _parse_vcf(text: str) -> List[Dict[str, str]]:
    # Unfold folded vCard lines
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    cards = re.split(r"(?i)BEGIN:VCARD", unfolded)
    contacts: List[Dict[str, str]] = []

    for card in cards[1:]:
        full_name = ""
        first_name = ""
        last_name = ""
        phone = ""
        company = ""
        role = ""
        notes = ""
        emails: List[str] = []

        for line in card.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key_part, val = line.split(":", 1)
            key = key_part.split(";", 1)[0].upper()
            val = val.strip()

            if key == "FN" and not full_name:
                full_name = val
            elif key == "N":
                parts = val.split(";")
                if len(parts) >= 2:
                    last_name = parts[0].strip()
                    first_name = parts[1].strip()
            elif key == "EMAIL":
                for em in EMAIL_REGEX.findall(val):
                    emails.append(em.lower())
            elif key == "TEL" and not phone:
                phone = val
            elif key == "ORG" and not company:
                company = val.replace(";", " ").strip()
            elif key == "TITLE" and not role:
                role = val
            elif key == "NOTE" and not notes:
                notes = val

        for em in emails:
            contacts.append(
                {
                    "email": em,
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": full_name,
                    "phone": phone,
                    "company": company,
                    "role": role,
                    "notes": notes,
                }
            )
    return contacts


def _parse_json(text: str) -> List[Dict[str, str]]:
    data = json.loads(text)
    contacts: List[Dict[str, str]] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif isinstance(obj, dict):
            # Check if this dict looks like a contact
            norm_dict = {_norm_header(k): v for k, v in obj.items()}
            found_email = ""
            for k, v in norm_dict.items():
                if isinstance(v, str) and ("mail" in k or "courriel" in k):
                    ms = EMAIL_REGEX.findall(v)
                    if ms:
                        found_email = ms[0].lower()
                        break
            if found_email:
                def _pick(field: str) -> str:
                    for alias in HEADER_MAP[field]:
                        if alias in norm_dict and norm_dict[alias] is not None:
                            return str(norm_dict[alias]).strip()
                    return ""

                contacts.append(
                    {
                        "email": found_email,
                        "first_name": _pick("first_name"),
                        "last_name": _pick("last_name"),
                        "full_name": _pick("full_name"),
                        "phone": _pick("phone"),
                        "company": _pick("company"),
                        "role": _pick("role"),
                        "tags": _pick("tags"),
                        "notes": _pick("notes"),
                    }
                )
            else:
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        _walk(v)
                    elif isinstance(v, str):
                        for em in EMAIL_REGEX.findall(v):
                            contacts.append({"email": em.lower()})

    _walk(data)
    return contacts


def _parse_docx(path: str) -> List[Dict[str, str]]:
    doc = Document(path)
    contacts: List[Dict[str, str]] = []
    # Tables first
    for table in doc.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        contacts.extend(_rows_to_contacts(rows))
    # Paragraphs
    para_text = "\n".join(p.text for p in doc.paragraphs if p.text)
    contacts.extend(_extract_emails_from_text(para_text))
    return contacts


def _parse_pdf(path: str) -> List[Dict[str, str]]:
    reader = PdfReader(path)
    chunks: List[str] = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            txt = page.extract_text()
            if txt:
                chunks.append(txt)
        except Exception:
            continue
    return _extract_emails_from_text("\n".join(chunks))


def parse_file(path: str) -> List[Dict[str, str]]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        return _parse_xlsx(path)
    if ext == ".xls":
        import pandas as pd

        xls = pd.read_excel(path, sheet_name=None, header=None)
        contacts: List[Dict[str, str]] = []
        for _, df in xls.items():
            rows = df.head(MAX_PARSED_ROWS).where(df.notna(), None).values.tolist()
            contacts.extend(_rows_to_contacts(rows))
        return contacts
    if ext == ".docx":
        _check_zip_bomb(path)
        return _parse_docx(path)
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in IMAGE_EXTENSIONS:
        import connectors

        return connectors.extract_contacts_from_image(path)["contacts"]

    with open(path, "rb") as f:
        raw = f.read()
    text = _decode_bytes(raw)

    if ext in (".csv", ".tsv"):
        return _parse_csv_text(text)
    if ext == ".vcf":
        return _parse_vcf(text)
    if ext == ".json":
        try:
            return _parse_json(text)
        except Exception:
            return _extract_emails_from_text(text)
    return _extract_emails_from_text(text)
