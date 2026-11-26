"""Network security helpers: anti-SSRF host/URL validation and safe HTTP fetching."""

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_global and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return False


def validate_public_host(host: str) -> str:
    """Resolve hostname and ensure every returned IP is publicly routable (anti-SSRF)."""
    h = (host or "").strip().rstrip(".")
    if not h or len(h) > 253:
        raise ValueError("Nom d'hôte invalide.")
    if h.lower() in ("localhost", "localhost.localdomain") or h.lower().endswith(".local"):
        raise ValueError("Accès aux hôtes locaux interdit.")

    # Direct IP literal check
    try:
        ip_obj = ipaddress.ip_address(h.strip("[]"))
        if not _is_public_ip(str(ip_obj)):
            raise ValueError("Adresse IP privée ou réservée interdite.")
        return h
    except ValueError as exc:
        if "interdite" in str(exc):
            raise

    try:
        infos = socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Impossible de résoudre l'hôte : {h}") from exc

    if not infos:
        raise ValueError(f"Aucune adresse IP trouvée pour : {h}")

    for info in infos:
        ip_addr = info[4][0]
        if not _is_public_ip(ip_addr):
            raise ValueError("Accès au réseau interne ou métadonnées cloud interdit.")

    return h


def validate_public_url(url: str) -> str:
    """Validate that URL uses http/https and points to a publicly routable host."""
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Seuls les schémas http:// et https:// sont autorisés.")
    if not parsed.hostname:
        raise ValueError("URL invalide : nom d'hôte manquant.")
    validate_public_host(parsed.hostname)
    return parsed.geturl()


def validate_http_url_syntax(url: str) -> str:
    """Syntax-only validation for redirect URLs (http/https or empty)."""
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("L'URL de redirection doit commencer par http:// ou https://.")
    return u


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        validate_public_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def safe_fetch_text(
    url: str,
    timeout: float = 5.0,
    max_bytes: int = 120000,
    user_agent: str = "Mozilla/5.0 (compatible; ContactsHub/1.0)",
    require_text_content: bool = True,
) -> Tuple[str, str]:
    """Fetch an HTTP/HTTPS URL safely with SSRF protection on initial URL and redirects."""
    clean_url = validate_public_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(clean_url, headers={"User-Agent": user_agent})
    with opener.open(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if require_text_content and ctype and ("text" not in ctype and "html" not in ctype):
            return "", ctype
        body = resp.read(max_bytes).decode("utf-8", errors="ignore")
        return body, ctype
