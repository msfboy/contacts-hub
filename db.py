import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "contacts.db")

GENERIC_PREFIXES = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "info", "infos", "contact", "contacts", "support", "help", "aide",
    "admin", "administrator", "administrateur", "webmaster", "postmaster",
    "sales", "commercial", "ventes", "hello", "bonjour", "salut",
    "newsletter", "news", "marketing", "communication", "press", "presse",
    "billing", "facturation", "compta", "comptabilite", "finance",
    "service", "services", "client", "clients", "rh", "hr", "jobs",
    "recrutement", "careers", "team", "equipe", "office", "direction",
    "accueil", "secretariat", "notifications", "alert", "alerts", "mailer",
}

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.fr", "yahoo.co.uk",
    "outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr", "live.com",
    "live.fr", "msn.com", "icloud.com", "me.com", "mac.com",
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr", "bbox.fr",
    "laposte.net", "proton.me", "protonmail.com", "gmx.com", "gmx.fr",
    "aol.com", "yandex.com", "mail.com", "zoho.com",
}

VALID_STATUSES = {"new", "contacted", "client", "followup", "unsubscribed"}


def classify_email(email: str) -> str:
    em = (email or "").strip().lower()
    if "@" not in em:
        return "unknown"
    local, domain = em.split("@", 1)
    if local in GENERIC_PREFIXES or any(local.startswith(p + ".") or local.startswith(p + "-") for p in ("noreply", "no-reply", "support", "info", "contact")):
        return "generic"
    if domain in PERSONAL_DOMAINS:
        return "perso"
    return "pro"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                company TEXT DEFAULT '',
                role TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                source_type TEXT DEFAULT 'manual',
                source_detail TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                email_type TEXT DEFAULT 'unknown',
                mx_status TEXT DEFAULT 'unchecked',
                last_contacted_at TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Safe migration for existing DBs
        existing_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()
        }
        migrations = [
            ("status", "TEXT DEFAULT 'new'"),
            ("email_type", "TEXT DEFAULT 'unknown'"),
            ("mx_status", "TEXT DEFAULT 'unchecked'"),
            ("last_contacted_at", "TEXT DEFAULT ''"),
            ("lead_score", "INTEGER DEFAULT 0"),
            ("website", "TEXT DEFAULT ''"),
            ("country", "TEXT DEFAULT ''"),
            ("industry", "TEXT DEFAULT ''"),
            ("company_desc", "TEXT DEFAULT ''"),
        ]
        for col, ddl in migrations:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS import_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_ref TEXT DEFAULT '',
                total_found INTEGER DEFAULT 0,
                inserted INTEGER DEFAULT 0,
                updated INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                imported_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watched_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                tag TEXT DEFAULT '',
                mode TEXT DEFAULT 'structured',
                enabled INTEGER DEFAULT 1,
                last_scan_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watched_file_state (
                file_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS smtp_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                host TEXT DEFAULT '',
                port INTEGER DEFAULT 587,
                username TEXT DEFAULT '',
                password TEXT DEFAULT '',
                from_name TEXT DEFAULT '',
                use_tls INTEGER DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS smtp_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 587,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                from_name TEXT DEFAULT '',
                use_tls INTEGER DEFAULT 1,
                daily_limit INTEGER DEFAULT 100,
                sent_today INTEGER DEFAULT 0,
                last_sent_date TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                delay_min_sec INTEGER DEFAULT 20,
                delay_max_sec INTEGER DEFAULT 45,
                include_unsubscribe INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sequence_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                step_order INTEGER NOT NULL,
                delay_days INTEGER DEFAULT 0,
                subject TEXT NOT NULL,
                body TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sequence_enrollments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                current_step INTEGER DEFAULT 0,
                next_send_at TEXT NOT NULL,
                state TEXT DEFAULT 'active',
                last_error TEXT DEFAULT '',
                enrolled_at TEXT NOT NULL,
                UNIQUE(sequence_id, contact_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id TEXT UNIQUE NOT NULL,
                contact_id INTEGER,
                email TEXT NOT NULL,
                sequence_id INTEGER,
                step_order INTEGER DEFAULT 0,
                smtp_account_id INTEGER,
                subject TEXT DEFAULT '',
                status TEXT DEFAULT 'sent',
                opened_count INTEGER DEFAULT 0,
                opened_at TEXT DEFAULT '',
                sent_at TEXT NOT NULL,
                error TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                kind TEXT DEFAULT 'note',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                filters_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                role TEXT DEFAULT 'admin',
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT DEFAULT 'local',
                action TEXT NOT NULL,
                detail TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whatsapp_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_forms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT DEFAULT '',
                cta_text TEXT DEFAULT 'Recevoir mon accès gratuit',
                tag TEXT DEFAULT 'Lead Chaud',
                redirect_url TEXT DEFAULT '',
                form_kind TEXT DEFAULT 'simple',
                config_json TEXT DEFAULT '{}',
                submissions_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        lf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(lead_forms)").fetchall()}
        if "form_kind" not in lf_cols:
            conn.execute("ALTER TABLE lead_forms ADD COLUMN form_kind TEXT DEFAULT 'simple'")
        if "config_json" not in lf_cols:
            conn.execute("ALTER TABLE lead_forms ADD COLUMN config_json TEXT DEFAULT '{}'")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS form_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id INTEGER NOT NULL,
                contact_id INTEGER,
                dossier_num TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                session_choice TEXT DEFAULT '',
                level_choice TEXT DEFAULT '',
                payment_plan TEXT DEFAULT '',
                payment_method TEXT DEFAULT '',
                payment_ref TEXT DEFAULT '',
                data_json TEXT DEFAULT '{}',
                submitted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                target_url TEXT NOT NULL,
                clicks_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS click_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                contact_id INTEGER,
                clicked_at TEXT NOT NULL
            )
            """
        )

        # Seed default WhatsApp templates & English test capture form
        now_seed = datetime.now().isoformat(timespec="seconds")
        if conn.execute("SELECT COUNT(*) FROM whatsapp_templates").fetchone()[0] == 0:
            wa_defaults = [
                (
                    "Test de niveau d'anglais gratuit (Particuliers)",
                    "Bonjour {prenom} 👋\nNous lançons une nouvelle session de formation en anglais (en présentiel et en ligne).\nSouhaitez-vous faire notre test de niveau gratuit (2 min) pour recevoir le programme adapté à votre objectif ?",
                ),
                (
                    "Offre Anglais Pro Entreprises (B2B)",
                    "Bonjour {prenom},\nNous accompagnons les équipes de {entreprise} pour maîtriser l'anglais professionnel (réunions, emails, TOEIC/Business English).\nPuis-je vous envoyer notre plaquette entreprise ici sur WhatsApp ?",
                ),
                (
                    "Relance rapide après email",
                    "Bonjour {prenom} 👋 Je viens de vous envoyer un email concernant notre prochaine rentrée d'anglais. Avez-vous des questions sur les horaires ou les tarifs ?",
                ),
            ]
            for wname, wbody in wa_defaults:
                conn.execute(
                    "INSERT INTO whatsapp_templates (name, body, created_at) VALUES (?, ?, ?)",
                    (wname, wbody, now_seed),
                )

        if conn.execute("SELECT COUNT(*) FROM lead_forms").fetchone()[0] == 0:
            conn.execute(
                """
                INSERT INTO lead_forms (slug, title, subtitle, cta_text, tag, redirect_url, form_kind, config_json, submissions_count, created_at)
                VALUES (?, ?, ?, ?, ?, '', 'simple', '{}', 0, ?)
                """,
                (
                    "test-anglais-gratuit",
                    "Évaluez votre niveau d'anglais gratuitement",
                    "Laissez vos coordonnées pour recevoir votre test de niveau rapide et découvrir le programme adapté à vos objectifs (Pro, Étudiant, IELTS/TOEFL).",
                    "Recevoir mon test gratuit",
                    "Lead Chaud, Test Anglais",
                    now_seed,
                ),
            )

        # Seed MSF'S English Learning Academy full registration form if not present
        msf_exists = conn.execute("SELECT id FROM lead_forms WHERE slug = 'msf-rentree-dec-2026'").fetchone()
        if not msf_exists:
            import json as _json
            msf_cfg = {
                "org_name": "MSF'S English Learning Academy",
                "badge": "Rentrée 5 Décembre 2026",
                "location": "Lomé — Carrefour Adroukpapé, Avedji Limosine",
                "primary_color": "#0077e6",
                "whatsapp_number": "",
                "dossier_prefix": "MSF-2026",
                "tuition_fee": "30 000 FCFA",
                "registration_fee": "5 000 FCFA",
                "total_fee": "35 000 FCFA",
                "sessions": [
                    "☀️ Session Matin (08h00 - 11h00)",
                    "🌤️ Session Midi (12h30 - 14h00)",
                    "🌙 Session Soir (18h30 - 20h00)",
                ],
                "levels": [
                    "Débutant (A1)",
                    "Élémentaire (A2)",
                    "Intermédiaire (B1/B2)",
                    "Avancé (C1)",
                ],
                "objectives": [
                    "Communication & fluidité à l'oral",
                    "Carrière & Anglais professionnel",
                    "Préparation Examens (IELTS / TOEFL / TOEIC)",
                    "Voyage & Études à l'étranger",
                ],
                "payment_methods": [
                    "Espèces (sur place)",
                    "Mixx by Yas (TMoney)",
                    "Flooz (Moov Money)",
                    "Virement",
                ],
                "custom_fields": [],
            }
            conn.execute(
                """
                INSERT INTO lead_forms (slug, title, subtitle, cta_text, tag, redirect_url, form_kind, config_json, submissions_count, created_at)
                VALUES (?, ?, ?, ?, ?, '', 'training', ?, 0, ?)
                """,
                (
                    "msf-rentree-dec-2026",
                    "Formation Standard d'Anglais — Rentrée 5 Déc. 2026",
                    "Formulaire officiel d'inscription à la Formation Standard d'Anglais. Sessions Matin, Midi et Soir.",
                    "✅ Valider mon inscription",
                    "MSF Rentrée Déc 2026, Inscrit Formation",
                    _json.dumps(msf_cfg, ensure_ascii=False),
                    now_seed,
                ),
            )
        seg_cnt = conn.execute("SELECT COUNT(*) FROM smart_segments").fetchone()[0]
        if seg_cnt == 0:
            now_s = datetime.now().isoformat(timespec="seconds")
            defaults = [
                ("🔥 Top Leads Pro (Score ≥ 60)", '{"email_type":"pro","min_score":60}'),
                ("💼 Emails Pro jamais contactés", '{"email_type":"pro","status":"new"}'),
                ("📞 Contacts avec téléphone", '{"has_phone":true}'),
            ]
            for sname, sjson in defaults:
                conn.execute(
                    "INSERT INTO smart_segments (name, filters_json, created_at) VALUES (?, ?, ?)",
                    (sname, sjson, now_s),
                )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_type ON contacts(email_type)")

        # Seed a default welcome template if none exists
        cnt = conn.execute("SELECT COUNT(*) FROM email_templates").fetchone()[0]
        if cnt == 0:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO email_templates (name, subject, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "Prise de contact rapide",
                    "Bonjour {prenom}, restons en contact",
                    "Bonjour {prenom},\n\nJ'espère que vous allez bien chez {entreprise}.\nJe vous écris pour prendre de vos nouvelles et voir si nous pouvons collaborer prochainement.\n\nBien cordialement,",
                    now,
                    now,
                ),
            )


def normalize_tags(tags_str: str) -> str:
    if not tags_str:
        return ""
    parts = [t.strip() for t in tags_str.replace(";", ",").split(",") if t.strip()]
    seen = []
    seen_lower = set()
    for p in parts:
        pl = p.lower()
        if pl not in seen_lower:
            seen_lower.add(pl)
            seen.append(p)
    return ", ".join(seen)


def merge_tags(existing: str, incoming: str) -> str:
    combined = ",".join([x for x in [existing, incoming] if x])
    return normalize_tags(combined)


def upsert_contacts(
    records: List[Dict[str, Any]],
    source_type: str,
    source_name: str,
    source_ref: str = "",
    extra_tag: str = "",
) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    updated = 0
    skipped = 0

    with get_conn() as conn:
        for rec in records:
            email = (rec.get("email") or "").strip().lower()
            if not email or "@" not in email:
                skipped += 1
                continue

            first_name = (rec.get("first_name") or "").strip()
            last_name = (rec.get("last_name") or "").strip()
            full_name = (rec.get("full_name") or "").strip()
            if not full_name and (first_name or last_name):
                full_name = f"{first_name} {last_name}".strip()
            elif full_name and not first_name and not last_name:
                parts = full_name.split(None, 1)
                first_name = parts[0]
                last_name = parts[1] if len(parts) > 1 else ""

            phone = (rec.get("phone") or "").strip()
            company = (rec.get("company") or "").strip()
            role = (rec.get("role") or "").strip()
            notes = (rec.get("notes") or "").strip()
            tags = merge_tags(rec.get("tags") or "", extra_tag)
            status = (rec.get("status") or "new").strip().lower()
            if status not in VALID_STATUSES:
                status = "new"
            email_type = classify_email(email)

            existing = conn.execute(
                "SELECT * FROM contacts WHERE email = ?", (email,)
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO contacts (
                        email, first_name, last_name, full_name, phone,
                        company, role, tags, source_type, source_detail,
                        notes, status, email_type, mx_status, last_contacted_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unchecked', '', ?, ?)
                    """,
                    (
                        email, first_name, last_name, full_name, phone,
                        company, role, tags, source_type, source_name,
                        notes, status, email_type, now, now,
                    ),
                )
                inserted += 1
            else:
                new_first = existing["first_name"] or first_name
                new_last = existing["last_name"] or last_name
                new_full = existing["full_name"] or full_name
                new_phone = existing["phone"] or phone
                new_company = existing["company"] or company
                new_role = existing["role"] or role
                new_notes = existing["notes"] or notes
                new_tags = merge_tags(existing["tags"], tags)
                new_type = existing["email_type"] if existing["email_type"] != "unknown" else email_type

                changed = any(
                    [
                        new_first != existing["first_name"],
                        new_last != existing["last_name"],
                        new_full != existing["full_name"],
                        new_phone != existing["phone"],
                        new_company != existing["company"],
                        new_role != existing["role"],
                        new_notes != existing["notes"],
                        new_tags != existing["tags"],
                        new_type != existing["email_type"],
                    ]
                )
                if changed:
                    conn.execute(
                        """
                        UPDATE contacts SET
                            first_name = ?, last_name = ?, full_name = ?,
                            phone = ?, company = ?, role = ?,
                            tags = ?, notes = ?, email_type = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            new_first, new_last, new_full,
                            new_phone, new_company, new_role,
                            new_tags, new_notes, new_type, now,
                            existing["id"],
                        ),
                    )
                    updated += 1
                else:
                    skipped += 1

        conn.execute(
            """
            INSERT INTO import_logs (
                source_type, source_name, source_ref,
                total_found, inserted, updated, skipped, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_type, source_name, source_ref,
                len(records), inserted, updated, skipped, now,
            ),
        )

    return {
        "source_type": source_type,
        "source_name": source_name,
        "total_found": len(records),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def query_contacts(
    q: str = "",
    tag: str = "",
    source_type: str = "",
    domain: str = "",
    status: str = "",
    email_type: str = "",
    mx_status: str = "",
    country: str = "",
    min_score: int = 0,
    has_phone: bool = False,
    sort_by: str = "updated_desc",
    page: int = 1,
    per_page: int = 50,
    all_rows: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    where = []
    params: List[Any] = []

    if q:
        like = f"%{q.strip()}%"
        where.append(
            "(email LIKE ? OR full_name LIKE ? OR company LIKE ? OR phone LIKE ? OR tags LIKE ? OR source_detail LIKE ? OR notes LIKE ? OR industry LIKE ? OR country LIKE ?)"
        )
        params.extend([like] * 9)

    if tag:
        where.append("(',' || LOWER(REPLACE(tags, ', ', ',')) || ',') LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")

    if source_type:
        where.append("source_type = ?")
        params.append(source_type.strip())

    if domain:
        where.append("LOWER(email) LIKE ?")
        params.append(f"%@{domain.strip().lower()}")

    if status:
        where.append("status = ?")
        params.append(status.strip().lower())

    if email_type:
        where.append("email_type = ?")
        params.append(email_type.strip().lower())

    if mx_status:
        where.append("mx_status = ?")
        params.append(mx_status.strip().lower())

    if country:
        where.append("LOWER(country) = ?")
        params.append(country.strip().lower())

    if min_score > 0:
        where.append("lead_score >= ?")
        params.append(int(min_score))

    if has_phone:
        where.append("phone != ''")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    order_map = {
        "score_desc": "lead_score DESC, updated_at DESC",
        "name_asc": "LOWER(full_name) ASC, email ASC",
        "updated_desc": "updated_at DESC, id DESC",
    }
    order_sql = order_map.get(sort_by, "updated_at DESC, id DESC")

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM contacts {where_sql}", params
        ).fetchone()[0]

        if all_rows:
            rows = conn.execute(
                f"SELECT * FROM contacts {where_sql} ORDER BY {order_sql}",
                params,
            ).fetchall()
        else:
            offset = max(0, (page - 1) * per_page)
            rows = conn.execute(
                f"SELECT * FROM contacts {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()

    return [dict(r) for r in rows], total


def reclassify_all_contacts() -> int:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, email, email_type FROM contacts").fetchall()
        updated = 0
        for r in rows:
            t = classify_email(r["email"])
            if t != r["email_type"]:
                conn.execute("UPDATE contacts SET email_type = ? WHERE id = ?", (t, r["id"]))
                updated += 1
        return updated


def find_duplicate_groups() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM contacts ORDER BY id ASC").fetchall()]

    by_name: Dict[str, List[Dict[str, Any]]] = {}
    by_phone: Dict[str, List[Dict[str, Any]]] = {}

    for r in rows:
        name_key = re.sub(r"\s+", " ", (r["full_name"] or "").strip().lower())
        if len(name_key) >= 4:
            by_name.setdefault(name_key, []).append(r)
        phone_digits = re.sub(r"\D+", "", r["phone"] or "")
        if len(phone_digits) >= 7:
            by_phone.setdefault(phone_digits[-8:], []).append(r)

    groups: List[Dict[str, Any]] = []
    seen_sets = set()

    for key, items in by_name.items():
        if len(items) > 1:
            sig = tuple(sorted(i["id"] for i in items))
            if sig not in seen_sets:
                seen_sets.add(sig)
                groups.append({"reason": f"Même nom : {items[0]['full_name']}", "contacts": items})

    for key, items in by_phone.items():
        if len(items) > 1:
            sig = tuple(sorted(i["id"] for i in items))
            if sig not in seen_sets:
                seen_sets.add(sig)
                groups.append({"reason": f"Même téléphone : {items[0]['phone']}", "contacts": items})

    return groups


def merge_contacts(primary_id: int, secondary_ids: List[int]) -> Dict[str, Any]:
    secondary_ids = [i for i in secondary_ids if i != primary_id]
    if not secondary_ids:
        return {"merged": 0}

    with get_conn() as conn:
        primary = conn.execute("SELECT * FROM contacts WHERE id = ?", (primary_id,)).fetchone()
        if not primary:
            raise ValueError("Contact principal introuvable.")
        primary = dict(primary)

        placeholders = ",".join("?" for _ in secondary_ids)
        others = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM contacts WHERE id IN ({placeholders})", secondary_ids
            ).fetchall()
        ]
        if not others:
            return {"merged": 0}

        alt_emails = []
        tags = primary["tags"]
        notes = primary["notes"]
        for o in others:
            for field in ("first_name", "last_name", "full_name", "phone", "company", "role"):
                if not primary[field] and o[field]:
                    primary[field] = o[field]
            tags = merge_tags(tags, o["tags"])
            if o["email"] and o["email"] != primary["email"]:
                alt_emails.append(o["email"])
            if o["notes"] and o["notes"] not in notes:
                notes = (notes + "\n" + o["notes"]).strip()

        if alt_emails:
            extra_note = f"Emails fusionnés : {', '.join(alt_emails)}"
            if extra_note not in notes:
                notes = (notes + "\n" + extra_note).strip()

        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE contacts SET
                first_name = ?, last_name = ?, full_name = ?, phone = ?,
                company = ?, role = ?, tags = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                primary["first_name"], primary["last_name"], primary["full_name"],
                primary["phone"], primary["company"], primary["role"],
                tags, notes, now, primary_id,
            ),
        )
        conn.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", secondary_ids)

    return {"merged": len(others), "primary_id": primary_id}


