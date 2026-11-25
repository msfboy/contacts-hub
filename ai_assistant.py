from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

import db


def get_ai_config(include_secret: bool = False) -> Dict[str, Any]:
    with db.get_conn() as conn:
        prov = conn.execute("SELECT value FROM app_settings WHERE key = 'ai_provider'").fetchone()
        key_row = conn.execute("SELECT value FROM app_settings WHERE key = 'ai_api_key'").fetchone()
        ctx_row = conn.execute("SELECT value FROM app_settings WHERE key = 'ai_business_context'").fetchone()

    provider = prov["value"] if prov and prov["value"] else "builtin"
    raw_key = db.decrypt_secret(key_row["value"]) if key_row and key_row["value"] else ""
    default_ctx = (
        "MSF'S English Learning Academy (Lomé — Carrefour Adroukpapé, Avedji Limosine). "
        "Formations intensives d'anglais (Débutant, Intermédiaire, Avancé, Anglais Pro Entreprises B2B, préparation IELTS/TOEFL). "
        "Sessions Matin (08h-11h), Midi (12h30-14h), Soir (18h-20h) et Samedi. "
        "Tarif indicatif : 30 000 FCFA + 5 000 FCFA inscription (paiement intégral ou en tranches via TMoney, Flooz ou Espèces). "
        "Test de niveau gratuit sur rendez-vous."
    )
    return {
        "provider": provider,
        "has_key": bool(raw_key),
        "api_key": raw_key if include_secret else (("•" * 10 + raw_key[-4:]) if len(raw_key) > 6 else ("•" * len(raw_key) if raw_key else "")),
        "business_context": ctx_row["value"] if ctx_row and ctx_row["value"].strip() else default_ctx,
    }


def save_ai_config(provider: str, api_key: str, business_context: str) -> Dict[str, Any]:
    existing = get_ai_config(include_secret=True)
    key_in = (api_key or "").strip()
    if key_in and not key_in.startswith("•"):
        enc_key = db.encrypt_secret(key_in)
    else:
        enc_key = db.encrypt_secret(existing.get("api_key") or "")

    with db.get_conn() as conn:
        for k, v in [
            ("ai_provider", (provider or "builtin").strip()),
            ("ai_api_key", enc_key),
            ("ai_business_context", (business_context or "").strip()),
        ]:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )
    return get_ai_config(include_secret=False)


def _builtin_smart_reply(contact_name: str, messages: List[Dict[str, Any]], goal: str, booking_url: str) -> str:
    first = (contact_name or "").strip().split(" ")[0] or ""
    greet = f"Bonjour {first} 👋" if first else "Bonjour 👋"

    last_in = ""
    for m in reversed(messages):
        if not m.get("from_me"):
            last_in = (m.get("body") or "").lower()
            break

    if goal == "relance":
        return (
            f"{greet}\n"
            f"Je reviens rapidement vers vous suite à notre dernier échange au sujet de votre formation d'anglais chez MSF'S English Learning Academy. "
            f"Souhaitez-vous réserver votre test de niveau gratuit cette semaine ? Vous pouvez choisir votre créneau ici : {booking_url}"
        )

    if any(w in last_in for w in ["prix", "tarif", "combien", "coût", "cout", "frais", "payer", "fcfa", "tranche"]):
        return (
            f"{greet}\n"
            f"Nos formations intensives d'anglais sont à 30 000 FCFA (+ 5 000 FCFA de frais d'inscription, soit 35 000 FCFA au total), "
            f"avec possibilité de régler en tranches via TMoney, Flooz ou en espèces.\n"
            f"Souhaitez-vous que je vous réserve un créneau pour le test de niveau gratuit afin de confirmer votre groupe ? 👉 {booking_url}"
        )

    if any(w in last_in for w in ["heure", "horaire", "session", "quand", "matin", "soir", "samedi", "jour", "emploi"]):
        return (
            f"{greet}\n"
            f"Nous proposons plusieurs sessions flexibles adaptées aux étudiants et professionnels :\n"
            f"• Matin : 08h00 – 11h00\n"
            f"• Midi : 12h30 – 14h00\n"
            f"• Soir : 18h00 – 20h00 (et Samedi)\n"
            f"Quelle plage horaire vous conviendrait le mieux ?"
        )

    if any(w in last_in for w in ["où", "ou ", "adresse", "situé", "situe", "lieu", "localisation", "avedji", "lomé", "lome"]):
        return (
            f"{greet}\n"
            f"MSF'S English Learning Academy est située à Lomé — Carrefour Adroukpapé, Avedji Limosine (nous proposons aussi des séances en ligne).\n"
            f"Vous pouvez réserver votre passage ou votre test de niveau gratuit directement ici : {booking_url}"
        )

    if any(w in last_in for w in ["entreprise", "société", "equipe", "équipe", "personnel", "b2b", "devis"]):
        return (
            f"{greet}\n"
            f"Nous accompagnons les entreprises et équipes professionnelles avec des programmes d'Anglais Pro sur mesure (audit de niveau inclus, horaires flexibles dans vos locaux ou en ligne).\n"
            f"Pouvons-nous planifier un bref échange de 15 min pour préparer une proposition adaptée à votre équipe ? 👉 {booking_url}"
        )

    if any(w in last_in for w in ["ielts", "toefl", "examen", "certificat", "débutant", "debutant", "niveau"]):
        return (
            f"{greet}\n"
            f"Excellente nouvelle : nous formons aussi bien les grands débutants que les candidats aux examens internationaux (IELTS / TOEFL), avec un suivi personnalisé.\n"
            f"Je vous invite à faire notre test de niveau gratuit pour vous placer dans le groupe idéal : {booking_url}"
        )

    if any(w in last_in for w in ["tmoney", "flooz", "momo", "paiement", "inscrire", "inscription", "ok", "d'accord", "oui", "intéressé", "interesse"]):
        return (
            f"{greet}\n"
            f"Parfait ! Pour finaliser votre inscription et bloquer votre place dans la prochaine session, "
            f"vous pouvez choisir votre créneau de test de niveau / validation ici : {booking_url}\n"
            f"Dès réception de votre règlement (TMoney, Flooz ou Espèces), nous vous envoyons votre reçu officiel directement ici sur WhatsApp. ✅"
        )

    return (
        f"{greet}\n"
        f"Merci pour votre message ! Chez MSF'S English Learning Academy, nous aidons nos apprenants à parler anglais avec assurance grâce à des sessions pratiques (Matin, Midi, Soir ou Samedi).\n"
        f"Quel est votre objectif principal (anglais professionnel, conversation, IELTS) ? Je peux aussi vous réserver un test de niveau gratuit ici : {booking_url}"
    )


