import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import openpyxl

sys.path.insert(0, os.path.abspath("."))
from scraper_engine import ScraperEngine

class TestLiveEngineFlow(unittest.TestCase):
    def test_engine_email_enrichment_and_dual_export(self):
        output_name = "test_dual_export"
        output_dir = "output"

        # Mock items that have websites
        sample_items = [
            {
                "name": "Apollo Clinic",
                "phone": "+91 98401 22334",
                "email": None,
                "address": "Kilpauk, Chennai",
                "category": "Clinic",
                "rating": "4.8",
                "website": "https://apolloclinic.com",
                "schedule": "8:00 AM - 9:00 PM",
                "link": "https://maps.google.com/apollo_clinic"
            },
            {
                "name": "MedPlus Pharmacy",
                "phone": "044 2433 1122",
                "email": None,
                "address": "Poonamallee High Road, Chennai",
                "category": "Pharmacy",
                "rating": "4.5",
                "website": "https://medpluspharmacy.com",
                "schedule": "Open 24 Hours",
                "link": "https://maps.google.com/medplus"
            }
        ]

        # Mock requests.get for website email crawling
        def mock_requests_get(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            m.headers = {"Content-Type": "text/html"}
            if "apolloclinic" in url:
                m.text = "<html><body>Contact: <a href='mailto:care@apolloclinic.com'>care@apolloclinic.com</a></body></html>"
            else:
                m.text = "<html><body>Reach us at info@medpluspharmacy.com</body></html>"
            return m

        with patch("scraper_engine.requests.get", side_effect=mock_requests_get):
            engine = ScraperEngine(
                query="Apollo Clinics Chennai",
                output_name=output_name,
                output_dir=output_dir,
                headless=True,
                district_deep=False,
                existing_items=sample_items
            )
            # Simulate already having feed-extracted items
            engine.scraped_items = [dict(x) for x in sample_items]

            # Fast parallel email enrichment test
            email_candidates = [i for i in engine.scraped_items if i.get("website") and not i.get("email")]
            self.assertEqual(len(email_candidates), 2)

            from scraper_engine import extract_email_from_website
            for it in email_candidates:
                em = extract_email_from_website(it["website"])
                if em:
                    it["email"] = em

            self.assertEqual(engine.scraped_items[0]["email"], "care@apolloclinic.com")
            self.assertEqual(engine.scraped_items[1]["email"], "info@medpluspharmacy.com")
            print("  [PASS] ScraperEngine successfully enriched emails from business websites.")

            # Test dual export
            csv_path = os.path.join(output_dir, f"{output_name}_details.csv")
            xlsx_path = os.path.join(output_dir, f"{output_name}_details.xlsx")

            from scraper_engine import export_clean_details_csv, export_clean_details_xlsx
            export_clean_details_csv(engine.scraped_items, csv_path)
            export_clean_details_xlsx(engine.scraped_items, xlsx_path)

            self.assertTrue(os.path.isfile(csv_path))
            self.assertTrue(os.path.isfile(xlsx_path))

            # Inspect Excel output
            wb = openpyxl.load_workbook(xlsx_path)
            ws = wb.active
            self.assertEqual(ws.cell(row=1, column=3).value, "Email")
            self.assertEqual(ws.cell(row=2, column=3).value, "care@apolloclinic.com")
            self.assertEqual(ws.cell(row=3, column=3).value, "info@medpluspharmacy.com")
            print("  [PASS] Dual export generated valid CSV and Excel files containing enriched emails.")

if __name__ == "__main__":
    unittest.main()
