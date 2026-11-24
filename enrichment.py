import concurrent.futures
import html
import re
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import db

TLD_COUNTRY = {
    "fr": "France",
    "ci": "Côte d'Ivoire",
    "sn": "Sénégal",
    "cm": "Cameroun",
    "ma": "Maroc",
    "tn": "Tunisie",
    "dz": "Algérie",
    "bj": "Bénin",
    "tg": "Togo",
    "bf": "Burkina Faso",
    "ml": "Mali",
    "ga": "Gabon",
    "cd": "RD Congo",
    "cg": "Congo",
    "mg": "Madagascar",
    "be": "Belgique",
    "ch": "Suisse",
    "ca": "Canada",
    "lu": "Luxembourg",
    "uk": "Royaume-Uni",
    "de": "Allemagne",
    "es": "Espagne",
    "it": "Italie",
    "nl": "Pays-Bas",
    "pt": "Portugal",
    "us": "États-Unis",
    "ae": "Émirats arabes unis",
}

PHONE_PREFIX_COUNTRY = [
    ("+225", "Côte d'Ivoire"),
    ("+221", "Sénégal"),
    ("+237", "Cameroun"),
    ("+212", "Maroc"),
    ("+216", "Tunisie"),
    ("+213", "Algérie"),
    ("+229", "Bénin"),
    ("+228", "Togo"),
    ("+226", "Burkina Faso"),
    ("+223", "Mali"),
    ("+241", "Gabon"),
    ("+243", "RD Congo"),
    ("+242", "Congo"),
    ("+261", "Madagascar"),
    ("+33", "France"),
    ("+32", "Belgique"),
    ("+41", "Suisse"),
    ("+352", "Luxembourg"),
    ("+44", "Royaume-Uni"),
    ("+49", "Allemagne"),
    ("+34", "Espagne"),
    ("+39", "Italie"),
    ("+1", "USA / Canada"),
]

DECISION_MAKER_WORDS = {
    "ceo", "cto", "cfo", "coo", "cmo", "dg", "pdg", "directeur", "directrice",
    "director", "founder", "fondateur", "fondatrice", "co-founder", "cofondateur",
    "président", "president", "gérant", "gerant", "owner", "propriétaire",
    "manager", "responsable", "head", "vp", "chief", "partner", "associé",
}

INDUSTRY_KEYWORDS = [
    ("Tech & IT", ["software", "logiciel", "tech", "digital", "cloud", "saas", "informatique", "développement", "app", "ia", "ai", "cyber", "data"]),
    ("Marketing & Agence", ["marketing", "agence", "communication", "publicité", "seo", "branding", "studio", "créatif", "media", "média"]),
    ("Finance & Assurance", ["banque", "bank", "finance", "assurance", "crédit", "invest", "comptab", "audit", "fintech", "patrimoine"]),
    ("Immobilier", ["immobilier", "real estate", "immo", "logement", "appartement", "architecture", "foncier"]),
    ("Santé & Médical", ["santé", "health", "médical", "clinique", "pharma", "docteur", "hôpital", "bien-être", "biotech"]),
    ("Éducation & Formation", ["école", "school", "université", "formation", "academy", "académie", "cours", "éducation", "learning", "institut"]),
    ("E-commerce & Retail", ["boutique", "shop", "store", "e-commerce", "ecommerce", "vente", "livraison", "mode", "fashion"]),
    ("BTP & Industrie", ["construction", "btp", "industrie", "ingénierie", "énergie", "travaux", "architecture", "usine", "manufactur"]),
    ("Conseil & Juridique", ["conseil", "consulting", "avocat", "juridique", "cabinet", "stratégie", "law", "legal"]),
    ("Transport & Logistique", ["transport", "logistique", "freight", "voyage", "travel", "mobilité", "auto", "aviation"]),
    ("Hôtellerie & Restauration", ["hôtel", "hotel", "restaurant", "cuisine", "tourisme", "traiteur", "café", "food"]),
    ("ONG & Association", ["association", "fondation", "ong", "ngo", "nonprofit", "solidaire", "humanitaire"]),
]


