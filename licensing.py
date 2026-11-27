"""Cryptographically signed SaaS license & subscription management (HMAC-SHA256)."""

import base64
import hashlib
import hmac
import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import db


def _signing_secret() -> bytes:
    raw = os.environ.get("LICENSE_SIGNING_SECRET", "").strip()
    if not raw:
        raw = db.get_or_create_local_secret("license_signing_secret")
    return raw.encode("utf-8")


def is_license_required() -> bool:
    return os.environ.get("CONTACTSHUB_REQUIRE_LICENSE", "0").strip() == "1"


def generate_license_key(
    client_name: str,
    plan: str = "pro",
    days: int = 30,
    expires_on: Optional[str] = None,
    max_contacts: int = 25000,
) -> Dict[str, Any]:
    """Generate a signed CHUB-... license key."""
    client = (client_name or "Client ContactsHub").strip()[:100]
    plan_clean = (plan or "pro").strip().lower()
    if plan_clean not in ("starter", "pro", "business", "lifetime"):
        plan_clean = "pro"

    if plan_clean == "lifetime" or (expires_on and expires_on.strip().lower() == "lifetime"):
        exp_str = "lifetime"
    elif expires_on:
        # Validate YYYY-MM-DD
        datetime.strptime(expires_on.strip(), "%Y-%m-%d")
        exp_str = expires_on.strip()
    else:
        exp_str = (date.today() + timedelta(days=max(1, int(days or 30)))).isoformat()

    payload = {
        "client": client,
        "plan": plan_clean,
        "exp": exp_str,
        "max_contacts": max(100, int(max_contacts or 25000)),
        "iat": date.today().isoformat(),
    }
    raw_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
    sig = hmac.new(_signing_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()[:16].upper()
    key = f"CHUB-{b64}.{sig}"
    try:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO issued_licenses (client_name, plan, exp_date, max_contacts, license_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (client, plan_clean, exp_str, payload["max_contacts"], key, datetime.now().isoformat(timespec="seconds")),
            )
    except Exception:
        pass
    return {"ok": True, "license_key": key, "payload": payload}


def list_issued_licenses(limit: int = 100) -> list:
    with db.get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM issued_licenses ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    for r in rows:
        v = verify_license_key(r["license_key"])
        r["valid"] = bool(v.get("valid"))
        r["expired"] = bool(v.get("expired"))
        r["days_left"] = v.get("days_left", 0)
    return rows


def verify_license_key(license_key: str) -> Dict[str, Any]:
    """Verify signature and expiration of a CHUB-... license key."""
    raw = (license_key or "").strip()
    if not raw.startswith("CHUB-") or "." not in raw:
        return {"valid": False, "error": "Format de clé invalide (doit commencer par CHUB-)."}

    body = raw[5:]
    try:
        b64, sig = body.rsplit(".", 1)
    except ValueError:
        return {"valid": False, "error": "Format de clé invalide."}

    expected_sig = hmac.new(_signing_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()[:16].upper()
    if not hmac.compare_digest(sig.upper(), expected_sig):
        return {"valid": False, "error": "Signature de licence invalide ou clé falsifiée."}

    try:
        padded = b64 + "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return {"valid": False, "error": "Contenu de licence corrompu."}

    exp_str = str(payload.get("exp") or "")
    if exp_str == "lifetime":
        return {
            "valid": True,
            "expired": False,
            "days_left": 99999,
            "client": payload.get("client") or "",
            "plan": payload.get("plan") or "lifetime",
            "exp": "lifetime",
            "max_contacts": int(payload.get("max_contacts") or 100000),
        }

    try:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
    except ValueError:
        return {"valid": False, "error": "Date d'expiration invalide dans la clé."}

    days_left = (exp_date - date.today()).days
    if days_left < 0:
        return {
            "valid": False,
            "expired": True,
            "days_left": days_left,
            "client": payload.get("client") or "",
            "plan": payload.get("plan") or "pro",
            "exp": exp_str,
            "error": f"Abonnement expiré le {exp_str}. Renouvelez votre clé de licence.",
        }

    return {
        "valid": True,
        "expired": False,
        "days_left": days_left,
        "client": payload.get("client") or "",
        "plan": payload.get("plan") or "pro",
        "exp": exp_str,
        "max_contacts": int(payload.get("max_contacts") or 25000),
    }


def get_stored_license_key() -> str:
    env_key = os.environ.get("CONTACTSHUB_LICENSE_KEY", "").strip()
    if env_key:
        return env_key
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'saas_license_key'").fetchone()
            return (row["value"] or "").strip() if row else ""
    except Exception:
        return ""


def save_license_key(license_key: str) -> Dict[str, Any]:
    check = verify_license_key(license_key)
    if not check.get("valid"):
        return {"ok": False, "error": check.get("error") or "Clé de licence invalide."}
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('saas_license_key', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (license_key.strip(),),
        )
    return {"ok": True, "status": check}


def get_current_license_status() -> Dict[str, Any]:
    required = is_license_required()
    key = get_stored_license_key()
    if key:
        info = verify_license_key(key)
        info["required"] = required
        info["has_key"] = True
        return info
    if not required:
        return {
            "valid": True,
            "required": False,
            "has_key": False,
            "mode": "owner_unlimited",
            "client": "Instance Propriétaire (Illimitée)",
            "plan": "owner",
            "exp": "illimité",
            "days_left": 99999,
        }
    return {
        "valid": False,
        "required": True,
        "has_key": False,
        "expired": False,
        "error": "Aucune clé de licence active. Entrez votre clé d'abonnement pour débloquer l'application.",
    }
