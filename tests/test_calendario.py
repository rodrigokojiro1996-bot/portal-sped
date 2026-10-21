import unittest
from datetime import date
from unittest import mock

from portal import calendario, config


class PublicationDeadlineTests(unittest.TestCase):
    def test_deadline_is_third_business_day_for_small_groups_and_fifth_for_larger(self):
        expected = {
            "2026-09": (date(2026, 10, 5), date(2026, 10, 7)),
            "2026-10": (date(2026, 11, 5), date(2026, 11, 9)),   # 02/11 Finados
            "2026-11": (date(2026, 12, 3), date(2026, 12, 7)),
            "2026-12": (date(2027, 1, 6), date(2027, 1, 8)),     # 01/01 Confraternização
        }
        for competence, (small, large) in expected.items():
            self.assertEqual(calendario.publication_deadline(competence, 3), (small, 3), competence)
            self.assertEqual(calendario.publication_deadline(competence, 4), (large, 5), competence)

    def test_movable_holidays_follow_easter(self):
        self.assertEqual(calendario.easter(2026), date(2026, 4, 5))
        self.assertIn(date(2027, 2, 9), calendario.non_business_days(2027))   # Carnaval 2027
        self.assertIn(date(2026, 4, 3), calendario.non_business_days(2026))   # Sexta-feira Santa 2026

    def test_next_competence_turns_the_year(self):
        self.assertEqual(calendario.next_competence("2026-12"), "2027-01")
        self.assertEqual(calendario.next_competence("2026-09"), "2026-10")


class TimelineTests(unittest.TestCase):
    def test_timeline_runs_until_forecast_month(self):
        with mock.patch.object(config, "FORECAST_UNTIL", "2026-12"):
            self.assertEqual(calendario.competence_timeline(["2026-09"], today=date(2026, 10, 16)), ["2026-09", "2026-10", "2026-11", "2026-12"])

    def test_timeline_follows_configured_forecast_and_keeps_at_most_15_months(self):
        with mock.patch.object(config, "FORECAST_UNTIL", "2027-03"):
            self.assertEqual(calendario.competence_timeline([], today=date(2026, 10, 16))[-1], "2027-03")
        with mock.patch.object(config, "FORECAST_UNTIL", "2026-12"):
            months = calendario.competence_timeline(["2024-01"], today=date(2026, 10, 16))
        self.assertEqual(len(months), 15)
        self.assertEqual(months[-1], "2026-12")


if __name__ == "__main__":
    unittest.main()
