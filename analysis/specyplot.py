#!/usr/bin/env python3
"""
specyplot.py  —  Plot specific nodes over a specific period from hs23_history.csv
v0.x by Emanuele & Claude
==================================================================================

USAGE
-----
  python3 specyplot.py -n wn-a23-001,wn-d22-003 \
                       -p 2026-01-06,2026-05-01 \
                       -s ./hs23_output/hs23_history.csv

  # output goes to same dir as the CSV by default, or specify -o
  python3 specyplot.py -n wn-a23-001 -p 2026-01-06,2026-05-01 \
                       -s ./hs23_output/hs23_history.csv -o ./my_slides/

FLAGS
-----
  -n / --nodes     Comma-separated node names (substring match: 'a23-001' works too)
  -p / --period    Start,end dates as YYYY-MM-DD (always midnight UTC)
  -s / --source    Path to hs23_history.csv produced by claude_history.py
  -o / --out-dir   Output directory (default: same directory as the CSV)
  --no-plots       Only write the filtered CSV, skip plots
  --dpi            Plot resolution in DPI (default: 150)

OUTPUTS
-------
  filtered_<nodes>_<start>_<end>.csv   — filtered data for the selected nodes/period
  timeseries_<nodes>_<start>_<end>.png — all metrics in one multi-panel figure
  (one file per node if --split, otherwise all nodes overlaid per metric)

NOTES
-----
  - Node matching is a substring search: 'a23-001' matches 'wn-a23-001' and
    'wn-a23-001:9100'. Case-insensitive.
  - If a node has no data in the requested period it is skipped with a warning.
  - hs23_per_watt is recalculated from hs23/power_w if the column is missing.

DEPENDENCIES
------------
  pip install pandas matplotlib
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

# ── colours and line styles (borrowed from claude_history.py) ─────────────────
GROUP_COLORS: dict[str, str] = {
    "h16": "#888780", "h17": "#185FA5",
    "d20": "#D85A30", "d21": "#BA7517", "d22": "#EF9F27",
    "a23": "#1D9E75", "m24": "#7F77DD",
}
LINE_STYLES = ["-", "--", "-.", ":", (0,(3,1,1,1)), (0,(5,2))]
MARKERS     = ["", "o", "s", "^", "v", "D"]   # first node no marker (clean line)


def _color_for(instance: str) -> str:
    """Pick group colour by matching tag pattern in instance name."""
    import re
    m = re.search(r"wn-([a-z][0-9]+)-", instance)
    if m:
        return GROUP_COLORS.get(m.group(1), "#444444")
    return "#444444"


def _date_axis(ax, dates: pd.Series) -> None:
    span_days = (dates.max() - dates.min()).days
    if span_days <= 14:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax.xaxis.set_major_locator(mdates.DayLocator())
    elif span_days <= 90:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())


# ── metric definitions ────────────────────────────────────────────────────────
METRICS = [
    ("freq_ghz",     "CPU Frequency (GHz)", None),
    ("hs23",         "HEP-Score (HS23)",    None),
    ("power_w",      "Power (W)",           None),
    ("hs23_per_watt","HS23 / Watt",         None),
]


# ── loading & filtering ───────────────────────────────────────────────────────

def load_and_filter(csv_path: Path,
                    nodes: list[str],
                    start: datetime,
                    end: datetime) -> pd.DataFrame:
    print(f"Loading {csv_path} …")
    df = pd.read_csv(csv_path, parse_dates=["timestamp"])

    # Normalise timezone: make timestamps UTC-aware if they aren't
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")

    # Ensure hs23_per_watt exists
    if "hs23_per_watt" not in df.columns:
        if "hs23" in df.columns and "power_w" in df.columns:
            df["hs23_per_watt"] = df["hs23"] / df["power_w"].replace(0, float("nan"))
        else:
            df["hs23_per_watt"] = float("nan")

    # Period filter
    mask_period = (df["timestamp"] >= start) & (df["timestamp"] <= end)
    df = df[mask_period]
    if df.empty:
        print(f"ERROR: No data in period {start.date()} – {end.date()}")
        sys.exit(1)

    # Node filter (substring, case-insensitive)
    matched_nodes: list[str] = []
    for node_query in nodes:
        q = node_query.lower().strip()
        hits = df["instance"].str.lower().str.contains(q, regex=False)
        found = df.loc[hits, "instance"].unique().tolist()
        if not found:
            print(f"WARNING: no data for node query '{node_query}' in this period — skipping")
        else:
            matched_nodes.extend(found)

    matched_nodes = sorted(set(matched_nodes))
    if not matched_nodes:
        print("ERROR: none of the requested nodes have data. Check node names and period.")
        sys.exit(1)

    df = df[df["instance"].isin(matched_nodes)]
    print(f"Found {len(matched_nodes)} node(s): {', '.join(matched_nodes)}")
    print(f"Period: {start.date()} → {end.date()}   Rows: {len(df)}")
    return df, matched_nodes


# ── plotting ──────────────────────────────────────────────────────────────────

def plot_timeseries(df: pd.DataFrame,
                    nodes: list[str],
                    start: datetime,
                    end: datetime,
                    out_path: Path,
                    dpi: int = 150) -> None:
    """
    One figure, one subplot per metric.  All nodes overlaid with distinct
    colour+linestyle combinations.  Daily mean is plotted (raw data can be
    very dense at 15 min resolution).
    """
    # Which metrics actually have data?
    active = [(col, label, _) for col, label, _ in METRICS
              if col in df.columns and not df[col].isna().all()]

    n_metrics = len(active)
    if n_metrics == 0:
        print("WARNING: no plottable metrics found — skipping plot")
        return

    fig, axes = plt.subplots(n_metrics, 1,
                              figsize=(13, 2.8 * n_metrics),
                              sharex=True, squeeze=False)
    axes = [ax[0] for ax in axes]   # unwrap squeeze=False

    df_plot = df.copy()
    # If already daily (timestamp is midnight), use as-is; otherwise aggregate
    is_daily = (df_plot["timestamp"].dt.time == pd.Timestamp("00:00:00").time()).all()
    if not is_daily:
        df_plot["date"] = df_plot["timestamp"].dt.date

    all_dates = pd.Series(dtype="object")

    for ax_i, (col, ylabel, _) in enumerate(active):
        ax = axes[ax_i]
        for n_i, node in enumerate(nodes):
            node_df = df_plot[df_plot["instance"] == node]
            if node_df[col].isna().all():
                continue
            if is_daily:
                daily = node_df[["timestamp", col]].rename(columns={"timestamp":"date"})
                daily = daily.dropna(subset=[col])
            else:
                daily = node_df.groupby("date")[col].mean().reset_index()
            dates = pd.to_datetime(daily["date"])
            all_dates = pd.concat([all_dates, dates])
            vals  = daily[col]

            color  = _color_for(node)
            lstyle = LINE_STYLES[n_i % len(LINE_STYLES)]
            marker = MARKERS[n_i % len(MARKERS)]
            msize  = 3 if marker else 0

            ax.plot(dates, vals,
                    color=color, linestyle=lstyle,
                    linewidth=1.4, marker=marker, markersize=msize,
                    label=node, alpha=0.9)

        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(axis="y", alpha=0.25)
        ax.grid(axis="x", alpha=0.12)

        # Legend only on the last subplot (HS23/Watt), top right
        if ax_i == n_metrics - 1:
            ax.legend(fontsize=8, framealpha=0.7, loc="upper right")

    if not all_dates.empty:
        _date_axis(axes[-1], all_dates)

    fig.autofmt_xdate()
    node_str = ", ".join(nodes) if len(nodes) <= 3 else f"{len(nodes)} nodes"
    fig.suptitle(
        f"Node time series: {node_str}\n"
        f"{start.date()} → {end.date()}  (daily means)",
        fontsize=12, y=1.01,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved  {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD as UTC midnight."""
    d = datetime.strptime(s.strip(), "%Y-%m-%d")
    return d.replace(tzinfo=timezone.utc)


