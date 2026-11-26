import concurrent.futures
import html
import re
import urllib.parse
from typing import Any, Dict, List

import connectors
import enrichment
import net_guard
import parsers


def _fetch_html(url: str, timeout: float = 5.0) -> str:
    try:
        body, _ = net_guard.safe_fetch_text(
            url,
            timeout=timeout,
            max_bytes=120000,
            user_agent="Mozilla/5.0 (compatible; ContactsHub-Prospector/1.0)",
            require_text_content=True,
        )
        return body
    except Exception:
        return ""


def _normalize_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _extract_from_single_site(raw_url: str) -> List[Dict[str, Any]]:
    base_url = _normalize_url(raw_url)
    if not base_url:
        return []

    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc.lower().lstrip("www.")

    pages = [base_url]
    # Also try common contact pages
    home_html = _fetch_html(base_url)
    if home_html:
        contact_links = re.findall(
            r'href=["\']([^"\']*(?:contact|a-propos|about|equipe|team)[^"\']*)["\']',
            home_html,
            re.IGNORECASE,
        )
        seen_urls = {base_url}
        for href in contact_links:
            full = urllib.parse.urljoin(origin, href)
            if urllib.parse.urlparse(full).netloc.lower().lstrip("www.") == domain and full not in seen_urls:
                seen_urls.add(full)
                pages.append(full)
                if len(pages) >= 3:
                    break

    combined_html = home_html
    for extra in pages[1:]:
        combined_html += "\n" + _fetch_html(extra)

    if not combined_html:
        return []

    # Title & industry
    title_m = re.search(r"<title[^>]*>(.*?)</title>", home_html, re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip())[:90] if title_m else domain
    company_name = re.split(r"\s*[\-|–|•|:]\s*", title)[0].strip() or domain

    low_all = combined_html[:25000].lower()
    industry = ""
    for ind_name, kws in enrichment.INDUSTRY_KEYWORDS:
        if any(kw in low_all for kw in kws):
            industry = ind_name
            break

    # Strip tags for clean regex scanning
    clean_text = re.sub(r"<script[^>]*>.*?</script>", " ", combined_html, flags=re.IGNORECASE | re.DOTALL)
    clean_text = re.sub(r"<style[^>]*>.*?</style>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
    clean_text = html.unescape(re.sub(r"<[^>]+>", " ", clean_text))

    emails_found = parsers._extract_emails_from_text(clean_text)
    # Also check mailto: links
    for m_em in re.findall(r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", combined_html, re.IGNORECASE):
        em = m_em.strip().lower()
        if parsers._is_valid_email(em) and not any(x["email"] == em for x in emails_found):
            emails_found.append({"email": em})

    phones = connectors.PHONE_REGEX.findall(clean_text)
    phone = phones[0].strip() if phones else ""

    results: List[Dict[str, Any]] = []
    for rec in emails_found[:10]:
        results.append(
            {
                "email": rec["email"],
                "full_name": rec.get("full_name") or "",
                "phone": phone,
                "company": company_name,
                "website": origin,
                "industry": industry,
                "notes": f"Extrait depuis {origin}",
            }
        )
    return results


def crawl_company_websites(urls: List[str], max_sites: int = 25) -> Dict[str, Any]:
    clean_urls = [u.strip() for u in urls if u and u.strip()][:max_sites]
    all_prospects: Dict[str, Dict[str, Any]] = {}
    scanned = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_extract_from_single_site, u): u for u in clean_urls}
        for fut in concurrent.futures.as_completed(futs):
            scanned += 1
            try:
                for p in fut.result():
                    if p["email"] not in all_prospects:
                        all_prospects[p["email"]] = p
            except Exception:
                pass

    return {
        "sites_scanned": scanned,
        "prospects": list(all_prospects.values()),
    }


# ---------------- SALES COPYWRITING GENERATOR ----------------


