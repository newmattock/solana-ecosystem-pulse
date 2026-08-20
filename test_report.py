import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import report


class PerformanceSummaryTests(unittest.TestCase):
    def test_metrics_and_anomaly_threshold(self):
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

    def test_non_list_returns_empty_defaults(self):
        summary = report.performance_summary("nope")
        self.assertEqual(summary["sampleCount"], 0)
        self.assertIsNone(summary["latest"])
        self.assertIsNone(summary["anomaly"])

    def test_filters_malformed_samples(self):
        samples = [
            {"numTransactions": "n/a", "samplePeriodSecs": 10, "numSlots": 5},
            {"numTransactions": 100, "samplePeriodSecs": 0, "numSlots": 5},
            {"numTransactions": 100, "samplePeriodSecs": 10, "numSlots": 5},
            "garbage",
            {"numTransactions": 200, "samplePeriodSecs": 10, "numSlots": 5},
        ]
        summary = report.performance_summary(samples)
        self.assertEqual(summary["sampleCount"], 2)

    def test_slow_slots_flag(self):
        samples = [
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 20},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 200},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 200},
        ]
        summary = report.performance_summary(samples)
        self.assertIn("slow-slots", summary["anomaly"]["flags"])

    def test_no_anomaly_when_quiet(self):
        samples = [
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 100},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 110},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 105},
        ]
        summary = report.performance_summary(samples)
        self.assertEqual(summary["anomaly"]["flags"], [])

    def test_anomaly_needs_at_least_three_samples(self):
        samples = [
            {"numTransactions": 2000, "samplePeriodSecs": 10, "numSlots": 200},
            {"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 200},
        ]
        summary = report.performance_summary(samples)
        self.assertIsNone(summary["anomaly"])


class ValidatorSummaryTests(unittest.TestCase):
    def test_ranks_and_normalizes_stake(self):
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

    def test_stringy_stake_values_are_coerced(self):
        value = {
            "current": [
                {"votePubkey": "a", "activatedStake": "1000", "commission": "5"},
                {"votePubkey": "b", "activatedStake": 2000, "commission": None},
            ],
            "delinquent": [],
        }
        summary = report.validator_summary(value)
        self.assertEqual(summary["totalStake"], 3000)
        self.assertEqual(summary["top"][0]["votePubkey"], "b")
        self.assertAlmostEqual(summary["top"][0]["stakeShare"], 2 / 3)

    def test_non_dict_value_defaults(self):
        summary = report.validator_summary("nope")
        self.assertIsNone(summary["currentCount"])
        self.assertEqual(summary["top"], [])


class SupplySummaryTests(unittest.TestCase):
    def test_parses_nested_value(self):
        summary = report.supply_summary({"value": {"total": 10, "circulating": 4, "nonCirculating": 6}})
        self.assertEqual(summary["totalLamports"], 10)
        self.assertEqual(summary["circulatingLamports"], 4)
        self.assertEqual(summary["nonCirculatingLamports"], 6)

    def test_defaults_on_bad_shape(self):
        summary = report.supply_summary({"value": "junk"})
        self.assertIsNone(summary["totalLamports"])


class EconomicsSummaryTests(unittest.TestCase):
    def test_coingecko(self):
        summary = report.coingecko_summary({"solana": {"usd": 150.5, "usd_24h_change": 2.3}})
        self.assertEqual(summary["priceUsd"], 150.5)
        self.assertEqual(summary["change24h"], 2.3)

    def test_defillama_tvl(self):
        summary = report.defillama_tvl_summary([{"date": 123, "tvl": 5e9}, {"date": 124, "tvl": 6e9}])
        self.assertEqual(summary["tvlUsd"], 6e9)
        self.assertEqual(summary["date"], 124)

    def test_defillama_chain_finds_solana_case_insensitively(self):
        rows = [{"name": "Ethereum", "tvl": 1}, {"name": "Solana", "tvl": 2, "stablecoins": 3, "mcap": 4}]
        summary = report.defillama_chain_summary(rows)
        self.assertEqual(summary["tvlUsd"], 2)
        self.assertEqual(summary["stablecoinsUsd"], 3)
        self.assertEqual(summary["mcapUsd"], 4)

    def test_defillama_chain_defaults_on_missing(self):
        summary = report.defillama_chain_summary([{"name": "Ethereum", "tvl": 1}])
        self.assertEqual(summary["tvlUsd"], None)

    def test_dex_summary(self):
        summary = report.dex_summary({"total24h": 7e8, "total7d": 5e9, "change_1d": -1.5})
        self.assertEqual(summary["volume24hUsd"], 7e8)
        self.assertEqual(summary["volume7dUsd"], 5e9)
        self.assertEqual(summary["change24h"], -1.5)

    def test_stablecoin_list_and_dict(self):
        summary = report.stablecoin_summary([{"date": 1, "totalCirculatingUSD": {"peggedUSD": 2e9}}])
        self.assertEqual(summary["supplyUsd"], 2e9)
        summary2 = report.stablecoin_summary({"date": 1, "totalCirculatingUSD": {"peggedUSD": 3e9}})
        self.assertEqual(summary2["supplyUsd"], 3e9)


class NumberAndFormatTests(unittest.TestCase):
    def test_as_number_rejects_bool_and_garbage(self):
        self.assertIsNone(report.as_number(True))
        self.assertIsNone(report.as_number("n/a"))
        self.assertEqual(report.as_number("12.5"), 12.5)
        self.assertEqual(report.as_number(3), 3.0)

    def test_safe_ratio_zero_denominator(self):
        self.assertIsNone(report.safe_ratio(5, 0))
        self.assertEqual(report.safe_ratio(6, 3), 2.0)

    def test_epoch_progress(self):
        self.assertEqual(report.epoch_progress(50, 100), 0.5)
        self.assertIsNone(report.epoch_progress(None, 100))
        self.assertIsNone(report.epoch_progress("bad", "worse"))
        self.assertIsNone(report.epoch_progress(10, 0))

    def test_fmt_number_scales(self):
        self.assertEqual(report.fmt_number(1.5e9), "1.5B")
        self.assertEqual(report.fmt_number(2_500_000), "2.5M")
        self.assertEqual(report.fmt_number(3200), "3.2K")
        self.assertEqual(report.fmt_number(123.4, 1), "123.4")
        self.assertEqual(report.fmt_number(None), "n/a")

    def test_fmt_delta_pct(self):
        self.assertEqual(report.fmt_delta_pct(2.5), "+2.50%")
        self.assertEqual(report.fmt_delta_pct(-1.25), "-1.25%")
        self.assertEqual(report.fmt_delta_pct(None), "n/a")


class FetchAndCallTests(unittest.TestCase):
    def test_request_json_post_sends_json_body(self):
        with unittest.mock.patch("report.urlopen") as mock_open:
            response = unittest.mock.Mock()
            response.read.return_value = b'{"x": 1}'
            mock_open.return_value.__enter__.return_value = response
            result = report.request_json("http://example", {"k": "v"})
        self.assertEqual(result, {"x": 1})
        call = mock_open.call_args[0][0]
        self.assertEqual(call.method, "POST")
        self.assertIn(b'"k": "v"', call.data)

    def test_request_json_raises_fetch_error_on_bad_json(self):
        with unittest.mock.patch("report.urlopen") as mock_open:
            response = unittest.mock.Mock()
            response.read.return_value = b"not json"
            mock_open.return_value.__enter__.return_value = response
            with self.assertRaises(report.FetchError):
                report.request_json("http://example")

    def test_request_json_raises_on_http_error(self):
        from urllib.error import HTTPError
        with unittest.mock.patch("report.urlopen", side_effect=HTTPError("u", 500, "err", {}, None)):
            with self.assertRaises(report.FetchError):
                report.request_json("http://example")

    def test_rpc_request_raises_on_error_field(self):
        with unittest.mock.patch("report.request_json", return_value={"error": {"code": -5}}):
            with self.assertRaises(report.FetchError):
                report.rpc_request("http://x", "getSlot", [])

    def test_safe_call_returns_error_dict(self):
        result = report.safe_call("boom", lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertFalse(result["ok"])
        self.assertIn("x", result["error"])


class CollectTests(unittest.TestCase):
    def _fake_request_json(self):
        def fake(url, payload=None, timeout=20.0):
            if payload is not None:
                method = payload.get("method")
                if method == "getHealth":
                    return {"jsonrpc": "2.0", "id": method, "result": "ok"}
                if method == "getSlot":
                    return {"jsonrpc": "2.0", "id": method, "result": 123}
                if method == "getBlockHeight":
                    return {"jsonrpc": "2.0", "id": method, "result": 122}
                if method == "getEpochInfo":
                    return {"jsonrpc": "2.0", "id": method,
                            "result": {"epoch": 500, "absoluteSlot": 123, "slotIndex": 50, "slotsInEpoch": 100, "transactionCount": 1}}
                if method == "getRecentPerformanceSamples":
                    return {"jsonrpc": "2.0", "id": method,
                            "result": [{"numTransactions": 1000, "samplePeriodSecs": 10, "numSlots": 100}] * 3}
                if method == "getVoteAccounts":
                    return {"jsonrpc": "2.0", "id": method,
                            "result": {"current": [{"votePubkey": "v", "activatedStake": 100}], "delinquent": []}}
                if method == "getSupply":
                    return {"jsonrpc": "2.0", "id": method, "result": {"value": {"total": 1, "circulating": 1, "nonCirculating": 0}}}
                if method == "getBlockTime":
                    return {"jsonrpc": "2.0", "id": method, "result": 1700000000}
                raise AssertionError(f"unexpected method {method}")
            if "coingecko" in url:
                return {"solana": {"usd": 150.0, "usd_24h_change": 1.5}}
            if "historicalChainTvl" in url:
                return [{"date": 1, "tvl": 2e9}]
            if "/chains" in url:
                return [{"name": "Solana", "tvl": 2e9, "stablecoins": 3e9, "mcap": 4e9}]
            if "/dexs/" in url:
                return {"total24h": 7e8}
            if "stablecoincharts" in url:
                return [{"date": 1, "totalCirculatingUSD": {"peggedUSD": 5e9}}]
            raise AssertionError(f"unexpected url {url}")

        return fake

    def test_collect_runs_with_mocked_sources(self):
        with unittest.mock.patch("report.request_json", self._fake_request_json()):
            snapshot = report.collect(rpc_url="http://fake", include_offchain=True)
        self.assertEqual(snapshot["health"]["rpc"], "ok")
        self.assertEqual(snapshot["health"]["slot"], 123)
        self.assertEqual(snapshot["epoch"]["progress"], 0.5)
        self.assertEqual(snapshot["economics"]["sol"]["priceUsd"], 150.0)
        self.assertEqual(snapshot["economics"]["tvl"]["tvlUsd"], 2e9)
        self.assertEqual(snapshot["economics"]["dex"]["volume24hUsd"], 7e8)
        self.assertEqual(snapshot["economics"]["stablecoins"]["supplyUsd"], 5e9)
        self.assertEqual(snapshot["validators"]["currentCount"], 1)
        self.assertTrue(snapshot["health"]["rpcChecksPassed"] > 0)

    def test_collect_no_offchain_marks_sources_skipped(self):
        with unittest.mock.patch("report.request_json", self._fake_request_json()):
            snapshot = report.collect(rpc_url="http://fake", include_offchain=False)
        self.assertIn("solPrice", snapshot["sources"])
        self.assertFalse(snapshot["sources"]["solPrice"]["ok"])
        self.assertIn("skipped", snapshot["sources"]["solPrice"]["error"])

    def test_collect_handles_offchain_parse_failure(self):
        fake = self._fake_request_json()
        def broken(url, payload=None, timeout=20.0):
            if payload is not None:
                return fake(url, payload, timeout)
            return {"totally": "unexpected"}
        with unittest.mock.patch("report.request_json", broken):
            snapshot = report.collect(rpc_url="http://fake", include_offchain=True)
        # Graceful parsers yield defaults for malformed off-chain payloads; the
        # report still completes with valid RPC data and a timestamp.
        self.assertIn("generatedAt", snapshot)
        self.assertEqual(snapshot["health"]["rpc"], "ok")
        self.assertEqual(snapshot["economics"]["sol"]["priceUsd"], None)
        self.assertIsInstance(snapshot["sources"], dict)


class AnomalyAndCLITests(unittest.TestCase):
    def test_build_anomalies_delinquency(self):
        performance = {"anomaly": {"flags": []}}
        validators = {"delinquentRate": 0.02}
        anomalies = report.build_anomalies(performance, validators, {"health": {"ok": True}})
        self.assertEqual(anomalies[0]["signal"], "validator-delinquency")

    def test_build_anomalies_rpc_unavailable(self):
        anomalies = report.build_anomalies({"anomaly": None}, {"delinquentRate": 0.0}, {"health": {"ok": False, "error": "down"}})
        self.assertEqual(anomalies[0]["signal"], "rpc-health-unavailable")

    def test_version_flag(self):
        self.assertEqual(report.main(["--version"]), 0)

    def test_stdout_mode_emits_json(self):
        fake = CollectTests()._fake_request_json()
        with unittest.mock.patch("report.request_json", fake), \
             unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as buf:
            code = report.main(["--stdout", "--rpc-url", "http://fake"])
        self.assertEqual(code, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["health"]["rpc"], "ok")

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

    def test_markdown_reports_sol_delta(self):
        snapshot = {
            "schemaVersion": "1.0",
            "generatedAt": "x", "network": {"cluster": "mainnet-beta"},
            "health": {"rpc": "ok", "slot": 1, "blockHeight": 2},
            "epoch": {"epoch": 1, "progress": 0.5},
            "performance": {"latest": {}, "samples": []},
            "validators": {"currentCount": 1, "delinquentCount": 0, "delinquentRate": 0, "top": []},
            "economics": {"sol": {"priceUsd": 150.0, "change24h": 2.5}, "tvl": {}, "dex": {}, "stablecoins": {}},
            "anomalies": [],
        }
        markdown = report.markdown_report(snapshot)
        self.assertIn("+2.50%", markdown)


if __name__ == "__main__":
    unittest.main()
