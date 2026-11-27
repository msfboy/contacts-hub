"""Automated regression & security test suite for ContactsHub.

Run with:
    python -m unittest discover -s tests -v
or (if pytest is installed):
    pytest -v
"""

import os
import re
import tempfile
import unittest

# Isolate DB & secrets in a dedicated temporary directory BEFORE importing app/db
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="contactshub_suite_")
os.environ["CONTACTSHUB_DATA_DIR"] = _TEST_DATA_DIR
os.environ["FLASK_SECRET_KEY"] = "test_flask_secret_" + "a" * 48
os.environ["CONTACTSHUB_MASTER_KEY"] = "test_master_key_" + "b" * 48
os.environ["WA_BRIDGE_SECRET"] = "test_wa_bridge_" + "c" * 48

import ai_assistant  # noqa: E402
import app as app_mod  # noqa: E402
import booking_hub  # noqa: E402
import campaigns  # noqa: E402
import db  # noqa: E402
import enrichment  # noqa: E402
import executive_brief  # noqa: E402
import gdrive_client  # noqa: E402
import licensing  # noqa: E402
import net_guard  # noqa: E402
import parsers  # noqa: E402
import payments_hub  # noqa: E402
import wa_client  # noqa: E402
import workflows_engine  # noqa: E402


class ContactsHubTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = app_mod.app.test_client()

    def _get_csrf(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        m = re.search(r'name="csrf-token" content="([^"]+)"', r.get_data(as_text=True))
        self.assertIsNotNone(m, "CSRF meta tag missing from index.html")
        return m.group(1)

    # --- 1. Health, Headers & CSRF ---
    def test_01_healthz_and_security_headers(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get("status"), "ok")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_02_csrf_protection_enforced(self):
        # Mutating without CSRF must return 403
        r_no = self.client.post("/api/webhooks/regenerate", json={})
        self.assertEqual(r_no.status_code, 403)
        # With valid CSRF token it succeeds
        tok = self._get_csrf()
        r_ok = self.client.post("/api/search/natural", json={"query": "togo"}, headers={"X-CSRF-Token": tok})
        self.assertEqual(r_ok.status_code, 200)

    def test_03_wa_bridge_webhook_requires_secret(self):
        r_bad = self.client.post("/api/wa-bridge/webhook", json={"event": "test"})
        self.assertEqual(r_bad.status_code, 401)
        r_ok = self.client.post(
            "/api/wa-bridge/webhook",
            json={"event": "test"},
            headers={"X-WA-Bridge-Secret": db.get_wa_bridge_secret()},
        )
        self.assertEqual(r_ok.status_code, 200)

    # --- 2. Contacts, Tag Sort & Duplicate Phone Prevention ---
    def test_04_contacts_tag_sort_and_phone_duplicate_guard(self):
        tok = self._get_csrf()
        r1 = self.client.post(
            "/api/contacts",
            json={"email": "alice@academy.tg", "full_name": "Alice Koffi", "phone": "+228 93 64 18 44", "tags": "Zeta"},
            headers={"X-CSRF-Token": tok},
        )
        self.assertEqual(r1.status_code, 201)

        # Adding another contact with same phone in different format must return 409
        r_dup = self.client.post(
            "/api/contacts",
            json={"email": "other@academy.tg", "full_name": "Autre", "phone": "93641844", "tags": "Alpha"},
            headers={"X-CSRF-Token": tok},
        )
        self.assertEqual(r_dup.status_code, 409)

        # Add a second distinct contact to test tag sorting
        r2 = self.client.post(
            "/api/contacts",
            json={"email": "bob@academy.tg", "full_name": "Bob Mensah", "phone": "+228 90 11 22 33", "tags": "Alpha"},
            headers={"X-CSRF-Token": tok},
        )
        self.assertEqual(r2.status_code, 201)

        items_asc, _ = db.query_contacts(sort_by="tag_asc")
        tagged = [c["tags"] for c in items_asc if c["tags"]]
        self.assertEqual(tagged[0], "Alpha")

    # --- 3. Quality, Duplicate Merge & Normalization ---
    def test_05_quality_phone_duplicate_merge_and_health(self):
        db.upsert_contacts(
            [
                {"email": "wa.22891000001@whatsapp.local", "full_name": "KODJO", "phone": "+228 91 00 00 01", "tags": "WhatsApp"},
                {"email": "kodjo@pro.tg", "full_name": "Kodjo Ama", "phone": "91000001", "tags": "VIP"},
            ],
            source_type="manual",
            source_name="test",
        )
        groups = [g for g in db.find_duplicate_groups() if g["kind"] == "phone"]
        self.assertGreaterEqual(len(groups), 1)
        # Real email must be recommended primary over @whatsapp.local
        self.assertEqual(groups[0]["contacts"][0]["email"], "kodjo@pro.tg")

        health = db.get_quality_health()
        self.assertGreaterEqual(health["phone_duplicates"], 1)

        res = db.auto_merge_phone_duplicates()
        self.assertGreaterEqual(res["merged_contacts"], 1)
        self.assertEqual(len([g for g in db.find_duplicate_groups() if g["kind"] == "phone"]), 0)

        norm = db.normalize_contacts_data("+228")
        self.assertTrue(norm["ok"])

    # --- 4. Training Form A4 Sheet, Payments Auto-Fill & Signed Receipt ---
    def test_06_training_form_dossier_and_signed_receipt(self):
        tok = self._get_csrf()
        r_f = self.client.post(
            "/api/forms",
            json={
                "title": "Anglais Intensif Test",
                "slug": "anglais-intensif-test",
                "form_kind": "training",
                "config": {
                    "org_name": "Test Academy",
                    "tuition_fee": "30 000 FCFA",
                    "registration_fee": "5 000 FCFA",
                    "total_fee": "35 000 FCFA",
                    "whatsapp_number": "+22890000000",
                },
            },
            headers={"X-CSRF-Token": tok},
        )
        self.assertEqual(r_f.status_code, 201)

        # Submit registration
        r_sub = self.client.post(
            "/f/anglais-intensif-test",
            data={
                "civilite": "M.",
                "last_name": "KOFFI",
                "first_name": "Kodjo",
                "email": "kodjo.inscrit@example.tg",
                "phone": "+22892223344",
                "session_choice": "Soir",
                "level_choice": "Débutant",
                "payment_plan": "Paiement Intégral (35 000 FCFA)",
                "payment_method": "TMoney",
                "payment_ref": "TM-TEST-001",
            },
        )
        self.assertEqual(r_sub.status_code, 200)
        body = r_sub.get_data(as_text=True)
        self.assertIn("Rappel des Modalités de Paiement", body)

        # Registrations API must list the new registration for payment auto-fill
        r_regs = self.client.get("/api/payments/registrations")
        self.assertEqual(r_regs.status_code, 200)
        regs = r_regs.get_json()["items"]
        self.assertTrue(any(x["email"] == "kodjo.inscrit@example.tg" for x in regs))

        # Record payment + check idempotence on payment_ref
        p1 = payments_hub.create_payment({
            "student_name": "Kodjo KOFFI",
            "student_email": "kodjo.inscrit@example.tg",
            "student_phone": "+22892223344",
            "course_name": "Anglais Intensif Test",
            "total_fee": 35000,
            "amount_paid": 35000,
            "payment_method": "TMoney",
            "payment_ref": "TM-TEST-001",
        })
        self.assertTrue(p1["ok"])
        p_dup = payments_hub.create_payment({
            "student_name": "Kodjo KOFFI",
            "course_name": "Anglais Intensif Test",
            "amount_paid": 35000,
            "payment_method": "TMoney",
            "payment_ref": "TM-TEST-001",
        })
        self.assertFalse(p_dup["ok"])
        self.assertTrue(p_dup.get("duplicate"))

        # Receipt page includes signature & official seal
        r_rec = self.client.get(f"/receipt/{p1['id']}")
        self.assertEqual(r_rec.status_code, 200)
        rec_html = r_rec.get_data(as_text=True)
        self.assertIn("VALIDÉ · DIRECTION", rec_html)
        self.assertIn("Signature &amp; Cachet", rec_html)

    # --- 5. Booking /rdv Validation & Anti-Double-Booking ---
    def test_07_booking_validation_and_double_booking(self):
        r_past = booking_hub.create_appointment({
            "full_name": "Test", "phone": "+22890001122",
            "appointment_date": "2020-01-01", "appointment_time": "09:00",
        })
        self.assertFalse(r_past["ok"])

        r_ok = booking_hub.create_appointment({
            "full_name": "Test Future", "phone": "+22890001122",
            "appointment_date": "2099-06-15", "appointment_time": "09:00",
        })
        self.assertTrue(r_ok["ok"])

        # Second booking on exact same slot must fail
        r_dup = booking_hub.create_appointment({
            "full_name": "Test Concurrent", "phone": "+22890003344",
            "appointment_date": "2099-06-15", "appointment_time": "09:00",
        })
        self.assertFalse(r_dup["ok"])

    # --- 6. Security: Anti-SSRF, WhatsApp LID, GDrive, SMTP CRLF, Parsers, Enrichment ---
    def test_08_security_guards(self):
        # Anti-SSRF
        for bad in ("http://127.0.0.1:5055/status", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/"):
            with self.assertRaises(ValueError):
                net_guard.validate_public_url(bad)

        # WhatsApp @lid & 14-digit ghost filter + Groups import
        self.assertFalse(wa_client._is_real_wa_phone("29596670013685@lid", "+29596670013685"))
        self.assertFalse(wa_client._is_real_wa_phone("147330296721487@s.whatsapp.net", "+147330296721487"))
        self.assertTrue(wa_client._is_real_wa_phone("22893641844@s.whatsapp.net", "+22893641844"))
        grp = wa_client.import_group_members_to_crm(
            "Groupe Test",
            [
                {"jid": "22897778899@s.whatsapp.net", "phone": "+22897778899", "name": "Afi"},
                {"jid": "29596670013685@lid", "phone": "+29596670013685", "name": "Ghost"},
            ],
            tag="Groupe: Test",
        )
        self.assertEqual(grp["inserted"], 1)
        self.assertEqual(grp["skipped"], 1)

        # GDrive path traversal & anti-SSRF
        self.assertEqual(gdrive_client._safe_filename(".."), "drive_file")
        ok, _, _, _ = gdrive_client.download_from_link("http://127.0.0.1:5050/secret", _TEST_DATA_DIR)
        self.assertFalse(ok)

        # SMTP CRLF sanitization
        self.assertNotIn("\r", campaigns._clean_header("Test\r\nBcc: evil@x.com"))

        # Parsers invalid email filter
        parsed = parsers._rows_to_contacts([["Email", "Name"], ["logo@2x.png", "Bad"], ["ok@domain.tg", "Good"]])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["email"], "ok@domain.tg")

        # Enrichment word-boundary check
        self.assertLess(
            enrichment.compute_lead_score({"email": "a@b.tg", "email_type": "pro", "role": "assistant budget"}),
            enrichment.compute_lead_score({"email": "a@b.tg", "email_type": "pro", "role": "DG"}),
        )

        # AI builtin fallback
        ai_res = ai_assistant.suggest_reply("Afi", [{"from_me": False, "body": "Quel est le tarif ?"}])
        self.assertTrue(ai_res["ok"] and ai_res["engine"] == "builtin")

    # --- 7. RGPD Forget & Unsubscribe GET Confirmation ---
    def test_09_gdpr_forget_and_unsubscribe(self):
        tok = self._get_csrf()
        r_unsub = self.client.get("/unsubscribe/nonexistent_tracking")
        self.assertEqual(r_unsub.status_code, 200)
        self.assertIn("Confirmer la désinscription", r_unsub.get_data(as_text=True))

        r_forget = self.client.post(
            "/api/gdpr/forget",
            json={"email": "kodjo.inscrit@example.tg"},
            headers={"X-CSRF-Token": tok},
        )
        self.assertEqual(r_forget.status_code, 200)
        with db.get_conn() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM contacts WHERE email = 'kodjo.inscrit@example.tg'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM form_submissions WHERE email = 'kodjo.inscrit@example.tg'").fetchone()[0], 0)

    # --- 8. Commercial Landing Page & Signed SaaS Licensing ---
    def test_10_landing_and_licensing(self):
        r_land = self.client.get("/landing")
        self.assertEqual(r_land.status_code, 200)
        self.assertIn("ContactsHub SaaS", r_land.get_data(as_text=True))

        gen = licensing.generate_license_key("Institut Test", plan="pro", days=30)
        key = gen["license_key"]
        self.assertTrue(key.startswith("CHUB-"))

        ver = licensing.verify_license_key(key)
        self.assertTrue(ver["valid"])
        self.assertEqual(ver["client"], "Institut Test")

        # Tampered key must fail
        bad = licensing.verify_license_key(key[:-4] + "FFFF")
        self.assertFalse(bad["valid"])

        # Expired key must fail
        exp_gen = licensing.generate_license_key("Client Expiré", plan="starter", expires_on="2020-01-01")
        exp_ver = licensing.verify_license_key(exp_gen["license_key"])
        self.assertFalse(exp_ver["valid"])
        self.assertTrue(exp_ver.get("expired"))

        # Reseller console history lists generated licenses
        hist = licensing.list_issued_licenses()
        self.assertGreaterEqual(len(hist), 2)

    # --- 9. Unpaid Balances & Cashbook Excel Export ---
    def test_11_unpaid_balances_and_cashbook_export(self):
        p = payments_hub.create_payment({
            "student_name": "Afi Partial",
            "student_phone": "+22894445566",
            "course_name": "Anglais Soir",
            "total_fee": 35000,
            "amount_paid": 15000,
            "payment_method": "Flooz",
            "payment_ref": "FL-PARTIAL-1",
        })
        self.assertTrue(p["ok"])
        unpaid = payments_hub.list_unpaid_balances()
        self.assertTrue(any(u["student_name"] == "Afi Partial" and u["remaining_balance"] == 20000 for u in unpaid))

        r_exp = self.client.get("/api/payments/export")
        self.assertEqual(r_exp.status_code, 200)
        self.assertIn("spreadsheetml", r_exp.content_type)
        self.assertGreater(len(r_exp.data), 500)

    # --- 10. Pack 1 (#1 Executive Brief, #2 WA Autopilot, #3 Workflows) ---
    def test_12_pack1_executive_autopilot_and_workflows(self):
        # #1 Executive Brief + PDF HTML Report
        brief = executive_brief.generate_executive_brief()
        self.assertIn("recommendations", brief)
        r_rep = self.client.get("/executive-report")
        self.assertEqual(r_rep.status_code, 200)
        self.assertIn("Presidential Daily Brief", r_rep.get_data(as_text=True))

        # #2 WhatsApp AI Autopilot config
        cfg = wa_client.save_autopilot_config(True, "payer,directeur")
        self.assertTrue(cfg["enabled"])
        wa_client.save_autopilot_config(False)

        # #3 Workflows engine (If -> Then)
        wf = workflows_engine.create_rule({
            "name": "Test VIP Tag",
            "trigger_event": "payment_received",
            "action_type": "add_tag",
            "action_config": {"tag": "AutoVIP"},
        })
        self.assertTrue(wf["ok"])
        with db.get_conn() as conn:
            c_row = conn.execute("SELECT id FROM contacts LIMIT 1").fetchone()
        if c_row:
            ran = workflows_engine.fire_event("payment_received", {"contact_id": c_row["id"], "full_name": "Test"})
            self.assertGreaterEqual(ran, 1)

    # --- 11. Pack 2 (#4 B2B Quotes, #5 Payment Gateway Webhook, #6 Sales Leaderboard) ---
    def test_13_pack2_quotes_gateway_leaderboard(self):
        import quotes_hub
        # #5 Configure payment gateway & test webhook HMAC validation + auto-receipt
        gw = payments_hub.save_gateway_config(1, {
            "enabled": True,
            "provider": "fedapay",
            "checkout_url_template": "https://me.fedapay.com/msf?amount={amount}&ref={ref}",
            "webhook_secret": "test-gw-secret-999",
        })
        self.assertTrue(gw["enabled"])
        link = payments_hub.build_payment_link(50000, "DEV-1", "Koffi", 1)
        self.assertIn("amount=50000", link)

        # Reject bad signature
        r_bad = self.client.post(
            "/api/webhooks/gateway/fedapay",
            json={"status": "approved", "amount": 25000, "student_name": "Bad Sig"},
            headers={"X-Webhook-Secret": "wrong"},
        )
        self.assertEqual(r_bad.status_code, 401)

        # Accept valid secret & create payment
        r_ok = self.client.post(
            "/api/webhooks/gateway/fedapay",
            json={
                "status": "approved",
                "amount": 25000,
                "student_name": "Mawuli Online",
                "student_phone": "+22897778899",
                "course_name": "Pack Online",
                "transaction_id": "FEDA-TX-001",
            },
            headers={"X-Webhook-Secret": "test-gw-secret-999"},
        )
        self.assertEqual(r_ok.status_code, 201)
        self.assertTrue(r_ok.get_json()["ok"])

        # #4 Create B2B quote -> view HTML -> 1-click convert to payment & receipt
        q = quotes_hub.create_quote({
            "doc_type": "proforma",
            "client_name": "Directeur RH",
            "client_company": "Orabank Togo",
            "client_phone": "+22896665544",
            "title": "Formation Anglais B2B",
            "items": [{"label": "Session Cadres", "qty": 5, "unit_price": 40000}],
            "discount": 10000,
            "tax_rate": 18,
        })
        self.assertTrue(q["ok"])
        self.assertEqual(q["total_amount"], 224200.0)
        r_qhtml = self.client.get(f"/quote/{q['id']}")
        self.assertEqual(r_qhtml.status_code, 200)
        self.assertIn("Orabank Togo", r_qhtml.get_data(as_text=True))
        self.assertIn("Payer en ligne", r_qhtml.get_data(as_text=True))

        conv = quotes_hub.convert_quote_to_payment(q["id"], payment_method="Virement")
        self.assertTrue(conv["ok"])
        self.assertTrue(conv["receipt_num"].startswith("REC-"))

        # #6 Sales Leaderboard
        lb = executive_brief.compute_sales_leaderboard(1)
        self.assertIn("leaderboard", lb)
        self.assertGreaterEqual(len(lb["leaderboard"]), 1)


if __name__ == "__main__":
    unittest.main()
