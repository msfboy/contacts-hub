"""No-code 'If -> Then' Workflow Automation Engine for ContactsHub."""

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List

import db
import wa_client

VALID_TRIGGERS = ("form_submitted", "payment_received", "booking_created", "contact_hot")
VALID_ACTIONS = ("add_tag", "create_task", "update_status", "send_whatsapp")


def init_default_rules() -> None:
    with db.get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM automation_rules").fetchone()[0]
        if cnt > 0:
            return
        now = datetime.now().isoformat(timespec="seconds")
        defaults = [
            (
                "🔥 Lead Chaud → Tâche d'appel prioritaire",
                1,
                "contact_hot",
                json.dumps({"min_score": 80}),
                "create_task",
                json.dumps({"title": "📞 Appeler ce prospect chaud ({nom})", "due_days": 0}),
                now,
            ),
            (
                "💰 Paiement reçu → Tag Client VIP",
                1,
                "payment_received",
                json.dumps({}),
                "add_tag",
                json.dumps({"tag": "Client VIP"}),
                now,
            ),
            (
                "🎓 Nouvelle inscription → Tâche vérification dossier",
                1,
                "form_submitted",
                json.dumps({}),
                "create_task",
                json.dumps({"title": "✅ Vérifier inscription & paiement de {nom}", "due_days": 1}),
                now,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO automation_rules (name, enabled, trigger_event, condition_json, action_type, action_config_json, runs_count, last_run_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, '', ?)
            """,
            defaults,
        )


def list_rules() -> List[Dict[str, Any]]:
    init_default_rules()
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM automation_rules ORDER BY id DESC").fetchall()]
    for r in rows:
        try:
            r["condition"] = json.loads(r.get("condition_json") or "{}")
        except Exception:
            r["condition"] = {}
        try:
            r["action_config"] = json.loads(r.get("action_config_json") or "{}")
        except Exception:
            r["action_config"] = {}
    return rows


def create_rule(data: Dict[str, Any]) -> Dict[str, Any]:
    name = (data.get("name") or "").strip()[:120]
    trigger = (data.get("trigger_event") or "").strip()
    action = (data.get("action_type") or "").strip()
    if not name or trigger not in VALID_TRIGGERS or action not in VALID_ACTIONS:
        return {"ok": False, "error": "Nom, déclencheur et action valides requis."}
    cond = data.get("condition") or {}
    act_cfg = data.get("action_config") or {}
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO automation_rules (name, enabled, trigger_event, condition_json, action_type, action_config_json, runs_count, last_run_at, created_at)
            VALUES (?, 1, ?, ?, ?, ?, 0, '', ?)
            """,
            (name, trigger, json.dumps(cond, ensure_ascii=False), action, json.dumps(act_cfg, ensure_ascii=False), now),
        )
        rid = cur.lastrowid
    return {"ok": True, "id": rid}


def toggle_rule(rid: int, enabled: bool) -> Dict[str, Any]:
    with db.get_conn() as conn:
        conn.execute("UPDATE automation_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rid))
    return {"ok": True}


def delete_rule(rid: int) -> Dict[str, Any]:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM automation_rules WHERE id = ?", (rid,))
    return {"ok": True}


def _render(tpl: str, ctx: Dict[str, Any]) -> str:
    out = str(tpl or "")
    for k, v in {
        "{nom}": ctx.get("full_name") or ctx.get("email") or "Client",
        "{prenom}": (ctx.get("full_name") or "Client").split(" ")[0],
        "{email}": ctx.get("email") or "",
        "{phone}": ctx.get("phone") or "",
        "{formation}": ctx.get("course_name") or ctx.get("source") or "",
    }.items():
        out = out.replace(k, str(v))
    return out


def fire_event(trigger_event: str, context: Dict[str, Any]) -> int:
    """Evaluate and execute all enabled automation rules matching trigger_event."""
    if trigger_event not in VALID_TRIGGERS:
        return 0
    try:
        init_default_rules()
    except Exception:
        return 0

    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    executed = 0

    with db.get_conn() as conn:
        rules = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM automation_rules WHERE enabled = 1 AND trigger_event = ?",
                (trigger_event,),
            ).fetchall()
        ]

    cid = context.get("contact_id")
    for r in rules:
        try:
            cond = json.loads(r.get("condition_json") or "{}")
            act_cfg = json.loads(r.get("action_config_json") or "{}")
        except Exception:
            continue

        min_score = int(cond.get("min_score") or 0)
        if min_score > 0 and int(context.get("lead_score") or 0) < min_score:
            continue

        action = r["action_type"]
        did_run = False

        if action == "add_tag" and cid and act_cfg.get("tag"):
            with db.get_conn() as conn:
                row = conn.execute("SELECT tags FROM contacts WHERE id = ?", (cid,)).fetchone()
                if row:
                    merged = db.merge_tags(row["tags"] or "", str(act_cfg["tag"]))
                    conn.execute("UPDATE contacts SET tags = ?, updated_at = ? WHERE id = ?", (merged, now_iso, cid))
                    did_run = True

        elif action == "update_status" and cid and act_cfg.get("status") in db.VALID_STATUSES:
            with db.get_conn() as conn:
                conn.execute("UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?", (act_cfg["status"], now_iso, cid))
                did_run = True

        elif action == "create_task" and cid:
            title = _render(act_cfg.get("title") or f"Action auto ({r['name']})", context)
            due_days = int(act_cfg.get("due_days") or 0)
            due_str = (now + timedelta(days=due_days)).strftime("%Y-%m-%d")
            with db.get_conn() as conn:
                conn.execute(
                    "INSERT INTO contact_tasks (contact_id, title, due_date, done, created_at) VALUES (?, ?, ?, 0, ?)",
                    (cid, title[:200], due_str, now_iso),
                )
                did_run = True

        elif action == "send_whatsapp" and context.get("phone") and act_cfg.get("message"):
            msg = _render(act_cfg["message"], context)
            phone = str(context["phone"])

            def _bg_send(p=phone, text=msg):
                try:
                    if wa_client.is_bridge_alive():
                        wa_client.bridge_post("/send", {"phone": p, "text": text}, timeout=12.0)
                except Exception:
                    pass

            threading.Thread(target=_bg_send, daemon=True).start()
            did_run = True

        if did_run:
            executed += 1
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE automation_rules SET runs_count = runs_count + 1, last_run_at = ? WHERE id = ?",
                    (now_iso, r["id"]),
                )

    return executed