def suggest_reply(
    contact_name: str,
    messages: List[Dict[str, Any]],
    goal: str = "repondre",
    booking_url: str = "http://127.0.0.1:5050/rdv",
) -> Dict[str, Any]:
    cfg = get_ai_config(include_secret=True)
    provider = cfg.get("provider") or "builtin"
    api_key = cfg.get("api_key") or ""
    biz_ctx = cfg.get("business_context") or ""

    transcript_lines = []
    for m in messages[-12:]:
        who = "Conseiller" if m.get("from_me") else (contact_name or "Prospect")
        transcript_lines.append(f"{who}: {m.get('body', '')}")
    transcript = "\n".join(transcript_lines) or "(Aucun message précédent)"

    prompt = (
        f"Tu es le conseiller commercial de l'organisation suivante :\n{biz_ctx}\n\n"
        f"Lien de prise de rendez-vous / test de niveau gratuit : {booking_url}\n"
        f"Objectif de la réponse : {goal} (répondre chaleureusement en français, court, naturel, style WhatsApp, orienté inscription ou prise de RDV, sans guillemets autour du message).\n\n"
        f"Nom du prospect : {contact_name or 'Prospect'}\n"
        f"Historique récent :\n{transcript}\n\n"
        f"Rédige uniquement le message WhatsApp prêt à envoyer :"
    )

    if provider == "gemini" and api_key:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15,
            )
            if r.status_code == 200:
                cands = r.json().get("candidates") or []
                if cands:
                    text = cands[0]["content"]["parts"][0]["text"].strip()
                    if text:
                        return {"ok": True, "engine": "gemini", "reply": text}
        except Exception:
            pass

    if provider == "openai" and api_key:
        try:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6,
                },
                timeout=15,
            )
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"].strip()
                if text:
                    return {"ok": True, "engine": "openai", "reply": text}
        except Exception:
            pass

    reply = _builtin_smart_reply(contact_name, messages, goal, booking_url)
    return {"ok": True, "engine": "builtin", "reply": reply}


def summarize_chat_to_crm(contact_id: int, contact_name: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not contact_id:
        return {"ok": False, "error": "Ce contact WhatsApp n'est pas encore relié à une fiche CRM."}
    if not messages:
        return {"ok": False, "error": "Aucun message à résumer."}

    in_msgs = [m["body"] for m in messages if not m.get("from_me")]
    out_msgs = [m["body"] for m in messages if m.get("from_me")]
    last_in = in_msgs[-1][:140] if in_msgs else "—"
    summary = f"🤖 Résumé WhatsApp ({len(messages)} msgs : {len(in_msgs)} reçus, {len(out_msgs)} envoyés) — Dernier message prospect : « {last_in} »"
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO contact_activities (contact_id, kind, content, created_at) VALUES (?, 'note', ?, ?)",
            (contact_id, summary, now),
        )
    return {"ok": True, "summary": summary}
