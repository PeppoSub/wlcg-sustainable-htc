#!/usr/bin/env python3
#
# co2_monitor.py  —  CO2 Intensity Monitor for ScotGrid Glasgow
# =============================================================
# Single script with two modes:
#
#   serve   persistent service (systemd): runs Prometheus exporter on :9092
#           and HTTP forecast server on :9091 in parallel threads.
#           Prometheus scrapes :9092 every minute and stores the timeseries.
#           Grafana Infinity plugin reads the forecast CSVs from :9091.
#
#   fetch   cron job (daily at 07:00 UTC): fetches 48h forecast for all
#           three regions, saves CSVs, appends to per-region rolling logs.
#           Run manually as needed, or from cron:
#             0 7 * * *  cd /opt/co2monitor && python3 co2_monitor.py fetch >> logs/co2_fetch.log 2>&1
#
# Three regions monitored:
#   G12     postcode  Glasgow area (our cluster location)
#   Scot16  regionid  Scotland  (Carbon Intensity API region 16)
#   UK18    national  GB average (Carbon Intensity API /intensity endpoint)
#
# Prometheus metrics exposed (no index label — avoids multiple Grafana lines):
#   co2_intensity_gco2_per_kwh{postcode="G12"}
#   co2_intensity_gco2_per_kwh{postcode="Scot16"}
#   co2_intensity_gco2_per_kwh{postcode="UK18"}
#   co2_intensity_fetch_ok{postcode=...}          1 = last fetch OK, 0 = failed
#   co2_intensity_fetch_timestamp{postcode=...}   unix timestamp of last fetch
#
# HTTP endpoints for Grafana Infinity (48h forecast, updated by cron):
#   GET /forecast/g12       Glasgow G12  forecast CSV
#   GET /forecast/scotland  Scotland 16  forecast CSV
#   GET /forecast/uk        GB national  forecast CSV
#   GET /health             service health check
#
# Useful PromQL once data is flowing:
#   co2_intensity_gco2_per_kwh                              all three regions
#   co2_intensity_gco2_per_kwh{postcode="Scot16"}           Scotland only
#   sum(node_sensor_power>0)/1000 * on() group_left()       cluster CO2 rate
#     sum(co2_intensity_gco2_per_kwh{postcode="Scot16"})    (gCO2/h)
#
# Dependencies:   pip install prometheus_client requests pandas
# Python:         3.9+
#

from __future__ import annotations
import sys
import os
import time
import logging
import requests
import threading
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Union
from prometheus_client import start_http_server, Gauge

# ── configuration ─────────────────────────────────────────────────────────────
EXPORTER_PORT   = 9092          # Prometheus scrapes this
HTTP_PORT       = 9091          # Grafana Infinity reads this
FETCH_INTERVAL  = 60            # seconds between Prometheus metric refreshes
FACTORS_MAX_AGE = 7             # days before refreshing static emission factors
THROTTLE_THRESHOLD = 100        # gCO2/kWh — printed in advisory, not automated

DATA_DIR    = Path("data")
LOG_DIR     = Path("logs")

# Region definitions — name, label (used in metric + filenames), fetch function tag
REGIONS = [
    {"name": "Glasgow G12",  "label": "G12",    "kind": "postcode",  "id": "G12"},
    {"name": "Scotland",     "label": "Scot16", "kind": "regionid",  "id": "16"},
    {"name": "GB National",  "label": "UK18",   "kind": "national",  "id": None},
]

# Emission factors — used by fetch to compute per-fuel CO2 contribution
# These rarely change; co2_daily_fetch also refreshes CarbonFactors.json weekly.
DEFAULT_FACTORS = {
    "gas": 394, "coal": 937, "biomass": 120, "nuclear": 0,
    "hydro": 0, "wind": 0, "solar": 0, "imports": 260, "other": 300,
}
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Prometheus metrics ────────────────────────────────────────────────────────
intensity_gauge = Gauge(
    "co2_intensity_gco2_per_kwh",
    "Grid carbon intensity (gCO2/kWh) — current 30-min slot",
    ["postcode"],          # label kept as 'postcode' for backwards compatibility with G12
)
fetch_ok_gauge = Gauge(
    "co2_intensity_fetch_ok",
    "1 if last API fetch succeeded, 0 if failed",
    ["postcode"],
)
fetch_ts_gauge = Gauge(
    "co2_intensity_fetch_timestamp",
    "Unix timestamp of last successful fetch",
    ["postcode"],
)

# ════════════════════════════════════════════════════════════════════════════
# API fetch helpers — three different endpoint shapes
# ════════════════════════════════════════════════════════════════════════════

def _get(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"GET {url} failed: {e}")
        return None