def generate_sales_copy(
    offer_type: str,
    channel: str,
    tone: str,
    business_name: str,
    booking_url: str = "",
    payment_url: str = "",
) -> Dict[str, str]:
    biz = business_name.strip() or "Notre Centre de Formation en Anglais"
    cta_line = ""
    if booking_url:
        cta_line += f"\n👉 Réserver votre créneau gratuit ici : {booking_url}"
    if payment_url:
        cta_line += f"\n💳 Lien d'inscription directe : {payment_url}"

    if offer_type == "b2b_english":
        if channel == "whatsapp":
            return {
                "subject": "Formation Anglais Pro — {entreprise}",
                "body": (
                    f"Bonjour {{prenom}} 👋\n"
                    f"Je suis de {biz}. Nous aidons les équipes de {{entreprise}} à devenir opérationnelles en anglais professionnel (réunions, emails, clients internationaux) en quelques semaines.\n"
                    f"Souhaitez-vous recevoir notre programme entreprise + un test de niveau offert pour 3 collaborateurs ?{cta_line}"
                ),
            }
        return {
            "subject": "Anglais professionnel pour les équipes de {entreprise}",
            "body": (
                f"Bonjour {{prenom}},\n\n"
                f"De nombreuses entreprises perdent des opportunités internationales faute d'aisance en anglais au sein de leurs équipes.\n\n"
                f"Chez {biz}, nous proposons des formations sur mesure (présentiel ou en ligne) orientées 100% pratique : réunions, présentations, emails professionnels et préparation TOEIC.\n\n"
                f"Seriez-vous disponible 10 minutes cette semaine pour en discuter et offrir un diagnostic de niveau gratuit à votre équipe ?{cta_line}\n\n"
                f"Bien cordialement,\n{biz}"
            ),
        }

    if offer_type == "b2c_english":
        if channel == "whatsapp":
            return {
                "subject": "Nouvelle session d'anglais",
                "body": (
                    f"Bonjour {{prenom}} 👋\n"
                    f"Les inscriptions pour la nouvelle session d'anglais chez {biz} sont ouvertes (cours du soir, intensif ou 100% en ligne) !\n"
                    f"Faites votre test de niveau gratuit (2 min) pour connaître votre groupe idéal 👇{cta_line}"
                ),
            }
        return {
            "subject": "{prenom}, boostez votre anglais dès cette session",
            "body": (
                f"Bonjour {{prenom}},\n\n"
                f"Que ce soit pour décrocher un meilleur poste, réussir l'IELTS/TOEFL ou parler anglais avec confiance, notre nouvelle session chez {biz} démarre très bientôt.\n\n"
                f"✓ Petits groupes orientés pratique orale\n"
                f"✓ Horaires flexibles (présentiel ou en ligne)\n"
                f"✓ Test de niveau gratuit avant inscription\n"
                f"{cta_line}\n\n"
                f"Répondez simplement à cet email si vous souhaitez recevoir le programme et les tarifs.\n\n"
                f"À très vite,\n{biz}"
            ),
        }

    # Generic / SaaS / Service B2B
    if channel == "whatsapp":
        return {
            "subject": "Opportunité pour {entreprise}",
            "body": (
                f"Bonjour {{prenom}} 👋\n"
                f"Je vous contacte de la part de {biz}. Nous aidons des structures comme {{entreprise}} à gagner du temps et attirer plus de clients.\n"
                f"Puis-je vous montrer en 5 minutes comment cela peut vous aider ?{cta_line}"
            ),
        }
    return {
        "subject": "Une idée concrète pour {entreprise}",
        "body": (
            f"Bonjour {{prenom}},\n\n"
            f"Je me permets de vous contacter au nom de {biz}. Nous accompagnons des professionnels et entreprises comme {{entreprise}} pour accélérer leurs résultats.\n\n"
            f"Auriez-vous 10 minutes cette semaine pour un bref échange ?{cta_line}\n\n"
            f"Cordialement,\n{biz}"
        ),
    }
