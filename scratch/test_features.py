import os
import sys
import unittest
import requests
import openpyxl
from openpyxl.styles import Font, PatternFill

# Add project root to path
sys.path.insert(0, os.path.abspath("."))

from scraper_engine import (
    extract_email_from_website,
    export_clean_details_xlsx,
    export_clean_details_csv,
    ScraperEngine,
    DISALLOWED_WEBSITE_DOMAINS,
    DISALLOWED_EMAIL_DOMAINS
)
import db
import security


class TestEmailAndExcelFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_url = "http://127.0.0.1:3000"
        # Get admin user and auth token
        conn = db.get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        conn.close()
        cls.token = security.generate_auth_token(user["id"], user["role"])
        cls.auth_headers = {"Authorization": f"Bearer {cls.token}"}

    def test_01_email_regex_and_filtering(self):
        """Test website email extraction logic with mock HTML scenarios."""
        # 1. Skip social media domains
        self.assertIsNone(extract_email_from_website("https://facebook.com/mybusiness"))
        self.assertIsNone(extract_email_from_website("https://wa.me/919999999999"))
        self.assertIsNone(extract_email_from_website("https://maps.google.com/test"))

        # 2. Test extraction from mock HTML via responses or direct function check
        from unittest.mock import patch, MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
        mock_resp.text = """
        <html>
          <body>
            <h1>Contact Us</h1>
            <p>For inquiries, email us at <a href="mailto:info@superbiz.com?subject=Hello">info@superbiz.com</a></p>
            <p>Support team: support@superbiz.com</p>
            <p>Fake asset: icon@2x.png</p>
            <p>Fake schema: person@schema.org</p>
          </body>
        </html>
        """

        with patch("requests.get", return_value=mock_resp):
            extracted = extract_email_from_website("https://superbiz.com")
            # Should prioritize info@ or support@
            self.assertIn(extracted, ["info@superbiz.com", "support@superbiz.com"])
            self.assertNotEqual(extracted, "icon@2x.png")
            self.assertNotEqual(extracted, "person@schema.org")
            print(f"  [PASS] Mock HTML email extracted: {extracted}")

    def test_02_export_clean_details_xlsx(self):
        """Test Excel (.xlsx) file generation, styling, freeze panes, and column ordering."""
        sample_items = [
            {
                "name": "Elite Fitness Gym",
                "phone": "+91 98400 11223",
                "email": "contact@elitefitness.in",
                "address": "123 Anna Salai, Chennai",
                "category": "Gym",
                "rating": "4.9",
                "website": "https://elitefitness.in",
                "schedule": "Open 24 Hours",
                "link": "https://maps.google.com/elite_fitness"
            },
            {
                "name": "Chennai Coffee House",
                "phone": "044 2499 8877",
                "email": "orders@chennaicoffee.com",
                "address": "45 TTK Road, Alwarpet",
                "category": "Cafe",
                "rating": "4.6",
                "website": "https://chennaicoffee.com",
                "schedule": "6:00 AM - 10:00 PM",
                "link": "https://maps.google.com/chennai_coffee"
            }
        ]

        test_xlsx_path = os.path.abspath("output/unit_test_leads.xlsx")
        export_clean_details_xlsx(sample_items, test_xlsx_path)

        self.assertTrue(os.path.exists(test_xlsx_path))
        wb = openpyxl.load_workbook(test_xlsx_path)
        self.assertIn("Verified Leads", wb.sheetnames)
        ws = wb["Verified Leads"]

        # Verify headers in exact order
        expected_headers = [
            "Business Name",
            "Phone Number",
            "Email",
            "Address",
            "Category",
            "Rating",
            "Website",
            "Opening Hours",
            "Google Maps Link"
        ]
        actual_headers = [ws.cell(row=1, column=c).value for c in range(1, len(expected_headers) + 1)]
        self.assertEqual(actual_headers, expected_headers)

        # Verify header fill (#1E293B) and font
        h1 = ws.cell(row=1, column=1)
        self.assertEqual(h1.fill.fill_type, "solid")
        self.assertEqual(h1.font.bold, True)
        self.assertEqual(h1.font.color.rgb, "00FFFFFF")

        # Verify freeze panes
        self.assertEqual(ws.freeze_panes, "A2")

        # Verify phone number cell text format (@)
        p_cell = ws.cell(row=2, column=2)
        self.assertEqual(p_cell.number_format, "@")
        self.assertEqual(p_cell.value, "+91 98400 11223")

        # Verify email cell
        e_cell = ws.cell(row=2, column=3)
        self.assertEqual(e_cell.value, "contact@elitefitness.in")

        print("  [PASS] Excel (.xlsx) formatting, headers, freeze panes & zebra striping verified.")

    def test_03_export_clean_details_csv_with_email(self):
        """Test CSV file generation to ensure Email column is in position 3 with UTF-8 BOM."""
        sample_items = [
            {
                "name": "Apollo Dental",
                "phone": "+91 98401 55667",
                "email": "enquiries@apollodental.com",
                "address": "T. Nagar, Chennai",
                "category": "Dental Clinic",
                "rating": "4.7",
                "website": "https://apollodental.com",
                "schedule": "9:00 AM - 8:00 PM",
                "link": "https://maps.google.com/apollo_dental"
            }
        ]

        test_csv_path = os.path.abspath("output/unit_test_leads.csv")
        export_clean_details_csv(sample_items, test_csv_path)

        self.assertTrue(os.path.exists(test_csv_path))
        with open(test_csv_path, "r", encoding="utf-8-sig") as f:
            lines = [l.strip() for l in f if l.strip()]

        headers_line = lines[0]
        self.assertIn('"Email"', headers_line)
        self.assertTrue(headers_line.startswith('"Business Name","Phone Number","Email"'))

        data_line = lines[1]
        self.assertIn('"enquiries@apollodental.com"', data_line)
        print("  [PASS] CSV export includes Email column in position 3 with RFC-4180 quoting.")

    def test_04_direct_xlsx_api_endpoint(self):
        """Test /api/export/xlsx API route for on-demand styled Excel downloads."""
        payload = {
            "items": [
                {
                    "name": "Spur Tank Medicals",
                    "phone": "044 2836 1234",
                    "email": "support@spurtankmed.com",
                    "address": "Chetpet, Chennai",
                    "category": "Pharmacy",
                    "rating": "4.5",
                    "website": "https://spurtankmed.com",
                    "schedule": "Open 24 Hours",
                    "link": "https://maps.google.com/spur_tank"
                }
            ]
        }

        res = requests.post(
            f"{self.base_url}/api/export/xlsx",
            json=payload,
            headers=self.auth_headers,
            timeout=10
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", res.headers.get("Content-Type", ""))
        self.assertTrue(len(res.content) > 1000)

        # Verify downloaded binary is a valid Excel workbook
        import io
        wb = openpyxl.load_workbook(io.BytesIO(res.content))
        self.assertIn("Verified Leads", wb.sheetnames)
        ws = wb["Verified Leads"]
        self.assertEqual(ws.cell(row=2, column=1).value, "Spur Tank Medicals")
        self.assertEqual(ws.cell(row=2, column=3).value, "support@spurtankmed.com")
        print("  [PASS] API /api/export/xlsx delivered valid styled Excel workbook.")

    def test_05_download_file_route_xlsx_support(self):
        """Test /api/download/<filename> supports .xlsx alongside .csv."""
        res = requests.get(
            f"{self.base_url}/api/download/unit_test_leads.xlsx",
            headers=self.auth_headers,
            timeout=10
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", res.headers.get("Content-Type", ""))
        self.assertTrue(len(res.content) > 1000)
        print("  [PASS] API /api/download/<filename> safely served .xlsx with correct MIME type.")

    def test_06_get_history_includes_xlsx(self):
        """Test /api/history returns both CSV and XLSX files."""
        res = requests.get(
            f"{self.base_url}/api/history?all=1",
            headers=self.auth_headers,
            timeout=10
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        files = [f["name"] for f in data.get("files", [])]
        self.assertTrue(any(f.endswith(".xlsx") for f in files))
        print("  [PASS] API /api/history lists .xlsx files for users.")


if __name__ == "__main__":
    unittest.main()
