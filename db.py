import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.environ.get("CONTACTSHUB_DATA_DIR"):
    DATA_DIR = os.environ["CONTACTSHUB_DATA_DIR"]
elif os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
    DATA_DIR = "/tmp/contactshub"
else:
    DATA_DIR = BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "contacts.db")
SECRETS_PATH = os.path.join(DATA_DIR, ".contactshub_secrets.json")


def _load_local_secrets() -> Dict[str, str]:
    try:
        if os.path.isfile(SECRETS_PATH):
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        pass
    return {}


def _save_local_secrets(data: Dict[str, str]) -> None:
    tmp_path = SECRETS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, SECRETS_PATH)


def get_or_create_local_secret(name: str, env_var: str = "", legacy_db_key: str = "") -> str:
    if env_var:
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            return env_val
    store = _load_local_secrets()
    if store.get(name):
        return store[name]
    val = ""
    if legacy_db_key:
        try:
            with get_conn() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (legacy_db_key,)).fetchone()
                if row and row["value"]:
                    val = str(row["value"]).strip()
                    # Migrate out of contacts.db so DB backups never leak keys
                    conn.execute("DELETE FROM app_settings WHERE key = ?", (legacy_db_key,))
        except Exception:
            pass
    if not val:
        val = secrets.token_hex(32)
    store[name] = val
    try:
        _save_local_secrets(store)
    except Exception:
        pass
    return val


def get_wa_bridge_secret() -> str:
    return get_or_create_local_secret("wa_bridge_secret", env_var="WA_BRIDGE_SECRET")


# ---------------- SECRET ENCRYPTION AT REST (STDLIB AUTHENTICATED CTR) ----------------

