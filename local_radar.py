import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

import db
import prospecting
import wa_client

# Built-in West Africa & popular cities bounding boxes (south, west, north, east) for fast OSM queries
CITY_BBOX_CACHE: Dict[str, Tuple[float, float, float, float]] = {
    "lome": (6.09, 1.13, 6.26, 1.33),
    "lomé": (6.09, 1.13, 6.26, 1.33),
    "kara": (9.50, 1.13, 9.60, 1.24),
    "sokode": (8.94, 1.10, 9.03, 1.18),
    "sokodé": (8.94, 1.10, 9.03, 1.18),
    "cotonou": (6.33, 2.34, 6.42, 2.48),
    "accra": (5.51, -0.30, 5.71, -0.08),
    "abidjan": (5.24, -4.12, 5.45, -3.88),
    "dakar": (14.64, -17.53, 14.79, -17.36),
    "paris": (48.81, 2.25, 48.90, 2.42),
}

# Mapping French/English business keywords to OpenStreetMap tags
OSM_CATEGORY_TAGS: Dict[str, List[Tuple[str, str]]] = {
    "hotel": [("tourism", "hotel"), ("tourism", "guest_house"), ("tourism", "motel")],
    "hôtel": [("tourism", "hotel"), ("tourism", "guest_house"), ("tourism", "motel")],
    "banque": [("amenity", "bank"), ("office", "financial")],
    "bank": [("amenity", "bank"), ("office", "financial")],
    "microfinance": [("amenity", "bank"), ("office", "financial")],
    "ecole": [("amenity", "school"), ("amenity", "college"), ("amenity", "university")],
    "école": [("amenity", "school"), ("amenity", "college"), ("amenity", "university")],
    "school": [("amenity", "school"), ("amenity", "college"), ("amenity", "university")],
    "universite": [("amenity", "university"), ("amenity", "college")],
    "clinique": [("amenity", "clinic"), ("amenity", "hospital"), ("amenity", "doctors")],
    "hopital": [("amenity", "hospital"), ("amenity", "clinic")],
    "pharmacie": [("amenity", "pharmacy")],
    "restaurant": [("amenity", "restaurant"), ("amenity", "cafe")],
    "logistique": [("office", "logistics"), ("office", "company"), ("office", "shipping")],
    "transit": [("office", "logistics"), ("office", "company")],
    "ong": [("office", "ngo"), ("office", "association")],
    "cabinet": [("office", "lawyer"), ("office", "accountant"), ("office", "consulting"), ("office", "company")],
    "entreprise": [("office", "company"), ("office", "commercial"), ("office", "it"), ("office", "insurance")],
    "assurance": [("office", "insurance"), ("office", "financial")],
    "voyage": [("shop", "travel_agency"), ("office", "travel_agent")],
    "supermarche": [("shop", "supermarket"), ("shop", "mall")],
}


def get_gmaps_key() -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'google_maps_api_key'").fetchone()
        return db.decrypt_secret(row["value"]) if row and row["value"] else ""


def save_gmaps_key(key: str) -> None:
    enc = db.encrypt_secret(key.strip()) if key.strip() else ""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('google_maps_api_key', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (enc,),
        )


def _resolve_city_bbox(city: str) -> Optional[Tuple[float, float, float, float]]:
    c_low = (city or "Lomé").strip().lower().split(",")[0].strip()
    if c_low in CITY_BBOX_CACHE:
        return CITY_BBOX_CACHE[c_low]
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": "ContactsHub-LocalRadar/1.0"},
            timeout=8,
        )
        arr = r.json()
        if arr and "boundingbox" in arr[0]:
            s, n, w, e = [float(x) for x in arr[0]["boundingbox"]]
            return (s, w, n, e)
    except Exception:
        pass
    return CITY_BBOX_CACHE["lome"]


def search_openstreetmap(keyword: str, city: str = "Lomé, Togo", limit: int = 40) -> Dict[str, Any]:
    bbox = _resolve_city_bbox(city)
    if not bbox:
        return {"ok": False, "error": f"Ville introuvable : {city}", "items": []}
    s, w, n, e = bbox
    kw = (keyword or "entreprise").strip().lower()

    tag_pairs: List[Tuple[str, str]] = []
    for k, pairs in OSM_CATEGORY_TAGS.items():
        if k in kw:
            tag_pairs.extend(pairs)

    query_parts = []
    if tag_pairs:
        seen_p = set()
        for tk, tv in tag_pairs:
            if (tk, tv) in seen_p:
                continue
            seen_p.add((tk, tv))
            query_parts.append(f'node["{tk}"="{tv}"]({s},{w},{n},{e});')
            query_parts.append(f'way["{tk}"="{tv}"]({s},{w},{n},{e});')
    else:
        safe_kw = re.sub(r'["\\]', "", kw)
        query_parts.append(f'node["name"~"{safe_kw}",i]({s},{w},{n},{e});')
        query_parts.append(f'way["name"~"{safe_kw}",i]({s},{w},{n},{e});')

    overpass_ql = f"[out:json][timeout:20];(\n{''.join(query_parts)}\n);out center {min(limit * 2, 120)};"

    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    data = None
    last_err = ""
    for ep in endpoints:
        try:
            r = requests.post(
                ep,
                data={"data": overpass_ql},
                headers={"User-Agent": "ContactsHub-LocalRadar/1.0"},
                timeout=22,
            )
            if r.status_code == 200:
                data = r.json()
                break
            last_err = f"Overpass HTTP {r.status_code}"
        except Exception as exc:
            last_err = str(exc)

    if data is None:
        return {"ok": False, "error": f"Serveur OpenStreetMap temporairement occupé ({last_err}). Réessayez dans quelques secondes.", "items": []}

    items: List[Dict[str, Any]] = []
    seen_names = set()
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or tags.get("brand") or "").strip()
        if not name:
            continue
        nk = name.lower()
        if nk in seen_names:
            continue
        seen_names.add(nk)

        phone = (tags.get("contact:phone") or tags.get("phone") or tags.get("contact:mobile") or "").strip()
        email = (tags.get("contact:email") or tags.get("email") or "").strip().lower()
        website = (tags.get("contact:website") or tags.get("website") or tags.get("url") or "").strip()
        if website and not website.startswith(("http://", "https://")):
            website = "https://" + website
        street = tags.get("addr:street") or ""
        suburb = tags.get("addr:suburb") or tags.get("addr:quarter") or ""
        addr = ", ".join([x for x in [street, suburb, city] if x])
        cat = tags.get("office") or tags.get("tourism") or tags.get("amenity") or tags.get("shop") or keyword

        items.append({
            "name": name,
            "category": cat,
            "phone": phone,
            "email": email,
            "website": website,
            "address": addr,
            "city": city,
            "source": "openstreetmap",
        })
        if len(items) >= limit:
            break

    # Sort businesses with phone/email/website first
    items.sort(key=lambda x: (bool(x["email"]), bool(x["phone"]), bool(x["website"])), reverse=True)
    return {"ok": True, "engine": "openstreetmap", "count": len(items), "items": items}