def _title_word(w: str) -> str:
    w = re.sub(r"\d+$", "", w).strip("-_.' ")
    if not w:
        return ""
    if len(w) == 1:
        return w.upper() + "."
    return w[0].upper() + w[1:].lower()


def deduce_name_from_email(email: str, email_type: str) -> Tuple[str, str, str]:
    if email_type == "generic" or "@" not in email:
        return "", "", ""
    local = email.split("@", 1)[0].strip().lower()
    # Remove trailing digits
    local = re.sub(r"\d+$", "", local)
    parts = [p for p in re.split(r"[._\-+]+", local) if p and not p.isdigit()]
    if len(parts) >= 2:
        first = _title_word(parts[0])
        last = _title_word(parts[1])
        if len(first) >= 2 and len(last) >= 2:
            return first, last, f"{first} {last}".strip()
    return "", "", ""


def deduce_company_and_website(email: str, email_type: str) -> Tuple[str, str]:
    if email_type != "pro" or "@" not in email:
        return "", ""
    domain = email.split("@", 1)[1].strip().lower()
    website = f"https://{domain}"
    # Take main label before TLD
    labels = domain.split(".")
    core = labels[-2] if len(labels) >= 2 else labels[0]
    if core in ("co", "com", "org", "net", "gouv", "gov") and len(labels) >= 3:
        core = labels[-3]
    company = " ".join(w.capitalize() for w in re.split(r"[-_]+", core) if w)
    return company, website


def normalize_phone(phone: str) -> str:
    p = (phone or "").strip()
    if not p:
        return ""
    # Preserve leading +
    has_plus = p.startswith("+") or p.startswith("00")
    digits = re.sub(r"\D+", "", p)
    if not digits:
        return p
    if p.startswith("00") and len(digits) > 4:
        return "+" + digits[2:]
    if has_plus:
        return "+" + digits
    return p


def deduce_country(email: str, phone: str, email_type: str) -> str:
    norm_p = normalize_phone(phone)
    for pref, country in PHONE_PREFIX_COUNTRY:
        if norm_p.startswith(pref):
            return country
    if email_type == "pro" and "@" in email:
        tld = email.rsplit(".", 1)[-1].lower()
        if tld in TLD_COUNTRY:
            return TLD_COUNTRY[tld]
    return ""


def compute_lead_score(contact: Dict[str, Any]) -> int:
    if (contact.get("status") or "").lower() == "unsubscribed":
        return 0

    score = 0
    etype = (contact.get("email_type") or "").lower()
    if etype == "pro":
        score += 30
    elif etype == "perso":
        score += 15
    else:
        score += 5

    mx = (contact.get("mx_status") or "").lower()
    if mx == "valid":
        score += 20
    elif mx == "invalid":
        score -= 20

    if (contact.get("full_name") or "").strip():
        score += 12
    if (contact.get("phone") or "").strip():
        score += 13
    if (contact.get("company") or "").strip():
        score += 10

    role = (contact.get("role") or "").lower()
    if role:
        if any(w in role for w in DECISION_MAKER_WORDS):
            score += 15
        else:
            score += 5

    return max(0, min(100, score))


