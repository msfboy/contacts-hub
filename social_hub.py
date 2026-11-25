import csv
import io
import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

import db
import wa_broadcast
import wa_client


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_channel_config(platform: str, include_secret: bool = False) -> Dict[str, Any]:
    platform = platform.lower().strip()
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM social_channels WHERE platform = ?", (platform,)).fetchone()
        if not row:
            verify_token = f"ch_{platform}_{secrets.token_hex(6)}"
            default_tag = "Facebook Ads" if platform == "facebook" else "TikTok Leads"
            now = _now()
            conn.execute(
                """
                INSERT INTO social_channels (platform, verify_token, lead_tag, auto_welcome_wa, updated_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (platform, verify_token, default_tag, now),
            )
            row = conn.execute("SELECT * FROM social_channels WHERE platform = ?", (platform,)).fetchone()
        d = dict(row)
        raw_token = db.decrypt_secret(d.get("access_token") or "")
        d["has_token"] = bool(raw_token)
        if include_secret:
            d["access_token"] = raw_token
        else:
            d["access_token"] = ("•" * 10 + raw_token[-4:]) if len(raw_token) > 6 else ("•" * len(raw_token) if raw_token else "")
        return d


def save_channel_config(platform: str, data: Dict[str, Any]) -> Dict[str, Any]:
    platform = platform.lower().strip()
    existing = get_channel_config(platform, include_secret=True)
    token_in = (data.get("access_token") or "").strip()
    if token_in and not token_in.startswith("•"):
        enc_token = db.encrypt_secret(token_in)
    else:
        enc_token = db.encrypt_secret(existing.get("access_token") or "")

    verify_token = (data.get("verify_token") or existing.get("verify_token") or f"ch_{platform}_{secrets.token_hex(6)}").strip()
    page_id = (data.get("page_id") if "page_id" in data else existing.get("page_id") or "").strip()
    page_name = (data.get("page_name") if "page_name" in data else existing.get("page_name") or "").strip()
    lead_tag = (data.get("lead_tag") if "lead_tag" in data else existing.get("lead_tag") or "").strip()
    bio_url = (data.get("bio_url") if "bio_url" in data else existing.get("bio_url") or "").strip()
    auto_wa = 1 if data.get("auto_welcome_wa", existing.get("auto_welcome_wa", 1)) else 0
    now = _now()

    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO social_channels (platform, page_id, page_name, access_token, verify_token, lead_tag, bio_url, auto_welcome_wa, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                page_id = excluded.page_id,
                page_name = excluded.page_name,
                access_token = excluded.access_token,
                verify_token = excluded.verify_token,
                lead_tag = excluded.lead_tag,
                bio_url = excluded.bio_url,
                auto_welcome_wa = excluded.auto_welcome_wa,
                updated_at = excluded.updated_at
            """,
            (platform, page_id, page_name, enc_token, verify_token, lead_tag, bio_url, auto_wa, now),
        )
    return get_channel_config(platform, include_secret=False)


def _upsert_social_lead(
    platform: str,
    full_name: str,
    email: str,
    phone: str,
    company: str = "",
    campaign: str = "",
    tag: str = "",
    workspace_id: int = 1,
    trigger_wa: bool = True,
) -> Optional[int]:
    now = _now()
    email_clean = (email or "").strip().lower()
    phone_clean = wa_client.format_plus_phone(phone) if phone else ""
    digits = wa_client.normalize_digits(phone)
    if not email_clean and not digits:
        return None
    if not email_clean:
        email_clean = f"{platform}.{digits}@social.local"

    full_name = (full_name or phone_clean or email_clean).strip()
    tag_final = tag or ("Facebook Ads" if platform == "facebook" else "TikTok Leads")
    detail = f"{platform.capitalize()} Lead" + (f" · {campaign}" if campaign else "")

    db.upsert_contacts(
        records=[{
            "email": email_clean,
            "full_name": full_name,
            "phone": phone_clean,
            "company": company,
            "status": "followup",
        }],
        source_type=platform,
        source_name=detail,
        source_ref=campaign or platform,
        extra_tag=tag_final,
    )

    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM contacts WHERE email = ?", (email_clean,)).fetchone()
        cid = int(row["id"]) if row else None
        if cid:
            conn.execute(
                "UPDATE contacts SET lead_score = MAX(lead_score, 85), workspace_id = COALESCE(workspace_id, ?), updated_at = ? WHERE id = ?",
                (workspace_id, now, cid),
            )
            icon = "📘" if platform == "facebook" else "🎵"
            conn.execute(
                "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'form_submit', ?, ?)",
                (cid, f"{icon} Nouveau lead {platform.capitalize()} : {full_name}" + (f" ({campaign})" if campaign else ""), now),
            )

    if trigger_wa and phone_clean:
        cfg = get_channel_config(platform, include_secret=False)
        if cfg.get("auto_welcome_wa"):
            wa_broadcast.trigger_auto_welcome_wa(
                contact_id=cid,
                phone=phone_clean,
                full_name=full_name,
                source_label=f"{platform.capitalize()} {campaign}".strip(),
            )
    return cid