def search_google_maps(keyword: str, city: str = "Lomé, Togo", limit: int = 20) -> Dict[str, Any]:
    api_key = get_gmaps_key()
    if not api_key:
        return {"ok": False, "error": "Aucune clé API Google Maps configurée. Utilisez le mode gratuit OpenStreetMap ou ajoutez votre clé.", "items": []}
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": f"{keyword} in {city}", "key": api_key, "language": "fr"},
            timeout=12,
        )
        data = r.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            return {"ok": False, "error": data.get("error_message") or f"Google Places: {data.get('status')}", "items": []}
        items = []
        for p in (data.get("results") or [])[:limit]:
            pid = p.get("place_id")
            phone = ""
            website = ""
            if pid:
                try:
                    dr = requests.get(
                        "https://maps.googleapis.com/maps/api/place/details/json",
                        params={"place_id": pid, "fields": "formatted_phone_number,international_phone_number,website", "key": api_key},
                        timeout=6,
                    )
                    det = dr.json().get("result") or {}
                    phone = det.get("international_phone_number") or det.get("formatted_phone_number") or ""
                    website = det.get("website") or ""
                except Exception:
                    pass
            items.append({
                "name": p.get("name") or "",
                "category": keyword,
                "phone": phone,
                "email": "",
                "website": website,
                "address": p.get("formatted_address") or city,
                "city": city,
                "rating": p.get("rating"),
                "source": "google_maps",
            })
        return {"ok": True, "engine": "google_maps", "count": len(items), "items": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "items": []}


def search_local_businesses(keyword: str, city: str = "Lomé, Togo", engine: str = "osm", enrich_emails: bool = False, limit: int = 35) -> Dict[str, Any]:
    if engine == "google":
        res = search_google_maps(keyword, city, limit=min(limit, 20))
    else:
        res = search_openstreetmap(keyword, city, limit=limit)

    if not res.get("ok"):
        return res

    items = res.get("items") or []
    if enrich_emails and items:
        urls = [it["website"] for it in items if it.get("website") and not it.get("email")][:12]
        if urls:
            try:
                crawled = prospecting.crawl_company_websites(urls, max_sites=12)
                by_domain: Dict[str, str] = {}
                for c in crawled.get("items") or []:
                    dom = (c.get("company") or c.get("website") or "").lower()
                    em = (c.get("email") or "").strip().lower()
                    if em and dom and dom not in by_domain:
                        by_domain[dom] = em
                for it in items:
                    if not it.get("email") and it.get("website"):
                        w_low = it["website"].lower()
                        for c in crawled.get("items") or []:
                            cw = (c.get("website") or "").lower()
                            if cw and (cw in w_low or w_low in cw):
                                it["email"] = c.get("email") or ""
                                if not it.get("phone") and c.get("phone"):
                                    it["phone"] = c["phone"]
                                break
            except Exception:
                pass
    return res


def import_radar_businesses(items: List[Dict[str, Any]], tag: str = "Radar B2B", workspace_id: int = 1) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    records = []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        email = (it.get("email") or "").strip().lower()
        phone = wa_client.format_plus_phone(it.get("phone") or "") if it.get("phone") else ""
        if not email:
            digits = wa_client.normalize_digits(phone)
            slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")[:28] or "biz"
            email = f"{slug}.{digits[-4:] if digits else 'b2b'}@radar.local"
        records.append({
            "email": email,
            "full_name": name,
            "company": name,
            "phone": phone,
            "website": (it.get("website") or "").strip(),
            "country": (it.get("city") or "Togo").strip(),
            "industry": (it.get("category") or "B2B").strip(),
            "notes": f"Adresse : {it.get('address') or ''} (Source: {it.get('source') or 'OpenStreetMap'})".strip(),
            "status": "new",
        })
    if not records:
        return {"ok": False, "error": "Aucune entreprise valide sélectionnée."}

    summary = db.upsert_contacts(
        records=records,
        source_type="maps_radar",
        source_name=f"Radar Local ({tag})",
        source_ref="local_radar",
        extra_tag=tag or "Radar B2B",
    )
    with db.get_conn() as conn:
        for r in records:
            conn.execute(
                "UPDATE contacts SET lead_score = MAX(lead_score, 65), workspace_id = ?, updated_at = ? WHERE email = ?",
                (workspace_id, now, r["email"]),
            )
    return {"ok": True, "inserted": summary.get("inserted", 0), "updated": summary.get("updated", 0)}