def fetch_current_intensity(region: dict) -> Optional[float]:
    """
    Fetch the current 30-min slot intensity for a region.
    Returns gCO2/kWh as a float, or None on failure.

    API shapes:
      postcode/regionid  →  /regional/intensity/{now}/fw24h/postcode|regionid/{id}
                            data["data"]["data"][0] or data["data"][0]["data"][0]
      national           →  /intensity
                            data["data"][0]["intensity"]["forecast"]
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    kind = region["kind"]

    if kind == "postcode":
        url  = f"https://api.carbonintensity.org.uk/regional/intensity/{now}/fw24h/postcode/{region['id']}"
        raw  = _get(url)
        if raw is None:
            return None
        d = raw["data"]
        slot = (d["data"] if isinstance(d, dict) else d[0]["data"])[0]
        return float(slot["intensity"]["forecast"])

    elif kind == "regionid":
        url  = f"https://api.carbonintensity.org.uk/regional/intensity/{now}/fw24h/regionid/{region['id']}"
        raw  = _get(url)
        if raw is None:
            return None
        d = raw["data"]
        slot = (d["data"] if isinstance(d, dict) else d[0]["data"])[0]
        return float(slot["intensity"]["forecast"])

    elif kind == "national":
        url  = "https://api.carbonintensity.org.uk/intensity"
        raw  = _get(url)
        if raw is None:
            return None
        slot = raw["data"][0]
        # prefer actual over forecast when available (national endpoint provides both)
        intensity = slot["intensity"]
        value = intensity.get("actual") or intensity.get("forecast")
        return float(value)

    return None


def fetch_48h_forecast(region: dict, now_iso: str) -> Optional[List[dict]]:
    """
    Fetch 48h forecast slots for a region.
    Returns a list of dicts with keys: from, to, forecast.

    API shapes:
      postcode   →  /regional/intensity/{now}/fw48h/postcode/{id}
      regionid   →  /regional/intensity/{now}/fw48h/regionid/{id}
      national   →  /intensity/{now}/fw48h
    """
    kind = region["kind"]

    if kind == "postcode":
        url = f"https://api.carbonintensity.org.uk/regional/intensity/{now_iso}/fw48h/postcode/{region['id']}"
        raw = _get(url)
        if raw is None:
            return None
        d = raw["data"]
        slots = d["data"] if isinstance(d, dict) else d[0]["data"]

    elif kind == "regionid":
        url = f"https://api.carbonintensity.org.uk/regional/intensity/{now_iso}/fw48h/regionid/{region['id']}"
        raw = _get(url)
        if raw is None:
            return None
        d = raw["data"]
        slots = d["data"] if isinstance(d, dict) else d[0]["data"]

    elif kind == "national":
        url = f"https://api.carbonintensity.org.uk/intensity/{now_iso}/fw48h"
        raw = _get(url)
        if raw is None:
            return None
        slots = raw["data"]   # flat list, no generationmix

    else:
        return None

    return [
        {
            "from":     s["from"],
            "to":       s["to"],
            "forecast": s["intensity"]["forecast"],
        }
        for s in slots
    ]

# ════════════════════════════════════════════════════════════════════════════
# SERVE mode — Prometheus exporter + HTTP server
# ════════════════════════════════════════════════════════════════════════════

def exporter_loop():
    """Runs forever: refresh all three region metrics every FETCH_INTERVAL seconds."""
    log.info(f"Exporter started — refreshing every {FETCH_INTERVAL}s")
    while True:
        for region in REGIONS:
            label = region["label"]
            value = fetch_current_intensity(region)
            if value is None:
                fetch_ok_gauge.labels(postcode=label).set(0)
                log.warning(f"  {label}: fetch failed")
            else:
                intensity_gauge.labels(postcode=label).set(value)
                fetch_ok_gauge.labels(postcode=label).set(1)
                fetch_ts_gauge.labels(postcode=label).set(time.time())
                log.info(f"  {label}: {value:.1f} gCO2/kWh")
        time.sleep(FETCH_INTERVAL)


def _forecast_csv(label: str) -> str:
    """Read the latest forecast CSV for a region and return clean 2-col CSV."""
    path = DATA_DIR / f"forecast_{label}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run:  python3 co2_monitor.py fetch"
        )
    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    out = pd.DataFrame({
        "datetime_utc": df.index.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "gco2_per_kwh": df["forecast"].round(2),
    })
    return out.to_csv(index=False)


class ForecastHandler(BaseHTTPRequestHandler):
    ROUTES = {
        "/forecast/g12":      "G12",
        "/forecast/scotland": "Scot16",
        "/forecast/uk":       "UK18",
    }

    def log_message(self, format, *args):
        pass   # suppress per-request noise; errors still reach the logger

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, "text/plain", b"ok\n")
            return

        label = self.ROUTES.get(self.path)
        if label is None:
            self._respond(404, "text/plain",
                          b"Not found. Try /forecast/g12, /forecast/scotland, /forecast/uk\n")
            return

        try:
            body = _forecast_csv(label).encode("utf-8")
            headers = {"Access-Control-Allow-Origin": "*"}
            self._respond(200, "text/csv; charset=utf-8", body, headers)
        except FileNotFoundError as e:
            self._respond(503, "text/plain", str(e).encode())
        except Exception as e:
            self._respond(500, "text/plain", f"Error: {e}".encode())

    def _respond(self, code, ctype, body, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def cmd_serve():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Prometheus metrics endpoint
    start_http_server(EXPORTER_PORT)
    log.info(f"Prometheus exporter on :{EXPORTER_PORT}")
    log.info(f"  test: curl http://localhost:{EXPORTER_PORT}/metrics | grep co2")

    # HTTP forecast server in a background thread
    http_server = HTTPServer(("0.0.0.0", HTTP_PORT), ForecastHandler)
    t = threading.Thread(target=http_server.serve_forever, daemon=True)
    t.start()
    log.info(f"Forecast HTTP server on :{HTTP_PORT}")
    log.info(f"  endpoints: /forecast/g12  /forecast/scotland  /forecast/uk  /health")

    # Exporter loop runs in main thread (blocks until Ctrl+C / systemd stop)
    try:
        exporter_loop()
    except KeyboardInterrupt:
        log.info("Stopped.")
        http_server.shutdown()

# ════════════════════════════════════════════════════════════════════════════
# FETCH mode — daily cron job
# ════════════════════════════════════════════════════════════════════════════

def slots_to_df(slots: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(slots)
    df.index = pd.to_datetime(df["from"], utc=True)
    df.index.name = "datetime"
    return df[["forecast"]].sort_index()


def append_rolling_log(label: str, df: pd.DataFrame):
    """Append new hourly rows to per-region rolling log, skip duplicates."""
    log_path = DATA_DIR / f"rolling_log_{label}.csv"
    hourly = df.resample("1h").mean().round(1)

    if log_path.exists():
        existing = pd.read_csv(log_path, index_col=0, parse_dates=True)
        new_rows = hourly[~hourly.index.isin(existing.index)]
        if new_rows.empty:
            log.info(f"  {label}: rolling log already up to date")
            return
        combined = pd.concat([existing, new_rows]).sort_index()
    else:
        combined = hourly

    combined.to_csv(log_path)
    log.info(f"  {label}: rolling log → {log_path}  ({len(combined)} rows total)")


def print_advisory(label: str, df: pd.DataFrame):
    col = "forecast"
    high = df[df[col] >= THROTTLE_THRESHOLD]
    mn, mx, avg = df[col].min(), df[col].max(), df[col].mean()
    print(f"\n  {label}:  range {mn:.0f}–{mx:.0f}  mean {avg:.0f} gCO2/kWh", end="")
    if high.empty:
        print(f"  ✅ no hours above {THROTTLE_THRESHOLD}")
    else:
        print(f"  ⚠  {len(high)} hours above {THROTTLE_THRESHOLD} gCO2/kWh")
        for ts, row in high.iterrows():
            print(f"      {ts.strftime('%a %d %b %H:%M UTC')}  {row[col]:.0f}")


def cmd_fetch():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%MZ")

    print(f"\n{'='*60}")
    print(f"  CO2 forecast fetch — {now_iso}")
    print(f"{'='*60}")

    for region in REGIONS:
        label = region["label"]
        log.info(f"Fetching 48h forecast: {region['name']} ({label})")

        slots = fetch_48h_forecast(region, now_iso)
        if slots is None:
            log.error(f"  {label}: fetch failed — skipping")
            continue

        df = slots_to_df(slots)

        # Save latest forecast CSV (read by HTTP server → Grafana Infinity)
        csv_path = DATA_DIR / f"forecast_{label}.csv"
        df.to_csv(csv_path)
        log.info(f"  {label}: forecast → {csv_path}  ({len(df)} slots)")

        # Append to rolling log
        append_rolling_log(label, df)

        # Print advisory
        print_advisory(label, df)

    print(f"\n{'='*60}")
    print("  To throttle nodes (if warranted):")
    print("    cd /etc/ansible")
    print("    ansible-playbook playbooks/co2_freq_manager.yml -e 'mode=throttle'")
    print("  To reset:")
    print("    ansible-playbook playbooks/co2_freq_manager.yml -e 'mode=reset'")
    print(f"{'='*60}\n")

# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

USAGE = """
Usage:
  python3 co2_monitor.py serve    Start Prometheus exporter + HTTP forecast server
  python3 co2_monitor.py fetch    Fetch 48h forecast for all regions (run from cron)
"""

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("serve", "fetch"):
        print(USAGE)
        sys.exit(1)
    if sys.argv[1] == "serve":
        cmd_serve()
    else:
        cmd_fetch()