def process_facebook_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_channel_config("facebook", include_secret=True)
    token = cfg.get("access_token") or ""
    now = _now()
    msgs_saved = 0
    leads_saved = 0

    for entry in payload.get("entry") or []:
        # 1. Messenger DMs
        for ev in entry.get("messaging") or []:
            sender_id = str((ev.get("sender") or {}).get("id") or "")
            recipient_id = str((ev.get("recipient") or {}).get("id") or "")
            msg = ev.get("message") or {}
            text = (msg.get("text") or "").strip()
            if not text or not sender_id:
                continue
            is_echo = bool(msg.get("is_echo")) or (cfg.get("page_id") and sender_id == cfg.get("page_id"))
            peer_id = recipient_id if is_echo else sender_id
            ext_id = msg.get("mid") or f"fb_{peer_id}_{ev.get('timestamp', now)}"
            ts_ms = ev.get("timestamp")
            sent_at = datetime.fromtimestamp(ts_ms / 1000).isoformat(timespec="seconds") if ts_ms else now

            sender_name = ""
            if token and not is_echo:
                try:
                    r = requests.get(
                        f"https://graph.facebook.com/v20.0/{peer_id}",
                        params={"fields": "name", "access_token": token},
                        timeout=3,
                    )
                    if r.status_code == 200:
                        sender_name = (r.json().get("name") or "").strip()
                except Exception:
                    pass

            with db.get_conn() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO social_messages
                    (platform, external_id, sender_id, sender_name, from_me, body, sent_at, created_at)
                    VALUES ('facebook', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ext_id, peer_id, sender_name, 1 if is_echo else 0, text, sent_at, now),
                )
                if cur.rowcount > 0:
                    msgs_saved += 1

        # 2. Facebook Lead Ads
        for ch in entry.get("changes") or []:
            if ch.get("field") != "leadgen":
                continue
            val = ch.get("value") or {}
            leadgen_id = str(val.get("leadgen_id") or "")
            if not leadgen_id:
                continue
            lead_fields: Dict[str, str] = {}
            if token:
                try:
                    r = requests.get(
                        f"https://graph.facebook.com/v20.0/{leadgen_id}",
                        params={"access_token": token},
                        timeout=6,
                    )
                    if r.status_code == 200:
                        for f in r.json().get("field_data") or []:
                            k = (f.get("name") or "").lower()
                            vals = f.get("values") or []
                            if k and vals:
                                lead_fields[k] = str(vals[0])
                except Exception:
                    pass
            elif isinstance(val.get("lead_data"), dict):
                lead_fields = {k.lower(): str(v) for k, v in val["lead_data"].items()}

            full_name = lead_fields.get("full_name") or f"{lead_fields.get('first_name', '')} {lead_fields.get('last_name', '')}".strip()
            email = lead_fields.get("email") or ""
            phone = lead_fields.get("phone_number") or lead_fields.get("phone") or ""
            company = lead_fields.get("company_name") or lead_fields.get("company") or ""
            if full_name or email or phone:
                cid = _upsert_social_lead("facebook", full_name, email, phone, company, campaign=str(val.get("ad_id") or "Lead Ads"), tag=cfg.get("lead_tag") or "Facebook Ads")
                if cid:
                    leads_saved += 1

    return {"ok": True, "messages_saved": msgs_saved, "leads_saved": leads_saved}


def process_tiktok_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_channel_config("tiktok", include_secret=False)
    leads_in = []
    if isinstance(payload.get("leads"), list):
        leads_in = payload["leads"]
    elif isinstance(payload.get("data"), dict):
        leads_in = [payload["data"]]
    else:
        leads_in = [payload]

    saved = 0
    for item in leads_in:
        fields: Dict[str, str] = {}
        if isinstance(item.get("field_data"), list):
            for f in item["field_data"]:
                k = (f.get("name") or f.get("field_name") or "").lower()
                v = f.get("values")[0] if isinstance(f.get("values"), list) and f["values"] else f.get("value", "")
                if k:
                    fields[k] = str(v)
        else:
            fields = {str(k).lower(): str(v) for k, v in item.items() if v is not None}

        full_name = fields.get("full_name") or fields.get("name") or f"{fields.get('first_name', '')} {fields.get('last_name', '')}".strip()
        email = fields.get("email") or fields.get("email_address") or ""
        phone = fields.get("phone_number") or fields.get("phone") or fields.get("mobile") or ""
        company = fields.get("company") or fields.get("company_name") or ""
        campaign = fields.get("campaign_name") or fields.get("ad_name") or "TikTok Lead Gen"
        cid = _upsert_social_lead("tiktok", full_name, email, phone, company, campaign=campaign, tag=cfg.get("lead_tag") or "TikTok Leads")
        if cid:
            saved += 1
    return {"ok": True, "leads_saved": saved}


