import json
import tempfile
import unittest
from pathlib import Path

import report


class ReportTests(unittest.TestCase):
    def test_performance_metrics_and_anomaly_threshold(self):
        samples = [
            {"numTransactions": 2000, "samplePeriodSecs": 10, "numSlots": 200},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 200},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 200},
        ]
        summary = report.performance_summary(samples)
        self.assertEqual(summary["sampleCount"], 3)
        self.assertEqual(summary["latest"]["tps"], 200)
        self.assertEqual(summary["latest"]["slotTimeMs"], 50)
        self.assertIn("tps-shift", summary["anomaly"]["flags"])
        self.assertEqual(len(summary["samples"]), 3)

    def test_validator_summary_ranks_and_normalizes_stake(self):
        value = {
            "current": [
                {"votePubkey": "small", "nodePubkey": "n1", "activatedStake": 25, "commission": 8},
                {"votePubkey": "large", "nodePubkey": "n2", "activatedStake": 75, "commission": 5},
            ],
            "delinquent": [{"voteAccountPubkey": "late"}],
        }
        summary = report.validator_summary(value)
        self.assertEqual(summary["currentCount"], 2)
        self.assertEqual(summary["delinquentCount"], 1)
        self.assertAlmostEqual(summary["delinquentRate"], 1 / 3)
        self.assertEqual(summary["top"][0]["votePubkey"], "large")
        self.assertAlmostEqual(summary["top"][0]["stakeShare"], 0.75)

    def test_outputs_are_self_contained_and_serializable(self):
        snapshot = {
            "schemaVersion": "1.0",
            "generatedAt": "2026-08-09T00:00:00Z",
            "network": {"cluster": "mainnet-beta"},
            "health": {"rpc": "ok", "slot": 1, "blockHeight": 2},
            "epoch": {"epoch": 3, "progress": 0.5},
            "performance": {"latest": {"tps": 100, "samplePeriodSecs": 10, "numTransactions": 1000, "slotTimeMs": 50}, "samples": []},
            "validators": {"currentCount": 1, "delinquentCount": 0, "delinquentRate": 0, "top": []},
            "economics": {
                "sol": {"priceUsd": 1, "change24h": 2},
                "tvl": {"tvlUsd": 3},
                "dex": {"volume24hUsd": 4},
                "stablecoins": {"supplyUsd": 5},
            },
            "anomalies": [],
        }
        markdown = report.markdown_report(snapshot)
        html = report.html_dashboard(snapshot)
        self.assertIn("Solana Ecosystem Pulse", markdown)
        self.assertIn("Solana Ecosystem Pulse", html)
        self.assertNotIn("${{", html)
        json.dumps(snapshot)
        with tempfile.TemporaryDirectory() as directory:
            report.write_outputs(snapshot, Path(directory))
            self.assertTrue((Path(directory) / "report.json").exists())
            self.assertTrue((Path(directory) / "report.md").exists())
            self.assertTrue((Path(directory) / "dashboard.html").exists())
            self.assertTrue((Path(directory) / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
