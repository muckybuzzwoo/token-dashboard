import os
import unittest

from token_dashboard.pricing import load_pricing, cost_for, format_for_user

PRICING = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pricing.json"))


class CostTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def _u(self, **kw):
        base = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        }
        base.update(kw)
        return base

    def test_known_opus_input_cost(self):
        c = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 5.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_sonnet_output_cost(self):
        c = cost_for("claude-sonnet-4-6", self._u(output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)

    def test_known_sonnet_5_input_cost(self):
        c = cost_for("claude-sonnet-5", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 2.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_sonnet_5_output_cost(self):
        c = cost_for("claude-sonnet-5", self._u(output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 10.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_sonnet_5_5_cost(self):
        c = cost_for("claude-sonnet-5-5", self._u(input_tokens=1_000_000, output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 12.00, places=4)
        self.assertFalse(c["estimated"])

    def test_sonnet_5_5_cache_read_is_0_05x_input(self):
        c = cost_for("claude-sonnet-5-5", self._u(cache_read_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 0.10, places=4)

    def test_known_fable_5_2_cost(self):
        c = cost_for("claude-fable-5-2", self._u(input_tokens=1_000_000, output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 48.00, places=4)
        self.assertFalse(c["estimated"])

    def test_fable_5_2_cache_rates(self):
        u = self._u(cache_read_tokens=1_000_000, cache_create_5m_tokens=1_000_000, cache_create_1h_tokens=1_000_000)
        c = cost_for("claude-fable-5-2", u, self.p)
        self.assertAlmostEqual(c["usd"], 0.20 + 10.00 + 16.00, places=4)

    def test_known_opus_5_5_cost(self):
        c = cost_for("claude-opus-5-5", self._u(input_tokens=1_000_000, output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 24.00, places=4)
        self.assertFalse(c["estimated"])

    def test_opus_5_5_cache_rates(self):
        u = self._u(cache_read_tokens=1_000_000, cache_create_5m_tokens=1_000_000, cache_create_1h_tokens=1_000_000)
        c = cost_for("claude-opus-5-5", u, self.p)
        self.assertAlmostEqual(c["usd"], 0.20 + 5.00 + 8.00, places=4)

    def test_known_opus_5_cost(self):
        c = cost_for("claude-opus-5", self._u(input_tokens=1_000_000, output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 30.00, places=4)
        self.assertFalse(c["estimated"])

    def test_fable_5_1_cache_read_is_cheaper_than_fable_5(self):
        u = self._u(cache_read_tokens=1_000_000)
        self.assertAlmostEqual(cost_for("claude-fable-5-1", u, self.p)["usd"], 0.25, places=4)
        self.assertAlmostEqual(cost_for("claude-fable-5", u, self.p)["usd"], 1.00, places=4)

    def test_known_fable_input_cost(self):
        c = cost_for("claude-fable-5", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 10.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_fable_output_cost(self):
        c = cost_for("claude-fable-5", self._u(output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 50.00, places=4)
        self.assertFalse(c["estimated"])

    def test_unknown_fable_falls_back(self):
        c = cost_for("claude-fable-5-experimental", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 10.00, places=4)
        self.assertTrue(c["estimated"])

    def test_unknown_opus_falls_back(self):
        c = cost_for("claude-opus-9-9-experimental", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 5.00, places=4)
        self.assertTrue(c["estimated"])

    def test_unknown_sonnet_falls_back(self):
        c = cost_for("claude-sonnet-9-9-experimental", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 2.00, places=4)
        self.assertTrue(c["estimated"])

    def test_unknown_unparseable_returns_none(self):
        c = cost_for("custom-local-model", self._u(input_tokens=9999), self.p)
        self.assertIsNone(c["usd"])

    def test_cache_read_cheaper_than_input(self):
        c_in = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        c_cr = cost_for("claude-opus-4-7", self._u(cache_read_tokens=1_000_000), self.p)
        self.assertLess(c_cr["usd"], c_in["usd"])


class PlanFormatTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def test_api_plan_returns_raw(self):
        out = format_for_user(12.34, "api", self.p)
        self.assertEqual(out["display_usd"], 12.34)
        self.assertIsNone(out["subscription_usd"])

    def test_pro_plan_returns_subscription_subtitle(self):
        out = format_for_user(12.34, "pro", self.p)
        self.assertEqual(out["subscription_usd"], 20)
        self.assertIn("Pro", out["subtitle"])


if __name__ == "__main__":
    unittest.main()
