#!/usr/bin/env python3
"""Generate a dependency-free Solana ecosystem pulse report.

The collector is intentionally read-only. It uses public Solana JSON-RPC and
best-effort public HTTP endpoints, then writes JSON, Markdown, and a
self-contained HTML dashboard. No wallet, API key, or transaction signing is
involved.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
SCHEMA_VERSION = "1.0"
USER_AGENT = "solana-ecosystem-pulse/1.0 (read-only public data)"


class FetchError(RuntimeError):
    """A public-data request failed or returned an unexpected shape."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    """Fetch JSON with a short timeout and a descriptive error."""

    data = None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url}: response was not JSON") from exc


def rpc_request(rpc_url: str, method: str, params: list[Any]) -> Any:
    body = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
    response = request_json(rpc_url, body)
    if not isinstance(response, dict):
        raise FetchError(f"RPC {method}: response was not an object")
    if response.get("error"):
        error = response["error"]
        raise FetchError(f"RPC {method}: {error}")
    if "result" not in response:
        raise FetchError(f"RPC {method}: missing result")
    return response["result"]


def safe_call(label: str, function: Callable[[], Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        value = function()
        return {
            "label": label,
            "ok": True,
            "elapsedMs": round((time.monotonic() - started) * 1000),
            "value": value,
        }
    except Exception as exc:  # Public endpoints are deliberately best-effort.
        return {
            "label": label,
            "ok": False,
            "elapsedMs": round((time.monotonic() - started) * 1000),
            "error": str(exc),
        }


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def performance_summary(samples: Any) -> dict[str, Any]:
    if not isinstance(samples, list):
        return {"sampleCount": 0, "latest": None, "samples": [], "anomaly": None}
    parsed: list[dict[str, float]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        tx = as_number(sample.get("numTransactions"))
        seconds = as_number(sample.get("samplePeriodSecs"))
        slots = as_number(sample.get("numSlots"))
        if not tx or not seconds or not slots:
            continue
        parsed.append(
            {
                "numTransactions": tx,
                "samplePeriodSecs": seconds,
                "numSlots": slots,
                "tps": tx / seconds,
                "slotTimeMs": seconds * 1000 / slots,
            }
        )
    latest = parsed[0] if parsed else None
    anomaly = None
    if latest and len(parsed) >= 3:
        baseline_tps = statistics.median(row["tps"] for row in parsed[1:])
        baseline_slot_ms = statistics.median(row["slotTimeMs"] for row in parsed[1:])
        tps_delta = safe_ratio(latest["tps"] - baseline_tps, baseline_tps)
        slot_delta = safe_ratio(latest["slotTimeMs"] - baseline_slot_ms, baseline_slot_ms)
        flags: list[str] = []
        if tps_delta is not None and abs(tps_delta) >= 0.25:
            flags.append("tps-shift")
        if slot_delta is not None and slot_delta >= 0.25:
            flags.append("slow-slots")
        anomaly = {
            "baselineTps": baseline_tps,
            "baselineSlotTimeMs": baseline_slot_ms,
            "tpsDelta": tps_delta,
            "slotTimeDelta": slot_delta,
            "flags": flags,
        }
    return {"sampleCount": len(parsed), "latest": latest, "samples": parsed, "anomaly": anomaly}


def validator_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"currentCount": None, "delinquentCount": None, "delinquentRate": None, "totalStake": None, "top": []}
    current = value.get("current", [])
    delinquent = value.get("delinquent", [])
    if not isinstance(current, list):
        current = []
    if not isinstance(delinquent, list):
        delinquent = []
    total_stake = sum(as_number(item.get("activatedStake")) or 0 for item in current if isinstance(item, dict))
    top = []
    for item in sorted(
        (item for item in current if isinstance(item, dict)),
        key=lambda row: as_number(row.get("activatedStake")) or 0,
        reverse=True,
    )[:10]:
        stake = as_number(item.get("activatedStake")) or 0
        top.append(
            {
                "votePubkey": item.get("votePubkey"),
                "nodePubkey": item.get("nodePubkey"),
                "activatedStake": int(stake),
                "stakeShare": safe_ratio(stake, total_stake),
                "commission": item.get("commission"),
            }
        )
    total = len(current) + len(delinquent)
    return {
        "currentCount": len(current),
        "delinquentCount": len(delinquent),
        "delinquentRate": safe_ratio(len(delinquent), total),
        "totalStake": int(total_stake),
        "top": top,
    }


def supply_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"totalLamports": None, "circulatingLamports": None, "nonCirculatingLamports": None}
    return {
        "totalLamports": value.get("value", {}).get("total") if isinstance(value.get("value"), dict) else None,
        "circulatingLamports": value.get("value", {}).get("circulating") if isinstance(value.get("value"), dict) else None,
        "nonCirculatingLamports": value.get("value", {}).get("nonCirculating") if isinstance(value.get("value"), dict) else None,
    }


def coingecko_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("solana"), dict):
        return {"priceUsd": None, "change24h": None}
    sol = value["solana"]
    return {"priceUsd": as_number(sol.get("usd")), "change24h": as_number(sol.get("usd_24h_change"))}


def defillama_tvl_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or not value:
        return {"tvlUsd": None, "date": None}
    latest = value[-1] if isinstance(value[-1], dict) else {}
    return {"tvlUsd": as_number(latest.get("tvl")), "date": latest.get("date")}


def defillama_chain_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        return {"tvlUsd": None, "stablecoinsUsd": None, "mcapUsd": None}
    solana = next((row for row in value if isinstance(row, dict) and str(row.get("name", "")).lower() == "solana"), None)
    if not solana:
        return {"tvlUsd": None, "stablecoinsUsd": None, "mcapUsd": None}
    return {
        "tvlUsd": as_number(solana.get("tvl")),
        "stablecoinsUsd": as_number(solana.get("stablecoins")),
        "mcapUsd": as_number(solana.get("mcap")),
    }


def dex_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"volume24hUsd": None, "volume7dUsd": None, "change24h": None}
    return {
        "volume24hUsd": as_number(value.get("total24h")),
        "volume7dUsd": as_number(value.get("total7d")),
        "change24h": as_number(value.get("change_1d")),
    }


def stablecoin_summary(value: Any) -> dict[str, Any]:
    def total_usd(row: Any) -> float | None:
        if isinstance(row, dict):
            values = [as_number(item) for item in row.values()]
            numeric = [item for item in values if item is not None]
            return sum(numeric) if numeric else None
        return as_number(row)

    if isinstance(value, list) and value:
        row = value[-1] if isinstance(value[-1], dict) else {}
        return {"supplyUsd": total_usd(row.get("totalCirculatingUSD")), "date": row.get("date")}
    if isinstance(value, dict):
        return {"supplyUsd": total_usd(value.get("totalCirculatingUSD")), "date": value.get("date")}
    return {"supplyUsd": None, "date": None}


def collect(rpc_url: str = DEFAULT_RPC, include_offchain: bool = True) -> dict[str, Any]:
    """Collect a single read-only snapshot."""

    rpc_specs: dict[str, tuple[str, list[Any]]] = {
        "health": ("getHealth", []),
        "slot": ("getSlot", [{"commitment": "confirmed"}]),
        "blockHeight": ("getBlockHeight", [{"commitment": "confirmed"}]),
        "epochInfo": ("getEpochInfo", [{"commitment": "confirmed"}]),
        "performance": ("getRecentPerformanceSamples", [5]),
        "voteAccounts": ("getVoteAccounts", [{"commitment": "confirmed"}]),
        "supply": ("getSupply", [{"commitment": "confirmed"}]),
    }

    checks: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(rpc_specs)) as pool:
        futures = {
            pool.submit(safe_call, label, lambda spec=spec: rpc_request(rpc_url, spec[0], spec[1])): label
            for label, spec in rpc_specs.items()
        }
        for future in as_completed(futures):
            result = future.result()
            checks[result["label"]] = result

    offchain_specs: dict[str, tuple[str, Callable[[Any], dict[str, Any]]]] = {
        "solPrice": (
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd&include_24hr_change=true",
            coingecko_summary,
        ),
        "chainTvl": ("https://api.llama.fi/v2/historicalChainTvl/Solana", defillama_tvl_summary),
        "chainOverview": ("https://api.llama.fi/v2/chains", defillama_chain_summary),
        "dexOverview": ("https://api.llama.fi/overview/dexs/Solana?excludeTotalDataChart=true", dex_summary),
        "stablecoins": ("https://stablecoins.llama.fi/stablecoincharts/Solana", stablecoin_summary),
    }
    if include_offchain:
        with ThreadPoolExecutor(max_workers=len(offchain_specs)) as pool:
            futures = {
                pool.submit(safe_call, label, lambda url=url: request_json(url)): label
                for label, (url, _parser) in offchain_specs.items()
            }
            for future in as_completed(futures):
                result = future.result()
                label = result["label"]
                parser = offchain_specs[label][1]
                if result["ok"]:
                    try:
                        result["value"] = parser(result["value"])
                    except Exception as exc:
                        result["ok"] = False
                        result.pop("value", None)
                        result["error"] = f"parse error: {exc}"
                checks[label] = result

    slot_result = checks.get("slot", {}).get("value")
    slot = int(slot_result) if isinstance(slot_result, (int, float)) else None
    block_time = None
    if slot is not None:
        block_time_result = safe_call("blockTime", lambda: rpc_request(rpc_url, "getBlockTime", [slot]))
        checks["blockTime"] = block_time_result
        if block_time_result["ok"]:
            block_time = block_time_result["value"]

    performance = performance_summary(checks.get("performance", {}).get("value"))
    validators = validator_summary(checks.get("voteAccounts", {}).get("value"))
    supply = supply_summary(checks.get("supply", {}).get("value"))
    epoch = checks.get("epochInfo", {}).get("value")
    epoch = epoch if isinstance(epoch, dict) else {}

    rpc_ok = sum(1 for label in rpc_specs if checks.get(label, {}).get("ok"))
    source_status = {
        label: {
            "ok": result.get("ok", False),
            "elapsedMs": result.get("elapsedMs"),
            **({"error": result["error"]} if not result.get("ok") else {}),
        }
        for label, result in checks.items()
    }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": utc_now(),
        "network": {"name": "Solana", "cluster": "mainnet-beta", "rpcUrl": rpc_url},
        "health": {
            "rpc": checks.get("health", {}).get("value"),
            "rpcChecksPassed": rpc_ok,
            "rpcChecksTotal": len(rpc_specs),
            "slot": slot,
            "blockHeight": checks.get("blockHeight", {}).get("value"),
            "blockTime": block_time,
        },
        "epoch": {
            "epoch": epoch.get("epoch"),
            "absoluteSlot": epoch.get("absoluteSlot"),
            "slotIndex": epoch.get("slotIndex"),
            "slotsInEpoch": epoch.get("slotsInEpoch"),
            "transactionCount": epoch.get("transactionCount"),
            "progress": safe_ratio(float(epoch.get("slotIndex", 0)), float(epoch.get("slotsInEpoch", 0)))
            if epoch.get("slotIndex") is not None and epoch.get("slotsInEpoch")
            else None,
        },
        "performance": performance,
        "validators": validators,
        "supply": supply,
        "economics": {
            "sol": checks.get("solPrice", {}).get("value", {"priceUsd": None, "change24h": None}),
            "tvl": checks.get("chainTvl", {}).get("value", {"tvlUsd": None, "date": None}),
            "chainOverview": checks.get("chainOverview", {}).get("value", {}),
            "dex": checks.get("dexOverview", {}).get("value", {"volume24hUsd": None, "volume7dUsd": None, "change24h": None}),
            "stablecoins": checks.get("stablecoins", {}).get("value", {"supplyUsd": None, "date": None}),
        },
        "anomalies": build_anomalies(performance, validators, checks),
        "sources": source_status,
    }


def build_anomalies(performance: dict[str, Any], validators: dict[str, Any], checks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    performance_anomaly = performance.get("anomaly") or {}
    for flag in performance_anomaly.get("flags", []):
        anomalies.append({"severity": "watch", "signal": flag, "details": performance_anomaly})
    delinquent_rate = validators.get("delinquentRate")
    if delinquent_rate is not None and delinquent_rate >= 0.01:
        anomalies.append({"severity": "watch", "signal": "validator-delinquency", "details": {"rate": delinquent_rate}})
    if not checks.get("health", {}).get("ok", False):
        anomalies.append({"severity": "error", "signal": "rpc-health-unavailable", "details": checks.get("health", {}).get("error")})
    return anomalies


def fmt_number(value: Any, digits: int = 1) -> str:
    number = as_number(value)
    if number is None:
        return "n/a"
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.{digits}f}B"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.{digits}f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.{digits}f}K"
    return f"{number:,.{digits}f}"


def fmt_usd(value: Any) -> str:
    number = as_number(value)
    return "n/a" if number is None else f"${number:,.2f}"


def fmt_percent(value: Any) -> str:
    number = as_number(value)
    return "n/a" if number is None else f"{number * 100:.2f}%"


def md_value(value: Any) -> str:
    return "n/a" if value is None else str(value)


def markdown_report(snapshot: dict[str, Any]) -> str:
    health = snapshot["health"]
    epoch = snapshot["epoch"]
    perf = snapshot["performance"]
    latest = perf.get("latest") or {}
    validators = snapshot["validators"]
    economics = snapshot["economics"]
    sol = economics["sol"]
    dex = economics["dex"]
    tvl = economics["tvl"]
    stablecoins = economics["stablecoins"]
    status = "OK" if health.get("rpc") == "ok" and not any(item.get("severity") == "error" for item in snapshot["anomalies"]) else "CHECK"
    lines = [
        "# Solana Ecosystem Pulse",
        "",
        f"Generated: `{snapshot['generatedAt']}`  ",
        f"Cluster: `{snapshot['network']['cluster']}`  ",
        f"Read-only status: **{status}**",
        "",
        "> This is a point-in-time public-data snapshot. Missing optional off-chain values are shown as `n/a`; they are not inferred.",
        "",
        "## Executive snapshot",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| RPC health | `{md_value(health.get('rpc'))}` |",
        f"| Slot / block height | `{md_value(health.get('slot'))}` / `{md_value(health.get('blockHeight'))}` |",
        f"| Epoch / progress | `{md_value(epoch.get('epoch'))}` / `{fmt_percent(epoch.get('progress'))}` |",
        f"| Recent throughput | `{fmt_number(latest.get('tps'))}` TPS |",
        f"| Recent slot time | `{fmt_number(latest.get('slotTimeMs'), 2)}` ms |",
        f"| Active validators | `{md_value(validators.get('currentCount'))}` |",
        f"| Delinquent validators | `{md_value(validators.get('delinquentCount'))}` ({fmt_percent(validators.get('delinquentRate'))}) |",
        f"| SOL price | `{fmt_usd(sol.get('priceUsd'))}` ({fmt_percent((sol.get('change24h') or 0) / 100 if sol.get('change24h') is not None else None)} 24h) |",
        f"| Solana TVL | `{fmt_usd(tvl.get('tvlUsd'))}` |",
        f"| DEX volume | `{fmt_usd(dex.get('volume24hUsd'))}` (24h) |",
        f"| Stablecoin supply | `{fmt_usd(stablecoins.get('supplyUsd'))}` |",
        "",
        "## Network performance",
        "",
        "| Sample | TPS | Slot time | Transactions |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for row in (perf.get("samples") or []):
        lines.append(f"| {row.get('samplePeriodSecs', 'n/a')} s | {fmt_number(row.get('tps'))} | {fmt_number(row.get('slotTimeMs'), 2)} ms | {fmt_number(row.get('numTransactions'), 0)} |")
    if not perf.get("samples"):
        lines.append("| n/a | n/a | n/a | n/a |")
    lines += [
        "",
        "## Validator concentration",
        "",
        f"Total active stake: `{fmt_number(validators.get('totalStake'), 0)}` lamports. The table is ranked by activated stake; vote and node keys are identifiers, not labels for operators.",
        "",
        "| Rank | Vote account | Stake share | Commission |",
        "| ---: | --- | ---: | ---: |",
    ]
    for rank, row in enumerate(validators.get("top", []), 1):
        lines.append(f"| {rank} | `{row.get('votePubkey') or 'n/a'}` | {fmt_percent(row.get('stakeShare'))} | {md_value(row.get('commission'))}% |")
    if not validators.get("top"):
        lines.append("| n/a | n/a | n/a | n/a |")
    lines += [
        "",
        "## Signals",
        "",
    ]
    if snapshot["anomalies"]:
        for item in snapshot["anomalies"]:
            lines.append(f"- **{item['severity']}** `{item['signal']}`")
    else:
        lines.append("- No configured watch thresholds were crossed in this snapshot.")
    lines += [
        "",
        "## Sources and limitations",
        "",
        "- Solana JSON-RPC: `getHealth`, `getSlot`, `getBlockHeight`, `getEpochInfo`, `getRecentPerformanceSamples`, `getVoteAccounts`, `getSupply`, and `getBlockTime`.",
        "- Optional public endpoints: CoinGecko simple price, DeFiLlama chain/DEX/TVL, and Stablecoins by DeFiLlama.",
        "- No X/Twitter API is used because it requires credentials; the report records that source as unavailable rather than scraping or fabricating sentiment.",
        "- Validator stake is summed from the active vote-account response. It is not a claim about governance influence or operator identity.",
        "",
        "## Reproduce",
        "",
        "```sh",
        "python3 report.py --out-dir out",
        "python3 report.py --out-dir out --interval 900",
        "```",
        "",
        "Generated by `solana-ecosystem-pulse`; all collection is read-only.",
        "",
    ]
    return "\n".join(lines)


def html_dashboard(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solana Ecosystem Pulse</title>
<style>
:root {{ color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324d; --text:#e8eefc; --muted:#95a4c4; --good:#6ee7b7; --warn:#fbbf24; --bad:#fb7185; --accent:#8b9cff; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:radial-gradient(circle at top right,#19254a 0,#0b1020 48%); color:var(--text); font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:1240px; margin:0 auto; padding:42px 22px 64px; }} h1 {{ margin:0; font-size:clamp(28px,5vw,52px); letter-spacing:-.04em; }} h2 {{ margin:34px 0 14px; font-size:20px; }} .subtitle {{ color:var(--muted); margin:8px 0 0; }} .meta {{ display:flex; flex-wrap:wrap; gap:8px; margin:22px 0; }} .pill {{ border:1px solid var(--line); border-radius:999px; color:var(--muted); padding:5px 11px; font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }} .card {{ background:rgba(18,26,45,.88); border:1px solid var(--line); border-radius:16px; padding:17px; box-shadow:0 12px 35px rgba(0,0,0,.14); }} .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }} .value {{ margin-top:5px; font-size:25px; font-weight:700; }} .small {{ color:var(--muted); font-size:12px; }} .good {{ color:var(--good); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
.panel {{ background:rgba(18,26,45,.88); border:1px solid var(--line); border-radius:16px; overflow:auto; }} table {{ width:100%; border-collapse:collapse; min-width:620px; }} th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:12px 14px; }} th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }} tr:last-child td {{ border-bottom:0; }} code {{ color:#c7d2fe; }} .bar {{ height:8px; background:#1e2943; border-radius:99px; overflow:hidden; }} .bar span {{ display:block; height:100%; background:linear-gradient(90deg,var(--accent),var(--good)); }} .foot {{ color:var(--muted); font-size:12px; margin-top:28px; }}
</style>
</head>
<body><main>
<h1>Solana Ecosystem Pulse</h1>
<p class="subtitle">Read-only public-data snapshot with bounded anomaly signals.</p>
<div class="meta"><span class="pill" id="generated"></span><span class="pill" id="cluster"></span><span class="pill" id="status"></span></div>
<section class="grid" id="cards"></section>
<h2>Network performance</h2><div class="panel"><table><thead><tr><th>Sample</th><th>TPS</th><th>Slot time</th><th>Transactions</th></tr></thead><tbody id="performance"></tbody></table></div>
<h2>Top active validators by stake</h2><div class="panel"><table><thead><tr><th>Rank</th><th>Vote account</th><th>Stake share</th><th>Commission</th></tr></thead><tbody id="validators"></tbody></table></div>
<h2>Signals</h2><div class="grid" id="signals"></div>
<p class="foot">Sources are recorded in the JSON output. Optional off-chain endpoints may be unavailable or rate-limited; unavailable data is shown as n/a. Re-run <code>python3 report.py</code> to refresh.</p>
</main>
<script>
const d = {encoded};
const n = v => v == null ? 'n/a' : new Intl.NumberFormat(undefined, {{maximumFractionDigits:1}}).format(v);
const usd = v => v == null ? 'n/a' : new Intl.NumberFormat(undefined, {{style:'currency',currency:'USD',maximumFractionDigits:2}}).format(v);
const pct = v => v == null ? 'n/a' : (v*100).toFixed(2)+'%';
const latest = d.performance.latest || {{}};
const sol = d.economics.sol || {{}};
const dex = d.economics.dex || {{}};
const cards = [
 ['RPC health', d.health.rpc || 'n/a', 'slot '+(d.health.slot ?? 'n/a')],
 ['Epoch progress', pct(d.epoch.progress), 'epoch '+(d.epoch.epoch ?? 'n/a')],
 ['Throughput', n(latest.tps)+' TPS', n(latest.numTransactions)+' tx in '+n(latest.samplePeriodSecs)+'s'],
 ['Slot time', latest.slotTimeMs == null ? 'n/a' : n(latest.slotTimeMs)+' ms', 'recent performance sample'],
 ['Validators', n(d.validators.currentCount), n(d.validators.delinquentCount)+' delinquent'],
 ['SOL price', usd(sol.priceUsd), sol.change24h == null ? 'n/a 24h' : sol.change24h.toFixed(2)+'% 24h'],
 ['Solana TVL', usd(d.economics.tvl?.tvlUsd), 'best-effort DeFiLlama'],
 ['DEX volume', usd(dex.volume24hUsd), '24h, best-effort']
];
document.getElementById('generated').textContent = 'Generated '+d.generatedAt;
document.getElementById('cluster').textContent = d.network.cluster;
const issue = d.anomalies.some(x=>x.severity==='error');
document.getElementById('status').textContent = issue ? 'status: check' : 'status: ok';
document.getElementById('cards').innerHTML = cards.map(([label,value,small])=>`<article class="card"><div class="label">${{label}}</div><div class="value">${{value}}</div><div class="small">${{small}}</div></article>`).join('');
const samples = d.performance.samples || [];
document.getElementById('performance').innerHTML = (samples.length ? samples : [{{}}]).map(x=>`<tr><td>${{x.samplePeriodSecs ?? 'n/a'}} s</td><td>${{n(x.tps)}}</td><td>${{x.slotTimeMs == null ? 'n/a' : n(x.slotTimeMs)+' ms'}}</td><td>${{n(x.numTransactions)}}</td></tr>`).join('');
const top = d.validators.top || [];
document.getElementById('validators').innerHTML = (top.length ? top : [{{}}]).map((x,i)=>`<tr><td>${{top.length ? i+1 : 'n/a'}}</td><td><code>${{x.votePubkey || 'n/a'}}</code></td><td><div>${{pct(x.stakeShare)}}</div><div class="bar"><span style="width:${{Math.min(100,(x.stakeShare||0)*100)}}%"></span></div></td><td>${{x.commission ?? 'n/a'}}%</td></tr>`).join('');
const signals = d.anomalies || [];
document.getElementById('signals').innerHTML = (signals.length ? signals : [{{severity:'good',signal:'no-threshold-crossings',details:'No configured watch threshold crossed.'}}]).map(x=>`<article class="card"><div class="label ${{x.severity==='error'?'bad':x.severity==='watch'?'warn':'good'}}">${{x.severity}}</div><div class="value" style="font-size:18px">${{x.signal}}</div><div class="small">${{typeof x.details === 'string' ? x.details : 'Review the JSON snapshot for details.'}}</div></article>`).join('');
</script>
</body></html>'''


def atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(contents, encoding="utf-8")
    os.replace(temporary, path)


def write_outputs(snapshot: dict[str, Any], out_dir: Path) -> None:
    atomic_write(out_dir / "report.json", json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    atomic_write(out_dir / "report.md", markdown_report(snapshot))
    atomic_write(out_dir / "dashboard.html", html_dashboard(snapshot))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", default=os.getenv("SOLANA_RPC_URL", DEFAULT_RPC))
    parser.add_argument("--out-dir", default="out", type=Path)
    parser.add_argument("--interval", default=0, type=int, help="Repeat every N seconds; 0 means one snapshot.")
    parser.add_argument("--no-offchain", action="store_true", help="Only call Solana RPC; skip optional public data endpoints.")
    args = parser.parse_args(argv)
    if args.interval < 0:
        parser.error("--interval must be non-negative")
    while True:
        snapshot = collect(args.rpc_url, include_offchain=not args.no_offchain)
        write_outputs(snapshot, args.out_dir)
        print(f"wrote {args.out_dir / 'report.json'}, {args.out_dir / 'report.md'}, {args.out_dir / 'dashboard.html'} at {snapshot['generatedAt']}")
        if not args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