def run_auto_enrichment() -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    enriched_names = 0
    enriched_companies = 0
    enriched_countries = 0
    scored = 0

    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM contacts").fetchall()]
        updates = []

        for c in rows:
            email = c["email"]
            etype = db.classify_email(email)
            first = (c["first_name"] or "").strip()
            last = (c["last_name"] or "").strip()
            full = (c["full_name"] or "").strip()
            company = (c["company"] or "").strip()
            website = (c.get("website") or "").strip()
            phone = normalize_phone(c["phone"] or "")
            country = (c.get("country") or "").strip()

            # Clean ALL CAPS names
            if full and full.isupper() and len(full) > 3:
                full = full.title()
            if first and first.isupper() and len(first) > 2:
                first = first.title()
            if last and last.isupper() and len(last) > 2:
                last = last.title()

            if not full:
                df, dl, dfull = deduce_name_from_email(email, etype)
                if dfull:
                    first = first or df
                    last = last or dl
                    full = dfull
                    enriched_names += 1

            if etype == "pro":
                dcomp, dweb = deduce_company_and_website(email, etype)
                if not company and dcomp:
                    company = dcomp
                    enriched_companies += 1
                if not website and dweb:
                    website = dweb

            if not country:
                dc = deduce_country(email, phone, etype)
                if dc:
                    country = dc
                    enriched_countries += 1

            c_updated = dict(c)
            c_updated.update(
                {
                    "email_type": etype,
                    "first_name": first,
                    "last_name": last,
                    "full_name": full,
                    "company": company,
                    "website": website,
                    "phone": phone,
                    "country": country,
                }
            )
            score = compute_lead_score(c_updated)
            scored += 1

            updates.append(
                (
                    first, last, full, phone, company, website,
                    country, etype, score, now, c["id"],
                )
            )

        conn.executemany(
            """
            UPDATE contacts SET
                first_name = ?, last_name = ?, full_name = ?, phone = ?,
                company = ?, website = ?, country = ?, email_type = ?,
                lead_score = ?, updated_at = ?
            WHERE id = ?
            """,
            updates,
        )

    return {
        "total_processed": scored,
        "enriched_names": enriched_names,
        "enriched_companies": enriched_companies,
        "enriched_countries": enriched_countries,
    }


# ---------------- WEB DOMAIN ENRICHMENT ----------------


def _fetch_domain_meta(domain: str) -> Dict[str, str]:
    url = f"https://{domain}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ContactsHub/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4.5) as resp:
            raw = resp.read(65536).decode("utf-8", errors="ignore")
    except Exception:
        return {}

    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip())[:140] if title_m else ""

    desc_m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if not desc_m:
        desc_m = re.search(
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
            raw,
            re.IGNORECASE | re.DOTALL,
        )
    desc = html.unescape(re.sub(r"\s+", " ", desc_m.group(1)).strip())[:240] if desc_m else ""

    combined = f"{title} {desc}".lower()
    industry = ""
    for ind_name, keywords in INDUSTRY_KEYWORDS:
        if any(kw in combined for kw in keywords):
            industry = ind_name
            break

    return {"title": title, "desc": desc, "industry": industry, "website": url}


