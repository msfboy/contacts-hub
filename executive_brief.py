"""Executive Daily Brief ('Presidential Brief') & strategic recommendations."""

from datetime import datetime
from typing import Any, Dict, List

import db
import payments_hub


def get_monthly_target(workspace_id: int = 1) -> float:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (f"monthly_revenue_target_ws_{workspace_id}",),
        ).fetchone()
    try:
        return max(0.0, float(row["value"])) if row and row["value"] else 1500000.0
    except (TypeError, ValueError):
        return 1500000.0


def set_monthly_target(amount: float, workspace_id: int = 1) -> float:
    val = max(0.0, float(amount or 0))
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"monthly_revenue_target_ws_{workspace_id}", str(val)),
        )
    return val


def generate_executive_brief(workspace_id: int = 1) -> Dict[str, Any]:
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")
    ws = db.get_workspace(workspace_id)
    currency = ws.get("currency") or "FCFA"
    monthly_target = get_monthly_target(workspace_id)

    with db.get_conn() as conn:
        total_contacts = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ?",
            (workspace_id,),
        ).fetchone()[0]
        contacted_or_more = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ? AND status IN ('contacted', 'followup', 'client')",
            (workspace_id,),
        ).fetchone()[0]
        clients_count = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE COALESCE(workspace_id, 1) = ? AND status = 'client'",
            (workspace_id,),
        ).fetchone()[0]
        hot_leads = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, full_name, email, phone, company, lead_score, status, deal_value
                FROM contacts
                WHERE COALESCE(workspace_id, 1) = ? AND status NOT IN ('client', 'unsubscribed')
                ORDER BY lead_score DESC, updated_at DESC
                LIMIT 5
                """,
                (workspace_id,),
            ).fetchall()
        ]
        month_revenue = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount_paid), 0) FROM student_payments WHERE COALESCE(workspace_id, 1) = ? AND paid_at LIKE ?",
                (workspace_id, f"{month_prefix}%"),
            ).fetchone()[0]
            or 0
        )
        total_revenue = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount_paid), 0) FROM student_payments WHERE COALESCE(workspace_id, 1) = ?",
                (workspace_id,),
            ).fetchone()[0]
            or 0
        )
        today_appointments = [
            dict(r)
            for r in conn.execute(
                "SELECT booking_ref, full_name, phone, topic, mode, appointment_time FROM appointments WHERE appointment_date = ? AND status = 'confirmed' ORDER BY appointment_time ASC",
                (today_str,),
            ).fetchall()
        ]
        overdue_tasks = [
            dict(r)
            for r in conn.execute(
                """
                SELECT t.id, t.title, t.due_date, c.full_name, c.phone
                FROM contact_tasks t
                LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.done = 0 AND t.due_date != '' AND t.due_date <= ?
                ORDER BY t.due_date ASC
                LIMIT 8
                """,
                (today_str,),
            ).fetchall()
        ]
        total_registrations = conn.execute("SELECT COUNT(*) FROM form_submissions").fetchone()[0]

    unpaid = payments_hub.list_unpaid_balances(workspace_id=workspace_id)
    unpaid_total = sum(u["remaining_balance"] for u in unpaid)
    pct_target = round((month_revenue / monthly_target) * 100, 1) if monthly_target > 0 else 0.0
    conv_rate = round((clients_count / total_contacts) * 100, 1) if total_contacts > 0 else 0.0

    # Strategic AI-style executive recommendations
    recommendations: List[str] = []
    if unpaid:
        recommendations.append(
            f"💰 Recouvrement prioritaire : {len(unpaid)} apprenant(s) cumulent {int(round(unpaid_total)):,} {currency} de soldes restants. Lancez les relances WhatsApp 1-clic dans Paiements.".replace(",", " ")
        )
    if hot_leads:
        top = hot_leads[0]
        recommendations.append(
            f"🔥 Closing immédiat : {len(hot_leads)} prospect(s) chauds prêts à convertir (n°1 : {top['full_name'] or top['email']}, score {top['lead_score']}/100). Appelez-les aujourd'hui."
        )
    if overdue_tasks:
        recommendations.append(
            f"⏰ Discipline commerciale : {len(overdue_tasks)} tâche(s)/relance(s) arrivent à échéance aujourd'hui ou sont en retard."
        )
    if today_appointments:
        recommendations.append(
            f"📅 Agenda du jour : {len(today_appointments)} rendez-vous confirmé(s) aujourd'hui — vérifiez que WhatsApp est connecté pour les rappels H-2."
        )
    if monthly_target > 0 and month_revenue < monthly_target:
        gap = monthly_target - month_revenue
        recommendations.append(
            f"🎯 Cap sur l'objectif mensuel : il manque {int(round(gap)):,} {currency} pour atteindre 100 % de l'objectif ({pct_target} % réalisés).".replace(",", " ")
        )
    if not recommendations:
        recommendations.append("✅ Tous les indicateurs sont au vert ! Continuez la prospection et les séquences automatisées.")

    return {
        "generated_at": now.strftime("%d/%m/%Y à %H:%M"),
        "month": month_prefix,
        "workspace": ws,
        "currency": currency,
        "kpis": {
            "total_contacts": total_contacts,
            "contacted_or_more": contacted_or_more,
            "total_registrations": total_registrations,
            "clients_count": clients_count,
            "conversion_rate": conv_rate,
            "month_revenue": month_revenue,
            "total_revenue": total_revenue,
            "monthly_target": monthly_target,
            "target_pct": pct_target,
            "unpaid_count": len(unpaid),
            "unpaid_total": unpaid_total,
        },
        "hot_leads": hot_leads,
        "unpaid_top": unpaid[:6],
        "today_appointments": today_appointments,
        "overdue_tasks": overdue_tasks,
        "recommendations": recommendations,
    }