def send_facebook_messenger_reply(sender_id: str, text: str) -> Dict[str, Any]:
    cfg = get_channel_config("facebook", include_secret=True)
    token = cfg.get("access_token") or ""
    if not token:
        return {"ok": False, "error": "Page Access Token Facebook non configuré."}
    try:
        r = requests.post(
            "https://graph.facebook.com/v20.0/me/messages",
            params={"access_token": token},
            json={"recipient": {"id": sender_id}, "message": {"text": text}},
            timeout=10,
        )
        data = r.json()
        if r.status_code >= 400:
            err = (data.get("error") or {}).get("message") or f"Erreur Facebook HTTP {r.status_code}"
            return {"ok": False, "error": err}
        now = _now()
        ext_id = data.get("message_id") or f"fb_out_{sender_id}_{secrets.token_hex(4)}"
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO social_messages
                (platform, external_id, sender_id, sender_name, from_me, body, sent_at, created_at)
                VALUES ('facebook', ?, ?, 'Moi', 1, ?, ?, ?)
                """,
                (ext_id, sender_id, text, now, now),
            )
        return {"ok": True, "message_id": ext_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_social_conversations(platform: str = "") -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        where = "WHERE m.platform = ?" if platform else ""
        params = [platform] if platform else []
        rows = conn.execute(
            f"""
            SELECT
                m.platform,
                m.sender_id,
                MAX(CASE WHEN m.from_me = 0 AND m.sender_name != '' THEN m.sender_name ELSE '' END) AS sender_name,
                MAX(m.sent_at) AS last_sent_at,
                COUNT(*) AS message_count,
                MAX(m.contact_id) AS contact_id,
                (SELECT body FROM social_messages m2 WHERE m2.platform = m.platform AND m2.sender_id = m.sender_id ORDER BY m2.sent_at DESC, m2.id DESC LIMIT 1) AS last_body
            FROM social_messages m
            {where}
            GROUP BY m.platform, m.sender_id
            ORDER BY last_sent_at DESC
            LIMIT 100
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_social_messages(platform: str, sender_id: str) -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, platform, external_id, sender_id, sender_name, contact_id, from_me, body, sent_at
            FROM social_messages
            WHERE platform = ? AND sender_id = ?
            ORDER BY sent_at ASC, id ASC
            LIMIT 200
            """,
            (platform, sender_id),
        ).fetchall()
        return [dict(r) for r in rows]


def convert_social_sender_to_crm(platform: str, sender_id: str, phone: str = "", email: str = "", full_name: str = "") -> Dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT sender_name FROM social_messages WHERE platform = ? AND sender_id = ? AND sender_name != '' LIMIT 1",
            (platform, sender_id),
        ).fetchone()
    name = (full_name or (row["sender_name"] if row else "") or f"{platform}_{sender_id}").strip()
    synth_email = (email or "").strip().lower() or f"{platform}.{sender_id}@social.local"
    cid = _upsert_social_lead(platform, name, synth_email, phone, campaign="Messenger DM", trigger_wa=False)
    if cid:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE social_messages SET contact_id = ? WHERE platform = ? AND sender_id = ?",
                (cid, platform, sender_id),
            )
    return {"ok": bool(cid), "contact_id": cid}


def import_social_leads_csv(platform: str, raw_bytes: bytes, tag: str = "", workspace_id: int = 1) -> Dict[str, Any]:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    inserted = 0
    skipped = 0
    for row in reader:
        norm = {str(k or "").strip().lower().replace(" ", "_"): str(v or "").strip() for k, v in row.items()}
        full_name = (
            norm.get("full_name")
            or norm.get("nom_complet")
            or norm.get("name")
            or f"{norm.get('first_name', norm.get('prenom', ''))} {norm.get('last_name', norm.get('nom', ''))}".strip()
        )
        email = norm.get("email") or norm.get("email_address") or norm.get("e-mail") or ""
        phone = norm.get("phone_number") or norm.get("phone") or norm.get("telephone") or norm.get("téléphone") or norm.get("mobile") or ""
        company = norm.get("company_name") or norm.get("company") or norm.get("entreprise") or ""
        campaign = norm.get("campaign_name") or norm.get("ad_name") or norm.get("form_name") or ""
        cid = _upsert_social_lead(
            platform=platform,
            full_name=full_name,
            email=email,
            phone=phone,
            company=company,
            campaign=campaign,
            tag=tag,
            workspace_id=workspace_id,
            trigger_wa=False,
        )
        if cid:
            inserted += 1
        else:
            skipped += 1
    return {"ok": True, "imported": inserted, "skipped": skipped}