def run_web_enrichment(limit_domains: int = 40) -> Dict[str, Any]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT LOWER(SUBSTR(email, INSTR(email, '@') + 1)) AS dom
            FROM contacts
            WHERE email_type = 'pro' AND (industry = '' OR company_desc = '')
            LIMIT ?
            """,
            (limit_domains,),
        ).fetchall()
    domains = [r["dom"] for r in rows if r["dom"] and "." in r["dom"]]

    results: Dict[str, Dict[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_fetch_domain_meta, d): d for d in domains}
        for fut in concurrent.futures.as_completed(futs):
            d = futs[fut]
            try:
                meta = fut.result()
                if meta:
                    results[d] = meta
            except Exception:
                pass

    now = datetime.now().isoformat(timespec="seconds")
    contacts_updated = 0
    with db.get_conn() as conn:
        for dom, meta in results.items():
            cur = conn.execute(
                """
                UPDATE contacts SET
                    website = CASE WHEN website = '' THEN ? ELSE website END,
                    industry = CASE WHEN industry = '' THEN ? ELSE industry END,
                    company_desc = CASE WHEN company_desc = '' THEN ? ELSE company_desc END,
                    updated_at = ?
                WHERE email_type = 'pro' AND LOWER(email) LIKE ?
                """,
                (
                    meta.get("website", ""),
                    meta.get("industry", ""),
                    meta.get("desc") or meta.get("title") or "",
                    now,
                    f"%@{dom}",
                ),
            )
            contacts_updated += cur.rowcount

    return {
        "domains_scanned": len(domains),
        "domains_enriched": len(results),
        "contacts_updated": contacts_updated,
    }


# ---------------- NATURAL LANGUAGE SEARCH ----------------


def parse_natural_query(prompt: str) -> Dict[str, Any]:
    text = (prompt or "").strip().lower()
    filters: Dict[str, Any] = {}
    badges: List[str] = []

    if any(w in text for w in ["pro ", "professionnel", "entreprise", "société", "b2b", " pro"]):
        filters["email_type"] = "pro"
        badges.append("Type: Professionnel")
    elif any(w in text for w in ["perso", "personnel", "gmail", "yahoo", "particulier"]):
        filters["email_type"] = "perso"
        badges.append("Type: Personnel")
    elif any(w in text for w in ["générique", "generique", "noreply", "info@", "contact@"]):
        filters["email_type"] = "generic"
        badges.append("Type: Générique")

    if any(w in text for w in ["invalide", "cassé", "mort", "bounce"]):
        filters["mx_status"] = "invalid"
        badges.append("MX: Invalide")
    elif any(w in text for w in ["valide", "vérifié", "verifie", "actif"]):
        filters["mx_status"] = "valid"
        badges.append("MX: Valide")

    if any(w in text for w in ["jamais contacté", "jamais contacte", "nouveau", "nouveaux"]):
        filters["status"] = "new"
        badges.append("Statut: Nouveau")
    elif any(w in text for w in ["relancer", "relance"]):
        filters["status"] = "followup"
        badges.append("Statut: À relancer")
    elif any(w in text for w in ["client", "clients"]):
        filters["status"] = "client"
        badges.append("Statut: Client")
    elif any(w in text for w in ["désinscrit", "desinscrit", "liste noire", "ne plus contacter"]):
        filters["status"] = "unsubscribed"
        badges.append("Statut: Ne plus contacter")
    elif any(w in text for w in ["déjà contacté", "deja contacte", "contactés"]):
        filters["status"] = "contacted"
        badges.append("Statut: Contacté")

    if any(w in text for w in ["téléphone", "telephone", "tel ", "numéro", "numero", "whatsapp"]):
        filters["has_phone"] = True
        badges.append("Avec téléphone")

    if any(w in text for w in ["google drive", "drive"]):
        filters["source_type"] = "gdrive"
        badges.append("Source: Google Drive")
    elif any(w in text for w in ["ordinateur", " pc", "pc ", "local"]):
        filters["source_type"] = "pc"
        badges.append("Source: PC")

    # Score detection
    score_m = re.search(r"score\s*(?:>=|>|supérieur à|superieur a|de)?\s*(\d{1,3})", text)
    if score_m:
        filters["min_score"] = int(score_m.group(1))
        badges.append(f"Score ≥ {filters['min_score']}")
    elif any(w in text for w in ["meilleur", "top ", "chaud", "hot", "qualifié", "qualifie"]):
        filters["min_score"] = 60
        filters["sort_by"] = "score_desc"
        badges.append("Score ≥ 60 (Meilleurs leads)")

    # Country detection
    for country in set(TLD_COUNTRY.values()):
        if country.lower() in text:
            filters["country"] = country
            badges.append(f"Pays: {country}")
            break

    # Domain detection (e.g. @gmail.com)
    dom_m = re.search(r"@([a-z0-9.\-]+\.[a-z]{2,})", text)
    if dom_m:
        filters["domain"] = dom_m.group(1)
        badges.append(f"Domaine: @{filters['domain']}")

    # Fallback free-text keyword if no structured filter matched
    if not filters and prompt.strip():
        filters["q"] = prompt.strip()
        badges.append(f'Mot-clé: "{prompt.strip()}"')

    return {"filters": filters, "badges": badges}