def get_stats() -> Dict[str, Any]:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        by_source = {
            r["source_type"]: r["cnt"]
            for r in conn.execute(
                "SELECT source_type, COUNT(*) AS cnt FROM contacts GROUP BY source_type"
            ).fetchall()
        }
        by_status = {
            r["status"]: r["cnt"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM contacts GROUP BY status"
            ).fetchall()
        }
        by_email_type = {
            r["email_type"]: r["cnt"]
            for r in conn.execute(
                "SELECT email_type, COUNT(*) AS cnt FROM contacts GROUP BY email_type"
            ).fetchall()
        }
        by_mx = {
            r["mx_status"]: r["cnt"]
            for r in conn.execute(
                "SELECT mx_status, COUNT(*) AS cnt FROM contacts GROUP BY mx_status"
            ).fetchall()
        }
        emails = [r["email"] for r in conn.execute("SELECT email FROM contacts").fetchall()]
        tags_rows = [
            r["tags"]
            for r in conn.execute("SELECT tags FROM contacts WHERE tags != ''").fetchall()
        ]
        imports_count = conn.execute("SELECT COUNT(*) FROM import_logs").fetchone()[0]
        avg_score = conn.execute("SELECT COALESCE(ROUND(AVG(lead_score)), 0) FROM contacts").fetchone()[0]
        hot_leads = conn.execute("SELECT COUNT(*) FROM contacts WHERE lead_score >= 60").fetchone()[0]
        top_countries = [
            {"country": r["country"], "count": r["cnt"]}
            for r in conn.execute(
                "SELECT country, COUNT(*) AS cnt FROM contacts WHERE country != '' GROUP BY country ORDER BY cnt DESC LIMIT 20"
            ).fetchall()
        ]
        recent_imports = [
            dict(r)
            for r in conn.execute(
                "SELECT source_name, inserted, updated, imported_at FROM import_logs ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]

    domain_counts: Dict[str, int] = {}
    for e in emails:
        if "@" in e:
            d = e.split("@", 1)[1].lower()
            domain_counts[d] = domain_counts.get(d, 0) + 1

    tag_counts: Dict[str, int] = {}
    for ts in tags_rows:
        for t in ts.split(","):
            t = t.strip()
            if t:
                tag_counts[t] = tag_counts.get(t, 0) + 1

    top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:30]

    return {
        "total_contacts": total,
        "unique_domains": len(domain_counts),
        "imports_count": imports_count,
        "by_source": by_source,
        "by_status": by_status,
        "by_email_type": by_email_type,
        "by_mx": by_mx,
        "avg_score": int(avg_score or 0),
        "hot_leads": int(hot_leads or 0),
        "top_countries": top_countries,
        "top_domains": [{"domain": d, "count": c} for d, c in top_domains],
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "recent_imports": list(reversed(recent_imports)),
    }


def get_import_logs(limit: int = 100) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM import_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