def _get_master_key() -> bytes:
    env_k = os.environ.get("CONTACTSHUB_MASTER_KEY", "").strip()
    if env_k:
        return hashlib.sha256(env_k.encode("utf-8")).digest()
    hex_key = get_or_create_local_secret("master_enc_key", legacy_db_key="master_enc_key")
    try:
        raw = bytes.fromhex(hex_key)
        if len(raw) == 32:
            return raw
    except ValueError:
        pass
    return hashlib.sha256(hex_key.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    if plaintext.startswith("enc1:"):
        return plaintext
    key = _get_master_key()
    nonce = secrets.token_bytes(16)
    pt = plaintext.encode("utf-8")
    ks = _keystream(key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    token = base64.urlsafe_b64encode(nonce + mac + ct).decode("ascii")
    return f"enc1:{token}"


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith("enc1:"):
        return stored  # Backward compatibility with legacy plaintext
    try:
        raw = base64.urlsafe_b64decode(stored[5:].encode("ascii"))
        nonce, mac, ct = raw[:16], raw[16:32], raw[32:]
        key = _get_master_key()
        expected_mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expected_mac):
            return ""
        ks = _keystream(key, nonce, len(ct))
        pt = bytes(a ^ b for a, b in zip(ct, ks))
        return pt.decode("utf-8")
    except Exception:
        return ""

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
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_app_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_app_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def create_hot_backup(dest_path: str) -> str:
    """Create a transactionally consistent SQLite hot backup (safe under WAL mode)."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    src = get_conn()
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_path


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
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
            ("deal_value", "REAL DEFAULT 0"),
            ("workspace_id", "INTEGER DEFAULT 1"),
            ("assigned_to", "TEXT DEFAULT ''"),
            ("referral_code", "TEXT DEFAULT ''"),
            ("referred_by", "TEXT DEFAULT ''"),
        ]
        for col, ddl in migrations:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")
        conn.execute("UPDATE contacts SET workspace_id = 1 WHERE workspace_id IS NULL")
        conn.execute("UPDATE contacts SET deal_value = 0 WHERE deal_value IS NULL")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                tagline TEXT DEFAULT 'CRM & Centralisation Email',
                logo_url TEXT DEFAULT '',
                primary_color TEXT DEFAULT '#2563eb',
                currency TEXT DEFAULT 'FCFA',
                email_signature TEXT DEFAULT '',
                public_base_url TEXT DEFAULT '',
                imap_host TEXT DEFAULT '',
                imap_port INTEGER DEFAULT 993,
                imap_username TEXT DEFAULT '',
                imap_password TEXT DEFAULT '',
                plan TEXT DEFAULT 'pro',
                onboarding_done INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        ws_cnt = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        if ws_cnt == 0:
            now_ws = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO workspaces (
                    id, slug, name, tagline, logo_url, primary_color, currency,
                    email_signature, public_base_url, plan, onboarding_done, created_at
                ) VALUES (1, 'msf-academy', ?, ?, '', '#0077e6', 'FCFA', ?, '', 'enterprise', 0, ?)
                """,
                (
                    "MSF'S English Learning Academy",
                    "CRM, Inscriptions & Croissance",
                    "L'équipe MSF'S English Learning Academy\nLomé — Carrefour Adroukpapé, Avedji Limosine",
                    now_ws,
                ),
            )

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
            CREATE TABLE IF NOT EXISTS whatsapp_contacts (
                jid TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                name TEXT DEFAULT '',
                notify TEXT DEFAULT '',
                contact_id INTEGER,
                last_synced_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whatsapp_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_msg_id TEXT UNIQUE NOT NULL,
                jid TEXT NOT NULL,
                phone TEXT NOT NULL,
                contact_id INTEGER,
                from_me INTEGER DEFAULT 0,
                body TEXT NOT NULL,
                push_name TEXT DEFAULT '',
                sent_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_msg_phone ON whatsapp_messages(phone, sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_msg_cid ON whatsapp_messages(contact_id, sent_at)")

        # Social channels (Facebook Messenger/Lead Ads & TikTok Lead Gen/Bio)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_channels (
                platform TEXT PRIMARY KEY,
                page_id TEXT DEFAULT '',
                page_name TEXT DEFAULT '',
                access_token TEXT DEFAULT '',
                verify_token TEXT DEFAULT '',
                lead_tag TEXT DEFAULT '',
                bio_url TEXT DEFAULT '',
                auto_welcome_wa INTEGER DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT UNIQUE,
                sender_id TEXT NOT NULL,
                sender_name TEXT DEFAULT '',
                contact_id INTEGER,
                from_me INTEGER DEFAULT 0,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_social_msg_sender ON social_messages(platform, sender_id, sent_at)")

        # Targeted WhatsApp Broadcasts
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wa_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                message_tpl TEXT NOT NULL,
                filter_json TEXT DEFAULT '{}',
                delay_min_sec INTEGER DEFAULT 18,
                delay_max_sec INTEGER DEFAULT 40,
                status TEXT DEFAULT 'pending',
                total_count INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wa_broadcast_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER NOT NULL,
                contact_id INTEGER,
                phone TEXT NOT NULL,
                full_name TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                sent_at TEXT DEFAULT ''
            )
            """
        )

        # Student Payments & Receipts (TMoney / Flooz / Cash / Bank)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS student_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_num TEXT UNIQUE NOT NULL,
                contact_id INTEGER,
                student_name TEXT NOT NULL,
                student_phone TEXT DEFAULT '',
                student_email TEXT DEFAULT '',
                course_name TEXT NOT NULL,
                total_fee REAL DEFAULT 0,
                amount_paid REAL NOT NULL,
                payment_method TEXT DEFAULT 'TMoney',
                payment_ref TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                workspace_id INTEGER DEFAULT 1,
                paid_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Appointments & Placement Tests (RDV)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_ref TEXT UNIQUE NOT NULL,
                contact_id INTEGER,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT DEFAULT '',
                topic TEXT DEFAULT 'Test de niveau gratuit',
                mode TEXT DEFAULT 'Présentiel',
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                status TEXT DEFAULT 'confirmed',
                notes TEXT DEFAULT '',
                workspace_id INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(appointment_date, appointment_time)")
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_ws ON contacts(workspace_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_score ON contacts(lead_score DESC, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_contact ON contact_activities(contact_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_contact_done ON contact_tasks(contact_id, done)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_logs_contact ON email_logs(contact_id, sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seq_enroll_due ON sequence_enrollments(sequence_id, state, next_send_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_form_subs_form ON form_submissions(form_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_ws ON student_payments(workspace_id)")
        # Auto-migrate reminder flags on appointments + issued_licenses table for SaaS Reseller Console
        appt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()}
        if "reminded_j1" not in appt_cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN reminded_j1 INTEGER DEFAULT 0")
        if "reminded_h2" not in appt_cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN reminded_h2 INTEGER DEFAULT 0")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS issued_licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                plan TEXT NOT NULL,
                exp_date TEXT NOT NULL,
                max_contacts INTEGER DEFAULT 25000,
                license_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                trigger_event TEXT NOT NULL,
                condition_json TEXT DEFAULT '{}',
                action_type TEXT NOT NULL,
                action_config_json TEXT DEFAULT '{}',
                runs_count INTEGER DEFAULT 0,
                last_run_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_num TEXT UNIQUE NOT NULL,
                doc_type TEXT DEFAULT 'devis',
                contact_id INTEGER,
                client_name TEXT NOT NULL,
                client_company TEXT DEFAULT '',
                client_phone TEXT DEFAULT '',
                client_email TEXT DEFAULT '',
                title TEXT NOT NULL,
                items_json TEXT DEFAULT '[]',
                subtotal REAL DEFAULT 0,
                discount REAL DEFAULT 0,
                tax_rate REAL DEFAULT 0,
                total_amount REAL DEFAULT 0,
                currency TEXT DEFAULT 'FCFA',
                valid_until TEXT DEFAULT '',
                status TEXT DEFAULT 'sent',
                notes TEXT DEFAULT '',
                payment_id INTEGER,
                workspace_id INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quotes_ws ON quotes(workspace_id)")
        q_cols = {r["name"] for r in conn.execute("PRAGMA table_info(quotes)").fetchall()}
        for col, ddl in (
            ("signed_by", "TEXT DEFAULT ''"),
            ("signed_at", "TEXT DEFAULT ''"),
            ("signature_data", "TEXT DEFAULT ''"),
            ("signature_hash", "TEXT DEFAULT ''"),
            ("signed_ip", "TEXT DEFAULT ''"),
        ):
            if col not in q_cols:
                conn.execute(f"ALTER TABLE quotes ADD COLUMN {col} {ddl}")

        # Clean up legacy unresolved WhatsApp @lid ghost records (fake 14-15 digit numbers)
        conn.execute("DELETE FROM whatsapp_messages WHERE jid LIKE '%@lid' OR LENGTH(REPLACE(phone, '+', '')) >= 14")
        conn.execute("DELETE FROM whatsapp_contacts WHERE jid LIKE '%@lid' OR LENGTH(REPLACE(phone, '+', '')) >= 14")
        conn.execute(
            "DELETE FROM contacts WHERE source_type = 'whatsapp' AND email LIKE 'wa.%@whatsapp.local' "
            "AND LENGTH(REPLACE(phone, '+', '')) >= 14"
        )
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_active_slot "
                "ON appointments(appointment_date, appointment_time) WHERE status != 'cancelled'"
            )
        except sqlite3.IntegrityError:
            pass

        # Add workspace_id column safely to secondary tables for SaaS multi-tenancy
        for tbl in ("lead_forms", "sequences", "smart_segments", "users"):
            tcols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if "workspace_id" not in tcols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN workspace_id INTEGER DEFAULT 1")
                conn.execute(f"UPDATE {tbl} SET workspace_id = 1 WHERE workspace_id IS NULL")

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


def get_workspace(workspace_id: int = 1) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM workspaces ORDER BY id ASC LIMIT 1").fetchone()
    if not row:
        return {
            "id": 1,
            "slug": "msf-academy",
            "name": "MSF'S English Learning Academy",
            "tagline": "CRM, Inscriptions & Croissance",
            "logo_url": "",
            "primary_color": "#0077e6",
            "currency": "FCFA",
            "email_signature": "",
            "public_base_url": "",
            "imap_host": "",
            "imap_port": 993,
            "imap_username": "",
            "imap_configured": False,
            "plan": "enterprise",
            "onboarding_done": 0,
        }
    d = dict(row)
    d["imap_configured"] = bool(d.get("imap_host") and d.get("imap_username") and d.get("imap_password"))
    d.pop("imap_password", None)
    return d


def get_workspace_imap(workspace_id: int = 1) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT imap_host, imap_port, imap_username, imap_password FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "host": row["imap_host"] or "",
        "port": int(row["imap_port"] or 993),
        "username": row["imap_username"] or "",
        "password": decrypt_secret(row["imap_password"] or ""),
    }


def list_workspaces() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.slug, w.name, w.tagline, w.logo_url, w.primary_color,
                   w.currency, w.plan, w.onboarding_done, w.created_at,
                   (SELECT COUNT(*) FROM contacts c WHERE COALESCE(c.workspace_id, 1) = w.id) AS contacts_count
            FROM workspaces w
            ORDER BY w.id ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def create_workspace(name: str, tagline: str = "CRM & Croissance", primary_color: str = "#2563eb", currency: str = "FCFA", plan: str = "pro") -> Dict[str, Any]:
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        slug = base_slug
        n = 2
        while conn.execute("SELECT 1 FROM workspaces WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{n}"
            n += 1
        cur = conn.execute(
            """
            INSERT INTO workspaces (slug, name, tagline, primary_color, currency, plan, onboarding_done, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (slug, name.strip(), tagline.strip(), primary_color.strip() or "#2563eb", currency.strip() or "FCFA", plan.strip() or "pro", now),
        )
        ws_id = cur.lastrowid
    return get_workspace(ws_id)


def update_workspace(workspace_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    fields = []
    params: List[Any] = []
    for col in ("name", "tagline", "logo_url", "primary_color", "currency", "email_signature", "public_base_url", "imap_host", "imap_username", "plan"):
        if col in data and data[col] is not None:
            fields.append(f"{col} = ?")
            params.append(str(data[col]).strip())
    if "imap_port" in data and data["imap_port"] is not None:
        fields.append("imap_port = ?")
        params.append(int(data["imap_port"] or 993))
    if data.get("imap_password"):
        fields.append("imap_password = ?")
        params.append(encrypt_secret(str(data["imap_password"])))
    if "onboarding_done" in data:
        fields.append("onboarding_done = ?")
        params.append(1 if data["onboarding_done"] else 0)
    if fields:
        params.append(workspace_id)
        with get_conn() as conn:
            conn.execute(f"UPDATE workspaces SET {', '.join(fields)} WHERE id = ?", params)
    return get_workspace(workspace_id)


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
    workspace_id: int = 1,
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
            try:
                deal_value = float(rec.get("deal_value") or 0)
            except (TypeError, ValueError):
                deal_value = 0.0
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
                        deal_value, workspace_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unchecked', '', ?, ?, ?, ?)
                    """,
                    (
                        email, first_name, last_name, full_name, phone,
                        company, role, tags, source_type, source_name,
                        notes, status, email_type, deal_value, workspace_id, now, now,
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
    workspace_id: Optional[int] = None,
    sort_by: str = "updated_desc",
    page: int = 1,
    per_page: int = 50,
    all_rows: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    where = []
    params: List[Any] = []

    if workspace_id is not None:
        where.append("COALESCE(workspace_id, 1) = ?")
        params.append(int(workspace_id))

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
        "tag_asc": "CASE WHEN TRIM(COALESCE(tags, '')) = '' THEN 1 ELSE 0 END ASC, LOWER(tags) ASC, updated_at DESC",
        "tag_desc": "CASE WHEN TRIM(COALESCE(tags, '')) = '' THEN 1 ELSE 0 END ASC, LOWER(tags) DESC, updated_at DESC",
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


SYNTHETIC_EMAIL_DOMAINS = ("@whatsapp.local", "@booking.local", "@radar.local")


def _phone_match_key(phone: str) -> str:
    d = re.sub(r"\D+", "", str(phone or ""))
    if d.startswith("00"):
        d = d[2:]
    if len(d) < 7:
        return ""
    return d[-8:] if len(d) >= 8 else d


def _is_synthetic_email(email: str) -> bool:
    em = (email or "").strip().lower()
    return any(em.endswith(s) for s in SYNTHETIC_EMAIL_DOMAINS)


def _normalize_email_alias(email: str) -> str:
    em = (email or "").strip().lower()
    if "@" not in em:
        return ""
    local, dom = em.split("@", 1)
    if dom in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        dom = "gmail.com"
    return f"{local}@{dom}"


def find_contact_by_phone(phone: str, exclude_id: Optional[int] = None, workspace_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    key = _phone_match_key(phone)
    if not key:
        return None
    with get_conn() as conn:
        if workspace_id is not None:
            rows = conn.execute(
                "SELECT id, email, full_name, phone FROM contacts WHERE phone != '' AND COALESCE(workspace_id, 1) = ?",
                (int(workspace_id),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, email, full_name, phone FROM contacts WHERE phone != ''").fetchall()
    for r in rows:
        if exclude_id is not None and int(r["id"]) == int(exclude_id):
            continue
        if _phone_match_key(r["phone"]) == key:
            return dict(r)
    return None


def _primary_rank(c: Dict[str, Any]) -> Tuple[int, int, int, int]:
    real_email = 0 if _is_synthetic_email(c.get("email") or "") else 1
    filled = sum(1 for k in ("full_name", "phone", "company", "role", "website", "country", "tags") if (c.get(k) or "").strip())
    score = int(c.get("lead_score") or 0)
    return (real_email, score, filled, -int(c.get("id") or 0))


def find_duplicate_groups() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM contacts ORDER BY id ASC").fetchall()]

    by_phone: Dict[str, List[Dict[str, Any]]] = {}
    by_email_norm: Dict[str, List[Dict[str, Any]]] = {}
    by_name: Dict[str, List[Dict[str, Any]]] = {}

    for r in rows:
        pkey = _phone_match_key(r.get("phone") or "")
        if pkey:
            by_phone.setdefault(pkey, []).append(r)

        ekey = _normalize_email_alias(r.get("email") or "")
        if ekey and not _is_synthetic_email(r.get("email") or ""):
            by_email_norm.setdefault(ekey, []).append(r)

        name_key = re.sub(r"\s+", " ", (r.get("full_name") or "").strip().lower())
        if len(name_key) >= 4 and not name_key.startswith("+"):
            by_name.setdefault(name_key, []).append(r)

    groups: List[Dict[str, Any]] = []
    seen_sets = set()

    # 1. Phone duplicates first (highest priority)
    for _, items in by_phone.items():
        if len(items) > 1:
            sig = tuple(sorted(i["id"] for i in items))
            if sig not in seen_sets:
                seen_sets.add(sig)
                ordered = sorted(items, key=_primary_rank, reverse=True)
                groups.append({
                    "kind": "phone",
                    "reason": f"📞 Même téléphone : {ordered[0]['phone']}",
                    "contacts": ordered,
                })

    # 2. Normalized email aliases (e.g., Gmail dot/plus variants)
    for ekey, items in by_email_norm.items():
        if len(items) > 1:
            sig = tuple(sorted(i["id"] for i in items))
            if sig not in seen_sets:
                seen_sets.add(sig)
                ordered = sorted(items, key=_primary_rank, reverse=True)
                groups.append({
                    "kind": "email_norm",
                    "reason": f"✉️ Email similaire : {ekey}",
                    "contacts": ordered,
                })

    # 3. Same full name
    for _, items in by_name.items():
        if len(items) > 1:
            sig = tuple(sorted(i["id"] for i in items))
            if sig not in seen_sets:
                seen_sets.add(sig)
                ordered = sorted(items, key=_primary_rank, reverse=True)
                groups.append({
                    "kind": "name",
                    "reason": f"👤 Même nom : {ordered[0]['full_name']}",
                    "contacts": ordered,
                })

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
        best_score = int(primary.get("lead_score") or 0)
        best_deal = float(primary.get("deal_value") or 0)

        for o in others:
            for field in ("first_name", "last_name", "full_name", "phone", "company", "role", "website", "country", "industry"):
                if not (primary.get(field) or "").strip() and (o.get(field) or "").strip():
                    primary[field] = o[field]
            best_score = max(best_score, int(o.get("lead_score") or 0))
            best_deal = max(best_deal, float(o.get("deal_value") or 0))
            tags = merge_tags(tags, o["tags"])
            if o["email"] and o["email"] != primary["email"] and not _is_synthetic_email(o["email"]):
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
                company = ?, role = ?, website = ?, country = ?, industry = ?,
                lead_score = ?, deal_value = ?, tags = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                primary["first_name"], primary["last_name"], primary["full_name"],
                primary["phone"], primary["company"], primary["role"],
                primary.get("website") or "", primary.get("country") or "", primary.get("industry") or "",
                best_score, best_deal, tags, notes, now, primary_id,
            ),
        )

        # Re-link all child records so no history/payments/messages are orphaned
        for tbl in (
            "contact_activities", "contact_tasks", "email_logs", "sequence_enrollments",
            "form_submissions", "student_payments", "appointments",
            "whatsapp_contacts", "whatsapp_messages", "wa_broadcast_recipients", "click_events",
        ):
            try:
                conn.execute(
                    f"UPDATE {tbl} SET contact_id = ? WHERE contact_id IN ({placeholders})",
                    [primary_id, *secondary_ids],
                )
            except sqlite3.OperationalError:
                pass

        conn.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", secondary_ids)

    return {"merged": len(others), "primary_id": primary_id}


def auto_merge_phone_duplicates() -> Dict[str, Any]:
    groups = [g for g in find_duplicate_groups() if g.get("kind") == "phone"]
    merged_groups = 0
    merged_contacts = 0
    for g in groups:
        cs = g["contacts"]
        if len(cs) < 2:
            continue
        primary_id = cs[0]["id"]
        sec_ids = [c["id"] for c in cs[1:]]
        try:
            res = merge_contacts(primary_id, sec_ids)
            if res.get("merged", 0) > 0:
                merged_groups += 1
                merged_contacts += res["merged"]
        except Exception:
            continue
    return {"ok": True, "merged_groups": merged_groups, "merged_contacts": merged_contacts}


def get_quality_health() -> Dict[str, Any]:
    groups = find_duplicate_groups()
    phone_dups = sum(1 for g in groups if g["kind"] == "phone")
    name_dups = sum(1 for g in groups if g["kind"] == "name")
    email_dups = sum(1 for g in groups if g["kind"] == "email_norm")

    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        invalid_mx = conn.execute("SELECT COUNT(*) FROM contacts WHERE mx_status = 'invalid'").fetchone()[0]
        generic_cnt = conn.execute("SELECT COUNT(*) FROM contacts WHERE email_type = 'generic'").fetchone()[0]
        missing_phone = conn.execute("SELECT COUNT(*) FROM contacts WHERE TRIM(COALESCE(phone, '')) = ''").fetchone()[0]
        missing_name = conn.execute("SELECT COUNT(*) FROM contacts WHERE TRIM(COALESCE(full_name, '')) = ''").fetchone()[0]
        synth_emails = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE email LIKE '%@whatsapp.local' OR email LIKE '%@booking.local' OR email LIKE '%@radar.local'"
        ).fetchone()[0]

    if total == 0:
        health_score = 100
    else:
        penalty = (
            min(30, phone_dups * 5)
            + min(15, name_dups * 2)
            + min(25, int((invalid_mx / max(1, total)) * 100))
            + min(15, int((generic_cnt / max(1, total)) * 40))
            + min(15, int((missing_name / max(1, total)) * 30))
        )
        health_score = max(0, min(100, 100 - penalty))

    return {
        "total": total,
        "health_score": health_score,
        "phone_duplicates": phone_dups,
        "name_duplicates": name_dups,
        "email_duplicates": email_dups,
        "invalid_mx": invalid_mx,
        "generic_emails": generic_cnt,
        "synthetic_emails": synth_emails,
        "missing_phone": missing_phone,
        "missing_name": missing_name,
    }


def normalize_contacts_data(default_country_code: str = "+228") -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    cc = re.sub(r"[^\d+]", "", default_country_code or "+228")
    if cc and not cc.startswith("+"):
        cc = "+" + cc

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, first_name, last_name, full_name, phone, tags FROM contacts").fetchall()]
        updates = []
        for r in rows:
            fn = re.sub(r"\s+", " ", (r["first_name"] or "").strip())
            ln = re.sub(r"\s+", " ", (r["last_name"] or "").strip())
            full = re.sub(r"\s+", " ", (r["full_name"] or "").strip())
            if full.isupper() and len(full) > 3:
                full = full.title()
            if fn.isupper() and len(fn) > 2:
                fn = fn.title()
            if ln.isupper() and len(ln) > 2:
                ln = ln.title()

            raw_phone = (r["phone"] or "").strip()
            norm_phone = raw_phone
            if raw_phone:
                digits = re.sub(r"\D+", "", raw_phone)
                if digits.startswith("00"):
                    digits = digits[2:]
                if len(digits) == 8 and cc:
                    norm_phone = f"{cc}{digits}"
                elif len(digits) >= 7:
                    norm_phone = f"+{digits}"

            norm_tags = merge_tags(r["tags"] or "", "")

            if (fn, ln, full, norm_phone, norm_tags) != (
                r["first_name"] or "", r["last_name"] or "", r["full_name"] or "",
                raw_phone, r["tags"] or "",
            ):
                updates.append((fn, ln, full, norm_phone, norm_tags, now, r["id"]))
                updated += 1

        if updates:
            conn.executemany(
                "UPDATE contacts SET first_name = ?, last_name = ?, full_name = ?, phone = ?, tags = ?, updated_at = ? WHERE id = ?",
                updates,
            )
    return {"ok": True, "updated": updated}


def purge_contacts_by_quality(mode: str) -> Dict[str, Any]:
    with get_conn() as conn:
        if mode == "invalid_mx":
            cur = conn.execute("DELETE FROM contacts WHERE mx_status = 'invalid'")
            return {"ok": True, "deleted": cur.rowcount, "mode": mode}
        if mode == "generic_noreply":
            cur = conn.execute(
                "DELETE FROM contacts WHERE email_type = 'generic' AND (LOWER(email) LIKE 'noreply@%' OR LOWER(email) LIKE 'no-reply@%' OR LOWER(email) LIKE 'donotreply@%')"
            )
            return {"ok": True, "deleted": cur.rowcount, "mode": mode}
        if mode == "unsubscribed":
            cur = conn.execute("DELETE FROM contacts WHERE status = 'unsubscribed'")
            return {"ok": True, "deleted": cur.rowcount, "mode": mode}
    return {"ok": False, "error": "Mode de nettoyage inconnu.", "deleted": 0}


def get_stats(workspace_id: Optional[int] = None) -> Dict[str, Any]:
    ws_clause = " WHERE COALESCE(workspace_id, 1) = ?" if workspace_id is not None else ""
    ws_and = " AND COALESCE(workspace_id, 1) = ?" if workspace_id is not None else ""
    ws_params = (int(workspace_id),) if workspace_id is not None else ()

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM contacts{ws_clause}", ws_params).fetchone()[0]
        by_source = {
            r["source_type"]: r["cnt"]
            for r in conn.execute(
                f"SELECT source_type, COUNT(*) AS cnt FROM contacts{ws_clause} GROUP BY source_type",
                ws_params,
            ).fetchall()
        }
        by_status = {
            r["status"]: r["cnt"]
            for r in conn.execute(
                f"SELECT status, COUNT(*) AS cnt FROM contacts{ws_clause} GROUP BY status",
                ws_params,
            ).fetchall()
        }
        pipeline_value_by_status = {
            r["status"]: float(r["val"] or 0)
            for r in conn.execute(
                f"SELECT status, COALESCE(SUM(deal_value), 0) AS val FROM contacts{ws_clause} GROUP BY status",
                ws_params,
            ).fetchall()
        }
        pipeline_value_total = float(
            conn.execute(
                f"SELECT COALESCE(SUM(deal_value), 0) FROM contacts WHERE status != 'unsubscribed'{ws_and}",
                ws_params,
            ).fetchone()[0]
            or 0
        )
        by_email_type = {
            r["email_type"]: r["cnt"]
            for r in conn.execute(
                f"SELECT email_type, COUNT(*) AS cnt FROM contacts{ws_clause} GROUP BY email_type",
                ws_params,
            ).fetchall()
        }
        by_mx = {
            r["mx_status"]: r["cnt"]
            for r in conn.execute(
                f"SELECT mx_status, COUNT(*) AS cnt FROM contacts{ws_clause} GROUP BY mx_status",
                ws_params,
            ).fetchall()
        }
        emails = [r["email"] for r in conn.execute(f"SELECT email FROM contacts{ws_clause}", ws_params).fetchall()]
        tags_rows = [
            r["tags"]
            for r in conn.execute(f"SELECT tags FROM contacts WHERE tags != ''{ws_and}", ws_params).fetchall()
        ]
        imports_count = conn.execute("SELECT COUNT(*) FROM import_logs").fetchone()[0]
        avg_score = conn.execute(f"SELECT COALESCE(ROUND(AVG(lead_score)), 0) FROM contacts{ws_clause}", ws_params).fetchone()[0]
        hot_leads = conn.execute(f"SELECT COUNT(*) FROM contacts WHERE lead_score >= 60{ws_and}", ws_params).fetchone()[0]
        top_countries = [
            {"country": r["country"], "count": r["cnt"]}
            for r in conn.execute(
                f"SELECT country, COUNT(*) AS cnt FROM contacts WHERE country != ''{ws_and} GROUP BY country ORDER BY cnt DESC LIMIT 20",
                ws_params,
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
        "pipeline_value_total": pipeline_value_total,
        "pipeline_value_by_status": pipeline_value_by_status,
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