def safe_name(s: str) -> str:
    """Strip characters unsafe for filenames."""
    import re
    return re.sub(r"[^\w\-]", "_", s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot specific nodes over a period from hs23_history.csv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-n", "--nodes",  required=True,
                        help="Comma-separated node names/substrings")
    parser.add_argument("-p", "--period", required=True,
                        help="Start,end as YYYY-MM-DD,YYYY-MM-DD")
    parser.add_argument("-s", "--source", required=True,
                        help="Path to hs23_history.csv")
    parser.add_argument("-o", "--out-dir", default=None,
                        help="Output directory (default: same dir as CSV)")
    parser.add_argument("--no-plots", action="store_true",
                        help="Write filtered CSV only, skip plots")
    parser.add_argument("--dpi", type=int, default=150)

    args = parser.parse_args()

    # Parse nodes
    nodes_raw = [n.strip() for n in args.nodes.split(",") if n.strip()]

    # Parse period
    parts = args.period.split(",")
    if len(parts) != 2:
        print("ERROR: --period must be two comma-separated dates: YYYY-MM-DD,YYYY-MM-DD")
        sys.exit(1)
    start = parse_date(parts[0])
    end   = parse_date(parts[1]) + timedelta(days=1) - timedelta(seconds=1)  # inclusive end

    # Paths
    csv_path = Path(args.source)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    out_dir = Path(args.out_dir) if args.out_dir else csv_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build output filename stem
    node_tag  = safe_name("_".join(nodes_raw[:3]))   # cap at 3 to keep names sane
    date_tag  = f"{parts[0]}_{parts[1]}"
    stem      = f"filtered_{node_tag}_{date_tag}"

    # Load & filter
    df, matched_nodes = load_and_filter(csv_path, nodes_raw, start, end)

    # Build daily averages  (mean of all numeric metrics per node per day)
    metric_cols = [c for c in ("freq_ghz","hs23","power_w","load5","temp_c","hs23_per_watt")
                   if c in df.columns]
    df["date"] = df["timestamp"].dt.normalize()   # midnight UTC timestamp
    daily_df = (df.groupby(["date","instance","tag"])[metric_cols]
                  .mean()
                  .reset_index()
                  .rename(columns={"date": "timestamp"}))
    daily_df.sort_values(["instance","timestamp"], inplace=True)

    # Save daily-average CSV (small, easy to hand-edit for mix & match)
    csv_out = out_dir / f"{stem}.csv"
    daily_df.to_csv(csv_out, index=False)
    print(f"Saved  {csv_out}  ({len(daily_df)} rows, daily averages)")

    # Plot  —  works on daily_df directly since plot_timeseries does daily groupby anyway
    if not args.no_plots:
        plot_path = out_dir / f"{stem}.png"
        plot_timeseries(daily_df, matched_nodes, start, end, plot_path, dpi=args.dpi)


if __name__ == "__main__":
    main()
