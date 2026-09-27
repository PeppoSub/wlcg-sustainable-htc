#!/usr/bin/env python3
"""
nodes_history.py  —  Historical HS23 analysis + Prometheus backfill generator
===============================================================================
USAGE
-----
  # Step 1 — reliability audit (find bad nodes)
  python3 nodes_history.py --prometheus-url http://prometheus:9090 \
      --start 2026-01-01T00:00:00Z --end 2026-03-20T00:00:00Z \
      --step 15m --reliability-only

  # Step 2 — full analysis, excluding bad nodes
  python3 nodes_history.py --prometheus-url http://prometheus:9090 \
      --start 2026-01-01T00:00:00Z --end 2026-03-20T00:00:00Z \
      --step 15m --exclude-file bad_nodes.txt --out-dir ./hs23_output

  # Optional flags:
  #   --exclude wn-d20-007,wn-h16-003   comma-separated inline exclusions
  #   --write-om historical_hs23.om      produce Prometheus backfill file
  #   --no-node-samples                  skip per-node grids (faster)
  #   --no-chep                          skip CHEP summary plots
  #   --no-plots                         CSV only

BACKFILL
--------
  promtool tsdb create-blocks-from openmetrics historical_hs23.om /var/lib/prometheus
  curl -X POST http://prometheus:9090/-/reload

OUTPUTS
-------
  node_reliability.csv              per-node metric coverage %, sorted by power_pct
  node_reliability.png              reliability bar chart
  hs23_history.csv                  full tidy dataset (all metrics)
  cluster_total_hs23.png            summed HS23 over time
  hs23_by_group_stacked.png         stacked HS23 per hardware group
  hs23_efficiency_by_group.png      % of nominal HS23 per group
  freq_vs_hs23_scatter.png          sanity check scatter
  power_by_group.png                total power draw per group
  load5_by_group.png                CPU load5 per group
  temperature_by_group.png          avg temperature per group
  hs23_per_watt_by_group.png        power efficiency over time
  node_samples_hs23.png             grid: HS23 for nodes -001/-002/-003
  node_samples_power_w.png          grid: Power
  node_samples_load5.png            grid: Load5
  node_samples_temp_c.png           grid: Temperature
  node_samples_hs23_per_watt.png    grid: HS23/Watt
  chep_power_efficiency_scatter.png Power vs HS23/W scatter (group + freq)
  chep_hs23_per_watt_cdf.png        CDF of HS23/Watt per group
  chep_summary_table.png            rendered per-group summary table

DEPENDENCIES
------------
  pip install requests numpy matplotlib pandas
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Hardware lookup table
# ─────────────────────────────────────────────────────────────────────────────
HARDWARE_LOOKUP: dict[str, list[tuple[float, float]]] = {
    "h16": [(1.20,310.00),(1.40,310.00),(1.60,310.00),(1.80,310.00),
            (2.00,310.00),(2.20,310.00),(2.40,314.26)],
    "h17": [(1.20,195.06),(1.40,227.16),(1.60,259.06),
            (1.80,290.37),(2.00,321.27),(2.20,388.77)],
    "d20": [(1.50,949.23),(2.00,1249.84),(2.35,1839.97)],
    "d21": [(1.50,949.23),(2.00,1249.84),(2.35,1839.97)],
    "d22": [(1.50,1010.57),(2.00,1332.15),(2.60,1939.60)],
    "a23": [(1.00,1086.40),(2.00,1943.41),(2.10,2011.27),(2.20,2088.41),
            (2.30,2154.63),(2.40,2230.03),(2.50,2289.09),(2.60,2358.93),
            (2.70,2420.19),(2.80,2489.48),(2.90,2548.79),(3.00,2622.49)],
    "m24": [(1.00,888.42),(2.00,1574.87),(2.10,1627.30),(2.20,1682.98),
            (2.30,1739.34),(2.40,1804.55),(2.50,1864.24),(2.60,1924.53),
            (2.70,1976.14),(2.80,2030.51),(2.90,2081.04),(3.00,2128.02)],
}

_ARRAYS: dict[str, tuple[np.ndarray, np.ndarray]] = {
    tag: (np.array([p[0] for p in sorted(pts)]),
          np.array([p[1] for p in sorted(pts)]))
    for tag, pts in HARDWARE_LOOKUP.items()
}

NOMINAL_HS23: dict[str, float] = {
    tag: max(p[1] for p in pts) for tag, pts in HARDWARE_LOOKUP.items()
}

GROUP_COLORS: dict[str, str] = {
    "h16":"#888780","h17":"#185FA5",
    "d20":"#D85A30","d21":"#BA7517","d22":"#EF9F27",
    "a23":"#1D9E75","m24":"#7F77DD",
}
GROUP_MARKERS: dict[str, str] = {
    "h16":"o","h17":"s","d20":"^","d21":"v","d22":"D","a23":"P","m24":"*",
}

_TAG_RE  = re.compile(r"wn-([a-z][0-9]+)-\d+")
_PORT_RE = re.compile(r":\d+$")

def _strip_port(instance: str) -> str:
    return _PORT_RE.sub("", instance)

def extract_tag(instance: str) -> str | None:
    m = _TAG_RE.search(instance)
    return m.group(1) if m else None

def interpolate_hs23(tag: str, freq_ghz: float) -> float | None:
    arrays = _ARRAYS.get(tag)
    if arrays is None:
        return None
    return float(np.interp(freq_ghz, arrays[0], arrays[1]))

# ─────────────────────────────────────────────────────────────────────────────
# Prometheus helpers
# ─────────────────────────────────────────────────────────────────────────────
def _prom_get(prom_url: str, endpoint: str, params: dict) -> dict:
    url = f"{prom_url.rstrip('/')}/api/v1/{endpoint}"
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "success":
        raise RuntimeError(f"Prometheus error: {data.get('error','?')}")
    return data["data"]

def query_range(prom_url: str, query: str,
                start: datetime, end: datetime, step: str) -> list[dict]:
    data = _prom_get(prom_url, "query_range", {
        "query": query,
        "start": int(start.timestamp()),
        "end":   int(end.timestamp()),
        "step":  step,
    })
    return data.get("result", [])

def _parse_step_seconds(step: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd]?)", step.strip())
    if not m:
        return 300
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s":1,"m":60,"h":3600,"d":86400}[unit]

def _fetch_metric(prom_url: str, query: str, label: str,
                  start: datetime, end: datetime,
                  step: str, chunk_days: int) -> dict[str, dict]:
    chunks: list[tuple[datetime, datetime]] = []
    cs = start
    while cs < end:
        ce = min(pd.Timestamp(cs) + pd.Timedelta(days=chunk_days), pd.Timestamp(end))
        chunks.append((cs, ce.to_pydatetime()))
        cs = ce.to_pydatetime()
    combined: dict[str, dict] = {}
    for i, (cs, ce) in enumerate(chunks):
        log.info("  [%s] chunk %d/%d  (%s → %s)", label, i+1, len(chunks),
                 cs.date(), ce.date())
        for series in query_range(prom_url, query, cs, ce, step):
            inst = series["metric"].get("instance", "?")
            if inst not in combined:
                combined[inst] = {"metric": series["metric"], "values": []}
            combined[inst]["values"].extend(series["values"])
    log.info("  [%s] received data for %d instances", label, len(combined))
    return combined

# ─────────────────────────────────────────────────────────────────────────────
# Reliability audit
# ─────────────────────────────────────────────────────────────────────────────
def compute_reliability(prom_url: str, start: datetime, end: datetime,
                        step: str, chunk_days: int) -> pd.DataFrame:
    """
    Per-node metric coverage.
    :port stripped from instance names.
    Sorted by power_pct ascending (worst power coverage first).
    """
    log.info("── Reliability audit ────────────────────────────────────────────────")
    step_secs  = _parse_step_seconds(step)
    window_sec = (end - start).total_seconds()
    expected   = max(1, int(window_sec / step_secs))
    log.info("  Expected steps/node: %d  (window=%.1f h, step=%s)",
             expected, window_sec/3600, step)

    freq_raw  = _fetch_metric(prom_url,
        'avg by (instance) (node_cpu_scaling_frequency_hertz{job="workernodes"}) / 1e9',
        "freq", start, end, step, chunk_days)
    power_raw = _fetch_metric(prom_url,
        'sum by (instance) (node_sensor_power{job="workernodes"})',
        "power", start, end, step, chunk_days)

    rows = []
    for inst in sorted(set(freq_raw) | set(power_raw)):
        tag     = extract_tag(inst)
        freq_n  = len(freq_raw[inst]["values"])  if inst in freq_raw  else 0
        power_n = len(power_raw[inst]["values"]) if inst in power_raw else 0
        rows.append({
            "instance":       _strip_port(inst),
            "tag":            tag or "?",
            "freq_steps":     freq_n,
            "power_steps":    power_n,
            "expected_steps": expected,
            "freq_pct":       round(100.0 * freq_n  / expected, 1),
            "power_pct":      round(100.0 * power_n / expected, 1),
        })
    return pd.DataFrame(rows).sort_values("power_pct").reset_index(drop=True)

def save_reliability_csv(rel: pd.DataFrame, out_dir: Path) -> None:
    path = out_dir / "node_reliability.csv"
    rel.to_csv(path, index=False)
    log.info("Saved  %s", path)

def plot_reliability(rel: pd.DataFrame, out_dir: Path) -> None:
    n = len(rel)
    if n == 0:
        return
    fig, axes = plt.subplots(1, 2, figsize=(14, max(4, n*0.22+1.5)), sharey=True)
    labels = rel["instance"].tolist()
    colors = [GROUP_COLORS.get(t, "#aaa") for t in rel["tag"]]
    for ax, col, title in [
        (axes[0], "freq_pct",  "Frequency metric coverage (%)"),
        (axes[1], "power_pct", "Power metric coverage (%)"),
    ]:
        ax.barh(labels, rel[col], color=colors, height=0.7, alpha=0.85)
        ax.axvline(90, color="#e24b4a", linewidth=1, linestyle="--",
                   alpha=0.7, label="90% threshold")
        ax.set_xlim(0, 105)
        ax.set_xlabel("%")
        ax.set_title(title, fontsize=11)
        ax.grid(axis="x", alpha=0.25)
        ax.legend(fontsize=8)
    handles = [plt.Rectangle((0,0),1,1, color=GROUP_COLORS.get(t,"#aaa"), label=t)
               for t in sorted(rel["tag"].unique())]
    fig.legend(handles=handles, loc="lower center", ncol=min(7,len(handles)),
               fontsize=9, framealpha=0.7, title="hardware group",
               bbox_to_anchor=(0.5,-0.03))
    fig.suptitle("Node metric reliability", fontsize=13, y=1.01)
    axes[0].invert_yaxis()
    fig.tight_layout()
    path = out_dir / "node_reliability.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Saved  %s", path)

# ─────────────────────────────────────────────────────────────────────────────
# Main data fetch & DataFrame
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_METRICS: dict[str, str] = {
    "freq_ghz": 'avg by (instance) (node_cpu_scaling_frequency_hertz{job="workernodes"}) / 1e9',
    "power_w":  'sum by (instance) (node_sensor_power{job="workernodes"})',
    "load5":    'avg by (instance) (node_load5{job="workernodes"})',
    "temp_c":   'avg by (instance) (node_hwmon_temp_celsius{job="workernodes"})',
}

def fetch_all_metrics(prom_url: str, start: datetime, end: datetime,
                      step: str, chunk_days: int,
                      exclude: set[str]) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = {}
    for key, query in ANALYSIS_METRICS.items():
        log.info("Fetching metric: %s", key)
        raw = _fetch_metric(prom_url, query, key, start, end, step, chunk_days)
        if exclude:
            raw = {inst: v for inst, v in raw.items()
                   if not any(ex in inst for ex in exclude)}
        result[key] = raw
    return result

def build_dataframe(raw_metrics: dict[str, dict[str, dict]]) -> pd.DataFrame:
    freq_raw = raw_metrics["freq_ghz"]
    rows = []
    skipped_tags: set[str] = set()
    for inst, series in freq_raw.items():
        tag = extract_tag(inst)
        if tag is None:
            log.debug("Cannot parse tag from %r — skipping", inst)
            continue
        inst_clean = _strip_port(inst)
        for ts_str, val_str in series["values"]:
            freq_ghz = float(val_str)
            hs23 = interpolate_hs23(tag, freq_ghz)
            if hs23 is None:
                skipped_tags.add(tag)
                continue
            rows.append({
                "timestamp": datetime.fromtimestamp(float(ts_str), tz=timezone.utc),
                "instance":  inst_clean,
                "tag":       tag,
                "freq_ghz":  freq_ghz,
                "hs23":      hs23,
            })
    if skipped_tags:
        log.warning("No lookup entry for tags: %s", skipped_tags)
    df = pd.DataFrame(rows)
    if df.empty:
        log.error("No frequency data — check Prometheus URL, time range, metric name.")
        sys.exit(1)
    for key in ("power_w", "load5", "temp_c"):
        side_rows = []
        for inst, series in raw_metrics.get(key, {}).items():
            inst_clean = _strip_port(inst)
            for ts_str, val_str in series["values"]:
                side_rows.append({
                    "timestamp": datetime.fromtimestamp(float(ts_str), tz=timezone.utc),
                    "instance":  inst_clean,
                    key:         float(val_str),
                })
        if side_rows:
            df = df.merge(pd.DataFrame(side_rows), on=["timestamp","instance"], how="left")
        else:
            log.warning("No data for metric '%s' — column will be NaN", key)
            df[key] = float("nan")
    df["hs23_per_watt"] = df["hs23"] / df["power_w"].replace(0, float("nan"))
    df.sort_values(["instance","timestamp"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    log.info("DataFrame ready: %d rows, %d nodes", len(df), df["instance"].nunique())
    return df

# ─────────────────────────────────────────────────────────────────────────────
# Plot helpers
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_k(x, _):
    return f"{x/1000:.1f}k" if abs(x) >= 1000 else f"{x:.0f}"

def _date_axis(ax) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())

def _per_group_subplots(df: pd.DataFrame, col: str, ylabel: str, title: str,
                        out_dir: Path, fname: str,
                        agg: str = "mean", y_fmt=None) -> None:
    tags = sorted(df["tag"].unique())
    n = len(tags)
    fig, axes = plt.subplots(n, 1, figsize=(12, 2.4*n), sharex=True)
    if n == 1:
        axes = [axes]
    for ax, tag in zip(axes, tags):
        sub = df[(df["tag"] == tag) & df[col].notna()].copy()
        color = GROUP_COLORS.get(tag, "#555")
        if sub.empty:
            ax.set_ylabel(tag, fontsize=9)
            continue
        sub["date"] = sub["timestamp"].dt.date
        if agg == "sum":
            per_ts = sub.groupby(["date","timestamp"])[col].sum().reset_index()
        else:
            per_ts = sub.groupby(["date","timestamp"])[col].mean().reset_index()
        daily = per_ts.groupby("date")[col].agg(["mean","std"]).reset_index()
        dates = pd.to_datetime(daily["date"])
        vals  = daily["mean"]
        stds  = daily["std"].fillna(0)
        ax.plot(dates, vals, color=color, linewidth=1.0)
        ax.fill_between(dates, vals-stds, vals+stds, alpha=0.15, color=color)
        ax.set_ylabel(tag, fontsize=9)
        if y_fmt:
            ax.yaxis.set_major_formatter(FuncFormatter(y_fmt))
        ax.grid(axis="y", alpha=0.25)
    axes[0].set_title(title, fontsize=13)
    _date_axis(axes[-1])
    fig.autofmt_xdate()
    fig.text(0.02, 0.5, ylabel, va="center", rotation="vertical", fontsize=10)
    fig.tight_layout(rect=[0.04,0,1,1])
    path = out_dir / fname
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)

# ─────────────────────────────────────────────────────────────────────────────
# Group-level plots
# ─────────────────────────────────────────────────────────────────────────────
def plot_cluster_total(df: pd.DataFrame, out_dir: Path) -> None:
    total = df.groupby("timestamp")["hs23"].sum().reset_index()
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(total["timestamp"], total["hs23"], color="#185FA5", linewidth=1.2)
    ax.fill_between(total["timestamp"], total["hs23"], alpha=0.12, color="#185FA5")
    ax.set_title("Total cluster HS23 over time", fontsize=13)
    ax.set_ylabel("HEP-Score (HS23)")
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_k))
    _date_axis(ax)
    fig.autofmt_xdate()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path = out_dir / "cluster_total_hs23.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)

def plot_by_group(df: pd.DataFrame, out_dir: Path) -> None:
    df2 = df.copy()
    df2["date"] = df2["timestamp"].dt.date
    daily = df2.groupby(["date","tag"])["hs23"].mean().reset_index()
    tags = sorted(df["tag"].unique())
    pivoted = daily.pivot(index="date", columns="tag", values="hs23").fillna(0)
    for t in tags:
        if t not in pivoted.columns:
            pivoted[t] = 0.0
    pivoted = pivoted[tags]
    fig, ax = plt.subplots(figsize=(12, 5))
    dates = pd.to_datetime(pivoted.index)
    ax.stackplot(dates, [pivoted[t] for t in tags], labels=tags,
                 colors=[GROUP_COLORS.get(t,"#aaa") for t in tags], alpha=0.85)
    ax.set_title("HS23 by hardware group (daily mean, stacked)", fontsize=13)
    ax.set_ylabel("HEP-Score (HS23)")
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_k))
    _date_axis(ax)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.7)
    fig.autofmt_xdate()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path = out_dir / "hs23_by_group_stacked.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)

def plot_frequency_efficiency(df: pd.DataFrame, out_dir: Path) -> None:
    df2 = df.copy()
    df2["eff_pct"] = df2.apply(
        lambda r: 100.0 * r["hs23"] / NOMINAL_HS23.get(r["tag"], r["hs23"]), axis=1)
    df2["date"] = df2["timestamp"].dt.date
    daily = df2.groupby(["date","tag"])["eff_pct"].mean().reset_index()
    tags = sorted(df["tag"].unique())
    n = len(tags)
    fig, axes = plt.subplots(n, 1, figsize=(12, 2.2*n), sharex=True)
    if n == 1:
        axes = [axes]
    for ax, tag in zip(axes, tags):
        sub   = daily[daily["tag"] == tag]
        dates = pd.to_datetime(sub["date"])
        color = GROUP_COLORS.get(tag, "#555")
        ax.plot(dates, sub["eff_pct"], color=color, linewidth=1.0)
        ax.fill_between(dates, sub["eff_pct"], 100, alpha=0.08, color=color)
        ax.axhline(100, color=color, linewidth=0.6, linestyle="--", alpha=0.5)
        ax.set_ylim(0, 110)
        ax.set_ylabel(f"{tag}\n(%)", fontsize=9)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0f}%"))
        ax.grid(axis="y", alpha=0.25)
    axes[0].set_title("HS23 efficiency per group (% of nominal at max freq)", fontsize=13)
    _date_axis(axes[-1])
    fig.autofmt_xdate()
    fig.tight_layout()
    path = out_dir / "hs23_efficiency_by_group.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)

def plot_freq_vs_hs23_scatter(df: pd.DataFrame, out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    for tag in sorted(df["tag"].unique()):
        sub = df[df["tag"] == tag].sample(min(5000, len(df[df["tag"] == tag])))
        ax.scatter(sub["freq_ghz"], sub["hs23"], s=4, alpha=0.25,
                   label=tag, color=GROUP_COLORS.get(tag, "#aaa"))
    ax.set_xlabel("CPU frequency (GHz)")
    ax.set_ylabel("HEP-Score (HS23)")
    ax.set_title("Frequency vs HS23 (historical measurements)", fontsize=13)
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_k))
    ax.legend(fontsize=9, markerscale=3, framealpha=0.7)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    path = out_dir / "freq_vs_hs23_scatter.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)

def plot_power(df: pd.DataFrame, out_dir: Path) -> None:
    if df["power_w"].isna().all():
        log.warning("No power data — skipping power plot"); return
    _per_group_subplots(df, col="power_w", agg="sum",
        ylabel="Power (W)",
        title="Total node power draw per group (daily sum, mean ± std over time)",
        out_dir=out_dir, fname="power_by_group.png")

def plot_load(df: pd.DataFrame, out_dir: Path) -> None:
    if df["load5"].isna().all():
        log.warning("No load5 data — skipping load plot"); return
    _per_group_subplots(df, col="load5", agg="mean",
        ylabel="Load avg (5 min)",
        title="CPU load5 per group (daily mean ± std across nodes)",
        out_dir=out_dir, fname="load5_by_group.png")

def plot_temperature(df: pd.DataFrame, out_dir: Path) -> None:
    if df["temp_c"].isna().all():
        log.warning("No temperature data — skipping temperature plot"); return
    _per_group_subplots(df, col="temp_c", agg="mean",
        ylabel="Temperature (°C)",
        title="CPU temperature per group (daily mean ± std across nodes)",
        out_dir=out_dir, fname="temperature_by_group.png")

def plot_hs23_per_watt(df: pd.DataFrame, out_dir: Path) -> None:
    if df["hs23_per_watt"].isna().all():
        log.warning("No power data — skipping HS23/Watt plot"); return
    tags = sorted(df["tag"].unique())
    n    = len(tags)
    fig, axes = plt.subplots(n, 1, figsize=(12, 2.4*n), sharex=True)
    if n == 1:
        axes = [axes]
    for ax, tag in zip(axes, tags):
        sub = df[(df["tag"] == tag) & df["hs23_per_watt"].notna()].copy()
        color = GROUP_COLORS.get(tag, "#555")
        if sub.empty:
            ax.set_ylabel(tag, fontsize=9); continue
        sub["date"] = sub["timestamp"].dt.date
        daily = sub.groupby("date")["hs23_per_watt"].mean().reset_index()
        dates = pd.to_datetime(daily["date"])
        vals  = daily["hs23_per_watt"]
        ax.plot(dates, vals, color=color, linewidth=1.1)
        ax.fill_between(dates, vals, alpha=0.15, color=color)
        if len(daily) > 1:
            ymin, ymax = vals.min(), vals.max()
            for yval, va in [(ymin,"top"),(ymax,"bottom")]:
                ax.axhline(yval, color=color, linewidth=0.5, linestyle=":", alpha=0.5)
                ax.text(dates.iloc[-1], yval, f"  {yval:.3f}",
                        fontsize=7, va=va, color=color, alpha=0.8)
        ax.set_ylabel(f"{tag}\nHS23/W", fontsize=9)
        ax.grid(axis="y", alpha=0.25)
    axes[0].set_title("HS23 per Watt by hardware group (daily mean)", fontsize=13)
    _date_axis(axes[-1])
    fig.autofmt_xdate()
    fig.text(0.02, 0.5, "HS23 / Watt", va="center", rotation="vertical", fontsize=10)
    fig.tight_layout(rect=[0.04,0,1,1])
    path = out_dir / "hs23_per_watt_by_group.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)

# ─────────────────────────────────────────────────────────────────────────────
# Per-node sample grid  (n_groups rows  ×  3 cols: nodes -001/-002/-003)
# ─────────────────────────────────────────────────────────────────────────────
_SAMPLE_METRICS = [
    ("hs23",          "HS23",         "HEP-Score (HS23)"),
    ("power_w",       "Power",        "Power (W)"),
    ("load5",         "Load5",        "Load avg (5 min)"),
    ("temp_c",        "Temperature",  "°C"),
    ("hs23_per_watt", "HS23 / Watt",  "HS23 / W"),
]
_SAMPLE_NODES = ["001", "002", "003"]

def plot_node_samples(df: pd.DataFrame, out_dir: Path) -> None:
    """
    For each metric: one figure with layout  (n_groups × 3).
    Rows = hardware groups, columns = node -001 / -002 / -003.
    Each cell = daily mean time-series for that specific node.
    Missing nodes get a grey 'no data' placeholder.
    """
    tags   = sorted(df["tag"].unique())
    n_rows = len(tags)
    n_cols = len(_SAMPLE_NODES)

    for col, metric_label, ylabel in _SAMPLE_METRICS:
        if col not in df.columns or df[col].isna().all():
            log.warning("No data for '%s' — skipping node sample plot", col)
            continue

        fig, axes = plt.subplots(
            n_rows, n_cols,
            figsize=(5 * n_cols, 2.6 * n_rows),
            sharex=False, squeeze=False,
        )

        for row_i, tag in enumerate(tags):
            color = GROUP_COLORS.get(tag, "#555")
            for col_i, idx in enumerate(_SAMPLE_NODES):
                ax      = axes[row_i][col_i]
                pattern = f"wn-{tag}-{idx}"
                node_df = df[
                    df["instance"].str.contains(pattern, regex=False) &
                    df[col].notna()
                ].copy()

                if node_df.empty:
                    ax.set_facecolor("#f5f5f5")
                    ax.text(0.5, 0.5, "no data", ha="center", va="center",
                            transform=ax.transAxes,
                            fontsize=9, color="#aaa", style="italic")
                else:
                    node_df["date"] = node_df["timestamp"].dt.date
                    daily = node_df.groupby("date")[col].mean().reset_index()
                    dates = pd.to_datetime(daily["date"])
                    vals  = daily[col]
                    ax.plot(dates, vals, color=color, linewidth=1.0)
                    ax.fill_between(dates, vals, alpha=0.13, color=color)
                    ax.text(0.97, 0.05, f"μ={vals.mean():.3g}",
                            ha="right", va="bottom", transform=ax.transAxes,
                            fontsize=7, color=color, alpha=0.9)

                ax.grid(axis="y", alpha=0.2)
                _date_axis(ax)
                ax.tick_params(axis="x", labelsize=6, labelrotation=30)
                ax.tick_params(axis="y", labelsize=7)

                if row_i == 0:
                    ax.set_title(f"node  -{idx}", fontsize=10,
                                 fontweight="bold", color="#333")
                if col_i == 0:
                    ax.set_ylabel(tag, fontsize=10,
                                  fontweight="bold", color=color)

        fig.suptitle(
            f"Per-node time series: {metric_label}"
            f"  (representative nodes  -001 / -002 / -003)",
            fontsize=13, y=1.01,
        )
        fig.text(0.02, 0.5, ylabel, va="center", rotation="vertical", fontsize=10)
        fig.tight_layout(rect=[0.04, 0, 1, 1])
        path = out_dir / f"node_samples_{col}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        log.info("Saved  %s", path)

# ─────────────────────────────────────────────────────────────────────────────
# CHEP2026 bonus plots
# ─────────────────────────────────────────────────────────────────────────────
def plot_power_efficiency_scatter(df: pd.DataFrame, out_dir: Path) -> None:
    """
    Scatter: per-node daily mean power (x) vs HS23/Watt (y).
    Colour = hardware group, marker size proportional to CPU frequency.
    One dot = one node-day.  Reveals the throttling Pareto frontier.
    """
    if df["hs23_per_watt"].isna().all() or df["power_w"].isna().all():
        log.warning("Insufficient data for power/efficiency scatter — skipping"); return

    sub = df[df["power_w"].notna() & df["hs23_per_watt"].notna()].copy()
    sub["date"] = sub["timestamp"].dt.date
    daily = (sub.groupby(["instance","date","tag"])[["power_w","hs23_per_watt","freq_ghz"]]
               .mean().reset_index())

    fmin, fmax = daily["freq_ghz"].min(), daily["freq_ghz"].max()
    frange = max(fmax - fmin, 0.01)
    daily["msize"] = 10 + 70 * (daily["freq_ghz"] - fmin) / frange

    fig, ax = plt.subplots(figsize=(10, 6))
    for tag in sorted(daily["tag"].unique()):
        td = daily[daily["tag"] == tag]
        ax.scatter(td["power_w"], td["hs23_per_watt"],
                   s=td["msize"], alpha=0.45,
                   color=GROUP_COLORS.get(tag, "#aaa"),
                   marker=GROUP_MARKERS.get(tag, "o"),
                   label=tag, edgecolors="none")

    ax.set_xlabel("Node power draw (W)", fontsize=11)
    ax.set_ylabel("HS23 / Watt", fontsize=11)
    ax.set_title("Power vs HS23/Watt per node-day\n"
                 "(marker size ∝ CPU frequency — larger dot = higher freq)", fontsize=12)
    ax.grid(alpha=0.2)

    # Two-section legend: groups + freq scale
    from matplotlib.lines import Line2D
    group_handles = [
        Line2D([0],[0], marker=GROUP_MARKERS.get(t,"o"), color="w",
               markerfacecolor=GROUP_COLORS.get(t,"#aaa"),
               markersize=8, label=t)
        for t in sorted(daily["tag"].unique())
    ]
    size_handles = [
        Line2D([0],[0], marker="o", color="w", markerfacecolor="#555",
               markersize=np.sqrt(10 + 70*(f-fmin)/frange),
               label=f"{f:.2f} GHz")
        for f in [fmin, (fmin+fmax)/2, fmax]
    ]
    leg1 = ax.legend(handles=group_handles, title="hardware group",
                     fontsize=8, framealpha=0.7, loc="upper right")
    ax.add_artist(leg1)
    ax.legend(handles=size_handles, title="freq scale",
              fontsize=8, framealpha=0.7, loc="lower right")

    fig.tight_layout()
    path = out_dir / "chep_power_efficiency_scatter.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)


def plot_hs23_per_watt_cdf(df: pd.DataFrame, out_dir: Path) -> None:
    """
    CDF of HS23/Watt per hardware group.
    Read as: 'X% of node-time-steps were at or below Y HS23/W.'
    Rightward shift = better efficiency distribution.
    """
    if df["hs23_per_watt"].isna().all():
        log.warning("No power data — skipping CDF plot"); return

    fig, ax = plt.subplots(figsize=(9, 5))
    for tag in sorted(df["tag"].unique()):
        vals = df.loc[(df["tag"]==tag) & df["hs23_per_watt"].notna(),
                      "hs23_per_watt"].values
        if len(vals) == 0:
            continue
        vals_sorted = np.sort(vals)
        cdf = np.arange(1, len(vals_sorted)+1) / len(vals_sorted)
        ax.plot(vals_sorted, cdf*100,
                color=GROUP_COLORS.get(tag,"#555"),
                linewidth=1.8, label=tag)

    ax.set_xlabel("HS23 / Watt", fontsize=11)
    ax.set_ylabel("Cumulative %", fontsize=11)
    ax.set_title("CDF of HS23/Watt per hardware group\n"
                 "(rightward = better power efficiency)", fontsize=12)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax.axhline(50, color="#aaa", linewidth=0.8, linestyle="--", alpha=0.6,
               label="median")
    ax.legend(fontsize=9, framealpha=0.7)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    path = out_dir / "chep_hs23_per_watt_cdf.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved  %s", path)


def plot_summary_table(df: pd.DataFrame, out_dir: Path) -> None:
    """
    Rendered matplotlib table — one row per hardware group.
    Designed as a CHEP slide backup: no code needed to display it.
    """
    tags = sorted(df["tag"].unique())
    col_labels = ["Group","Nodes","Mean freq\n(GHz)","Mean HS23",
                  "Nominal HS23","Mean power\n(W)","HS23/W","Efficiency\n(%)"]
    col_fmts   = ["{}","{}","{:.2f}","{:.0f}","{:.0f}","{:.0f}","{:.3f}","{:.1f}%"]

    table_text = []
    for tag in tags:
        sub  = df[df["tag"] == tag]
        pw   = sub["power_w"].dropna()
        h23w = sub["hs23_per_watt"].dropna()
        nom  = NOMINAL_HS23.get(tag, float("nan"))
        m    = sub["hs23"].mean()
        row  = [tag, sub["instance"].nunique(),
                sub["freq_ghz"].mean(), m, nom,
                pw.mean()   if not pw.empty   else float("nan"),
                h23w.mean() if not h23w.empty else float("nan"),
                100.0*m/nom if nom else float("nan")]
        table_text.append([
            fmt.format(v) if not (isinstance(v,float) and np.isnan(v)) else "—"
            for fmt, v in zip(col_fmts, row)
        ])

    n_rows = len(tags)
    n_cols = len(col_labels)
    fig, ax = plt.subplots(figsize=(max(12, n_cols*1.6), max(2.5, n_rows*0.55+1.3)))
    ax.axis("off")
    tbl = ax.table(cellText=table_text, colLabels=col_labels,
                   cellLoc="center", loc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1.0, 1.7)

    for c in range(n_cols):
        cell = tbl[0, c]
        cell.set_facecolor("#2b2b2b")
        cell.set_text_props(color="white", fontweight="bold")

    for r, tag in enumerate(tags, start=1):
        tbl[r,0].set_facecolor(GROUP_COLORS.get(tag,"#ccc"))
        tbl[r,0].set_text_props(color="white", fontweight="bold")
        bg = "#f7f7f7" if r % 2 == 0 else "white"
        for c in range(1, n_cols):
            tbl[r,c].set_facecolor(bg)

    ax.set_title("Hardware group performance summary",
                 fontsize=13, pad=12, fontweight="bold")
    fig.tight_layout()
    path = out_dir / "chep_summary_table.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Saved  %s", path)

# ─────────────────────────────────────────────────────────────────────────────
# CSV & OpenMetrics
# ─────────────────────────────────────────────────────────────────────────────
def save_csv(df: pd.DataFrame, out_dir: Path) -> None:
    path = out_dir / "hs23_history.csv"
    df.to_csv(path, index=False)
    log.info("Saved  %s  (%d rows)", path, len(df))

def write_openmetrics(df: pd.DataFrame, out_path: Path) -> None:
    log.info("Writing OpenMetrics backfill file → %s", out_path)
    n = 0
    with out_path.open("w") as fh:
        fh.write("# HELP node_hs23_score Interpolated HEP-Score (HS23) from CPU frequency\n")
        fh.write("# TYPE node_hs23_score gauge\n")
        for _, row in df.sort_values(["timestamp","instance"]).iterrows():
            ts_ms = int(row["timestamp"].timestamp() * 1000)
            fh.write(f'node_hs23_score{{instance="{row["instance"]}",tag="{row["tag"]}"}} '
                     f'{row["hs23"]:.4f} {ts_ms}\n')
            n += 1
        fh.write("# EOF\n")
    log.info("Wrote %d data points to %s", n, out_path)
    log.info("Backfill: promtool tsdb create-blocks-from openmetrics %s /var/lib/prometheus", out_path)
    log.info("Reload:   curl -X POST http://prometheus:9090/-/reload")

# ─────────────────────────────────────────────────────────────────────────────
# Exclusion list
# ─────────────────────────────────────────────────────────────────────────────
def load_exclusions(csv_list: str | None, file_path: str | None) -> set[str]:
    excl: set[str] = set()
    if csv_list:
        for tok in csv_list.split(","):
            tok = tok.strip()
            if tok:
                excl.add(tok)
    if file_path:
        p = Path(file_path)
        if not p.exists():
            log.error("Exclusion file not found: %s", file_path); sys.exit(1)
        for line in p.read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                excl.add(line)
    if excl:
        log.info("Exclusions (%d): %s", len(excl), sorted(excl))
    return excl

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def parse_dt(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "T" not in s:
        s += "T00:00:00+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Historical HS23 analysis + Prometheus backfill generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--prometheus-url", default="http://localhost:9090", metavar="URL")
    parser.add_argument("--start", default="2024-01-01T00:00:00Z", metavar="ISO8601")
    parser.add_argument("--end",   default=None, metavar="ISO8601", help="Default: now")
    parser.add_argument("--step",  default="15m", metavar="DURATION")
    parser.add_argument("--chunk-days", type=int, default=30, metavar="N")
    parser.add_argument("--out-dir",    default="./hs23_output", metavar="DIR")
    parser.add_argument("--write-om",   default=None, metavar="PATH")
    parser.add_argument("--no-plots",        action="store_true")
    parser.add_argument("--no-node-samples", action="store_true",
                        help="Skip per-node grid plots (faster run)")
    parser.add_argument("--no-chep",         action="store_true",
                        help="Skip CHEP2026 summary plots")
    parser.add_argument("--debug",           action="store_true")
    parser.add_argument("--reliability-only",action="store_true",
                        help="Reliability audit only; exit after CSV + chart")
    parser.add_argument("--exclude",      default=None, metavar="LIST")
    parser.add_argument("--exclude-file", default=None, metavar="FILE")

    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    start   = parse_dt(args.start)
    end     = parse_dt(args.end) if args.end else datetime.now(tz=timezone.utc)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    exclude = load_exclusions(args.exclude, args.exclude_file)

    log.info("Window: %s → %s  (step=%s)", start.date(), end.date(), args.step)

    # Reliability audit (always runs)
    rel = compute_reliability(args.prometheus_url, start, end,
                              args.step, args.chunk_days)
    save_reliability_csv(rel, out_dir)
    if not args.no_plots:
        plot_reliability(rel, out_dir)

    if args.reliability_only:
        log.info("Done. Review %s/node_reliability.csv", out_dir)
        log.info("Re-run with --exclude <nodes> or --exclude-file bad_nodes.txt")
        return

    # Full data fetch
    raw = fetch_all_metrics(args.prometheus_url, start, end,
                            args.step, args.chunk_days, exclude)
    df  = build_dataframe(raw)

    # Summary
    log.info("─── Summary ─────────────────────────────────────────────────────────")
    for tag in sorted(df["tag"].unique()):
        sub = df[df["tag"] == tag]
        pw  = sub["power_w"].dropna()
        log.info("  %-5s  nodes=%-3d  freq=[%.2f–%.2f] GHz  HS23=[%.0f–%.0f]  power=[%.0f–%.0f] W",
            tag, sub["instance"].nunique(),
            sub["freq_ghz"].min(), sub["freq_ghz"].max(),
            sub["hs23"].min(), sub["hs23"].max(),
            pw.min() if not pw.empty else float("nan"),
            pw.max() if not pw.empty else float("nan"))
    log.info("  TOTAL  nodes=%-3d  avg_total_hs23=%.0f",
             df["instance"].nunique(),
             df.groupby("timestamp")["hs23"].sum().mean())
    log.info("─────────────────────────────────────────────────────────────────────")

    save_csv(df, out_dir)

    if not args.no_plots:
        log.info("Generating group-level plots …")
        plot_cluster_total(df, out_dir)
        plot_by_group(df, out_dir)
        plot_frequency_efficiency(df, out_dir)
        plot_freq_vs_hs23_scatter(df, out_dir)
        plot_power(df, out_dir)
        plot_load(df, out_dir)
        plot_temperature(df, out_dir)
        plot_hs23_per_watt(df, out_dir)

        if not args.no_node_samples:
            log.info("Generating per-node sample grids …")
            plot_node_samples(df, out_dir)

        if not args.no_chep:
            log.info("Generating CHEP2026 summary plots …")
            plot_power_efficiency_scatter(df, out_dir)
            plot_hs23_per_watt_cdf(df, out_dir)
            plot_summary_table(df, out_dir)

        log.info("All plots saved to %s/", out_dir)

    if args.write_om:
        write_openmetrics(df, Path(args.write_om))

if __name__ == "__main__":
    main()
