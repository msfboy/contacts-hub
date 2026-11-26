# Déploiement SaaS de ContactsHub sur Railway

Ce guide déploie **ContactsHub** (Flask + Gunicorn + `wa-bridge` WhatsApp QR + Volume persistant) sur **Railway** en ~5 minutes.

---

## Prérequis
- Un compte **GitHub** (avec ce dépôt poussé en **privé**).
- Un compte **Railway** (https://railway.com).

> **Important :** Vérifiez que `contacts.db`, `.contactshub_secrets.json` et `wa-bridge/auth_info/` ne sont **jamais** commités sur GitHub (ils sont déjà dans `.gitignore` et `.dockerignore`).

---

## Étape 1 — Créer le projet Railway
1. Dans Railway, cliquez sur **New Project** → **Deploy from GitHub Repo**.
2. Sélectionnez votre dépôt `contacts-hub`.
3. Railway détecte automatiquement `railway.json` et le `Dockerfile` (Python 3.12 + Node.js 20).

---

## Étape 2 — Attacher un Volume Persistant (OBLIGATOIRE)
Sans volume, la base SQLite et la session WhatsApp seraient effacées à chaque redéploiement.
1. Cliquez sur votre service → onglet **Volumes** → **Add Volume**.
2. **Mount Path :** `/data`
3. Validez (1 Go suffit largement pour démarrer).

---

## Étape 3 — Configurer les Variables d'environnement
Dans votre service → onglet **Variables** → **Raw Editor**, collez et personnalisez les variables de `.env.example` :

```env
CONTACTSHUB_SAAS_MODE=1
CONTACTSHUB_HTTPS=1
CONTACTSHUB_DATA_DIR=/data
FLASK_SECRET_KEY=<clé hex 64 caractères>
CONTACTSHUB_MASTER_KEY=<clé hex 64 caractères>
WA_BRIDGE_SECRET=<clé hex 64 caractères>
CONTACTSHUB_ADMIN_USER=admin
CONTACTSHUB_ADMIN_PASSWORD=<mot de passe fort>
```

Pour générer 3 clés aléatoires en local :
```bash
python -c "import secrets; [print(secrets.token_hex(32)) for _ in range(3)]"
```

---

## Étape 4 — Activer le domaine public HTTPS
1. Onglet **Settings** → **Networking** → **Generate Domain** (ou ajoutez votre domaine personnalisé, ex. `crm.votreclient.com`).
2. Vérifiez que `/healthz` répond `{"status": "ok", "mode": "saas"}`.
3. Connectez-vous sur `/login` avec `CONTACTSHUB_ADMIN_USER` et `CONTACTSHUB_ADMIN_PASSWORD`.
4. Dans **Administration → Marque Blanche**, renseignez votre URL publique (`https://votre-domaine.up.railway.app`) afin que tous les liens `/rdv`, `/f/<slug>`, `/receipt/` et le tracking email utilisent le bon domaine.

---

## Modèle multi-clients SaaS sur Railway (1 client = 1 service isolé)
Pour vendre à un **nouveau client** en toute sécurité :
1. Dupliquez le service dans Railway (ou créez un nouveau projet à partir du même repo GitHub).
2. Attachez-lui son propre Volume `/data` et ses propres clés (`FLASK_SECRET_KEY`, `CONTACTSHUB_MASTER_KEY`, `WA_BRIDGE_SECRET`).
3. Assignez-lui son sous-domaine (`client2.votredomaine.com`).
→ **Isolation totale** (base, WhatsApp, marque blanche, facturation), zéro risque de fuite entre clients, et mise à jour de tous vos clients en un seul `git push` !
