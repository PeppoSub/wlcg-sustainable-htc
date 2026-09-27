#!/usr/bin/env python3

# Carbon Intensity Visualiser — Glasgow / Scotland by Emanuele & Claude.AI
# Reads two JSON files produced by the Carbon Intensity API:
#
#  ScotEMix_2days.json   - 48 h (or longer) regional generation-mix forecast
#                          (curl .../regional/intensity/.../fw48h/postcode/G12)
#
#  CarbonFactors.json    - static gCO2/kWh factors per fuel type
#                          (curl .../intensity/factors)
#
# Produces:
#  1. plot_1_generation_mix.png      - stacked-area: generation share (%)
#  2. plot_2_co2_contribution.png    - stacked-area: CO2 contribution per source
#  3. plot_3_total_intensity.png     - line chart: total gCO2/kWh over time
#  4. <out>.csv                      - hourly generation shares (%) + CO2 totals
#                                      with a factor row and an empty spacer row
#                                      so Excel formulas are straightforward
#  5. Console summary                - best / worst hours to run appliances
#
# CSV layout:
# 
#  Row 1  : column headers with units
#  Row 2  : gCO2/kWh factor for each fuel (metadata row, easy to reference)
#  Row 3  : empty spacer
#  Row 4+ : hourly data — generation shares in % and CO2 totals in gCO2/kWh
#
# Usage:
#
#  pip install matplotlib pandas numpy
#  python plot_intensity.py --mix G12eMix_2days.json --factors CarbonFactors.json --out co2_hourly.csv
#

import json
import argparse
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import MaxNLocator

# ── colour palette (one per fuel type) ──────────────────────────────────────
FUEL_COLOURS = {
    "gas":       "#F4A261",
    "coal":      "#264653",
    "biomass":   "#2A9D8F",
    "nuclear":   "#9B5DE5",
    "hydro":     "#4895EF",
    "wind":      "#90E0EF",
    "solar":     "#FFD166",
    "imports":   "#E9C46A",
    "other":     "#B7B7A4",
    "storage":   "#6D9DC5",
}

# ── mapping: regional API fuel name → factors API key(s) ────────────────────
FUEL_TO_FACTOR_KEY = {
    "gas":      ["Gas (Combined Cycle)", "Gas (Open Cycle)"],
    "coal":     ["Coal"],
    "biomass":  ["Biomass"],
    "nuclear":  ["Nuclear"],
    "hydro":    ["Hydro"],
    "wind":     ["Wind"],
    "solar":    ["Solar"],
    "imports":  ["Dutch Imports", "French Imports", "Irish Imports"],
    "other":    ["Other"],
    "storage":  ["Pumped Storage"],
}


# ════════════════════════════════════════════════════════════════════════════
# Data loading
# ════════════════════════════════════════════════════════════════════════════

def load_mix(path: str) -> pd.DataFrame:
    # Parse the regional generation-mix JSON into a tidy DataFrame.
    # Handles two API shapes:
    #  - postcode/regionid endpoint: data is a single dict with a nested list
    #  - national/multi-region endpoint: data is a list of region dicts

    with open(path) as f:
        raw = json.load(f)

    d = raw["data"]
    if isinstance(d, dict):
        slots = d["data"]
    elif isinstance(d, list) and d and isinstance(d[0], dict) and "data" in d[0]:
        slots = d[0]["data"]
    else:
        slots = d

    rows = []
    for slot in slots:
        ts = pd.to_datetime(slot["from"], utc=True)
        intensity = slot.get("intensity", {}).get("forecast", np.nan)
        mix = {item["fuel"]: item["perc"] for item in slot.get("generationmix", [])}
        mix["_datetime"] = ts
        mix["_intensity"] = intensity
        rows.append(mix)

    df = pd.DataFrame(rows).set_index("_datetime").sort_index()
    df.index.name = "datetime"
    return df


def load_factors(path: str) -> dict:
    # Parse /intensity/factors JSON into {api_key: gCO2/kWh}.
    with open(path) as f:
        raw = json.load(f)
    return raw["data"][0]


def resolve_factor(fuel: str, factors: dict) -> float:
    # Return a single gCO2/kWh value for a regional fuel short-name.
    keys   = FUEL_TO_FACTOR_KEY.get(fuel, [fuel.title()])
    values = [factors.get(k, 0) for k in keys if factors.get(k) is not None]
    return float(np.mean(values)) if values else 0.0


# ════════════════════════════════════════════════════════════════════════════
# Computation
# ════════════════════════════════════════════════════════════════════════════

def build_co2_df(mix_df: pd.DataFrame, factors: dict) -> pd.DataFrame:
    # co2[fuel] = (share_pct / 100) * factor  →  gCO2/kWh contribution.
    fuel_cols = [c for c in mix_df.columns if c != "_intensity"]
    co2 = pd.DataFrame(index=mix_df.index)
    for fuel in fuel_cols:
        co2[fuel] = (mix_df[fuel] / 100.0) * resolve_factor(fuel, factors)
    return co2


def build_hourly(mix_df: pd.DataFrame, co2_df: pd.DataFrame) -> pd.DataFrame:
    # Resample both DataFrames to 1-h means and join into one table.
    fuel_cols = [c for c in mix_df.columns if c != "_intensity"]
    share_h   = mix_df[fuel_cols].resample("1h").mean()

    totals = co2_df.copy()
    totals["total_computed"] = co2_df.sum(axis=1)
    totals["total_api"]      = mix_df["_intensity"]
    totals_h = totals[["total_computed", "total_api"]].resample("1h").mean()

    return share_h.join(totals_h).round(1)


# ════════════════════════════════════════════════════════════════════════════
# CSV output
# ════════════════════════════════════════════════════════════════════════════

def write_csv(path: str, hourly: pd.DataFrame, fuel_cols: list, factors: dict):
    # Write CSV with:
    #  Row 1  - headers with units
    #  Row 2  - gCO2/kWh factor for each fuel column
    #  Row 3  - empty spacer
    #  Row 4+ - hourly data (generation share in %, CO2 totals in gCO2/kWh)

    # Rename map: internal name -> display name with unit
    rename = {
        "total_computed": "total_computed (gCO2/kWh)",
        "total_api":      "total_api (gCO2/kWh)",
    }
    for f in fuel_cols:
        rename[f] = f"{f} (% share)"

    # Build ordered column list
    ordered = (
        ["total_computed (gCO2/kWh)", "total_api (gCO2/kWh)"]
        + [f"{f} (% share)" for f in fuel_cols]
    )

    out = hourly.rename(columns=rename)
    out = out[[c for c in ordered if c in out.columns]]

    # Factor metadata row
    factor_row = {"total_computed (gCO2/kWh)": "# gCO2/kWh factor:",
                  "total_api (gCO2/kWh)": ""}
    for f in fuel_cols:
        factor_row[f"{f} (% share)"] = resolve_factor(f, factors)

    factor_df = pd.DataFrame([factor_row], index=["factor (gCO2/kWh)"])
    factor_df.index.name = "datetime"

    # Empty spacer row
    spacer_df = pd.DataFrame(
        [[""] * len(out.columns)],
        columns=out.columns,
        index=[""]
    )
    spacer_df.index.name = "datetime"

    combined = pd.concat([factor_df, spacer_df, out])
    combined.to_csv(path)
    print(f"CSV saved → {path}")


# ════════════════════════════════════════════════════════════════════════════
# Adaptive x-axis helper
# ════════════════════════════════════════════════════════════════════════════

def smart_xaxis(ax, index: pd.DatetimeIndex):
    # Pick tick density and label format based on the time span of the data.
    # Rotates labels when they would otherwise crowd each other.

    span_hours = (index[-1] - index[0]).total_seconds() / 3600

    if span_hours <= 50:            # up to ~2 days: every 6 h
        locator   = mdates.HourLocator(byhour=range(0, 24, 6))
        formatter = mdates.DateFormatter("%d %b\n%H:%M")
        rotation  = 0
    elif span_hours <= 200:         # up to ~8 days: every 12 h
        locator   = mdates.HourLocator(byhour=[0, 12])
        formatter = mdates.DateFormatter("%d %b\n%H:%M")
        rotation  = 0
    elif span_hours <= 500:         # up to ~3 weeks: daily
        locator   = mdates.DayLocator(interval=1)
        formatter = mdates.DateFormatter("%d %b")
        rotation  = 45
    elif span_hours <= 1500:        # up to ~2 months: every 2 days
        locator   = mdates.DayLocator(interval=2)
        formatter = mdates.DateFormatter("%d %b")
        rotation  = 45
    else:                           # longer: weekly
        locator   = mdates.WeekdayLocator(byweekday=0)
        formatter = mdates.DateFormatter("%d %b '%y")
        rotation  = 45

    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    ax.tick_params(axis="x", labelsize=8, rotation=rotation)
    if rotation:
        for label in ax.get_xticklabels():
            label.set_ha("right")


# ════════════════════════════════════════════════════════════════════════════
# Individual plot functions (each saves its own PNG)
# ════════════════════════════════════════════════════════════════════════════

def _base_fig(title: str, ylabel: str, suptitle: str):
    fig, ax = plt.subplots(figsize=(14, 5))
    if suptitle:
        fig.suptitle(suptitle, fontsize=11, y=1.01)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=8)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_xlabel("Time (UTC)", fontsize=10)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    return fig, ax


def plot_generation_mix(mix_df: pd.DataFrame, fuel_cols: list, suptitle: str, outfile: str):
    fig, ax = _base_fig("Generation Mix  (% share)", "Share (%)", suptitle)

    data = mix_df[fuel_cols].copy()
    row_sum = data.sum(axis=1).replace(0, np.nan)
    data = data.div(row_sum, axis=0) * 100

    colours = [FUEL_COLOURS.get(f, "#AAAAAA") for f in fuel_cols]
    ax.stackplot(data.index, [data[f] for f in fuel_cols],
                 labels=fuel_cols, colors=colours, alpha=0.85)
    ax.set_ylim(0, 100)
    smart_xaxis(ax, mix_df.index)

    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labels[::-1],
              loc="upper left", bbox_to_anchor=(1.01, 1),
              fontsize=8, frameon=True, title="Fuel")
    fig.tight_layout()
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    print(f"Plot saved → {outfile}")
    plt.close(fig)


def plot_co2_contribution(co2_df: pd.DataFrame, fuel_cols: list,
                          suptitle: str, outfile: str):
    fig, ax = _base_fig("CO2 Contribution per Source", "gCO2 / kWh", suptitle)

    colours = [FUEL_COLOURS.get(f, "#AAAAAA") for f in fuel_cols]
    ax.stackplot(co2_df.index, [co2_df[f] for f in fuel_cols],
                 labels=fuel_cols, colors=colours, alpha=0.85)
    smart_xaxis(ax, co2_df.index)

    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labels[::-1],
              loc="upper left", bbox_to_anchor=(1.01, 1),
              fontsize=8, frameon=True, title="Fuel")
    fig.tight_layout()
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    print(f"Plot saved → {outfile}")
    plt.close(fig)


def plot_total_intensity(hourly: pd.DataFrame, suptitle: str, outfile: str):
    fig, ax = _base_fig("Total CO2 Intensity — Hourly Average", "gCO2 / kWh", suptitle)

    ax.plot(hourly.index, hourly["total_computed"],
            label="Computed (mix × factors)", color="#E63946", lw=1.8)
    if hourly["total_api"].notna().any():
        ax.plot(hourly.index, hourly["total_api"],
                label="API forecast", color="#457B9D", lw=1.8, linestyle="--")

    smart_xaxis(ax, hourly.index)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(fontsize=9)
    ax.grid(linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    print(f"Plot saved → {outfile}")
    plt.close(fig)


# ════════════════════════════════════════════════════════════════════════════
# Console summary
# ════════════════════════════════════════════════════════════════════════════

def print_summary(hourly: pd.DataFrame):
    col   = "total_computed"
    best  = hourly[col].nsmallest(5)
    worst = hourly[col].nlargest(5)
    print("\n" + "=" * 57)
    print("  5 BEST hours to run high-power appliances")
    print("-" * 57)
    for ts, val in best.items():
        print(f"  {ts.strftime('%a %d %b  %H:%M UTC'):<30}  {val:6.1f} gCO2/kWh")
    print("\n  5 WORST hours (avoid if possible)")
    print("-" * 57)
    for ts, val in worst.items():
        print(f"  {ts.strftime('%a %d %b  %H:%M UTC'):<30}  {val:6.1f} gCO2/kWh")
    print("=" * 57)


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Carbon Intensity Visualiser")
    parser.add_argument("--mix",     default="ScotEMix_2days.json",
                        help="Regional generation-mix JSON")
    parser.add_argument("--factors", default="CarbonFactors.json",
                        help="Intensity factors JSON")
    parser.add_argument("--out",     default="co2_hourly.csv",
                        help="Output CSV filename")
    args = parser.parse_args()

    # ── load ─────────────────────────────────────────────────────────────────
    print(f"Loading mix data from:  {args.mix}")
    mix_df = load_mix(args.mix)
    print(f"  -> {len(mix_df)} 30-min slots  |  "
          f"{mix_df.index[0]}  ->  {mix_df.index[-1]}")

    print(f"Loading factors from:   {args.factors}")
    factors = load_factors(args.factors)
    print(f"  -> {len(factors)} fuel types")

    fuel_cols = [c for c in mix_df.columns if c != "_intensity"]
    co2_df    = build_co2_df(mix_df, factors)
    hourly    = build_hourly(mix_df, co2_df)

    # ── CSV ───────────────────────────────────────────────────────────────────
    write_csv(args.out, hourly, fuel_cols, factors)

    # ── console ───────────────────────────────────────────────────────────────
    print("\n-- Hourly CO2 intensity ------------------------------------------")
    print(f"  {'datetime (UTC)':<26}  {'computed':>10}  {'API forecast':>12}")
    print("-" * 58)
    for ts, row in hourly.iterrows():
        api_s = f"{row['total_api']:10.1f}" if pd.notna(row["total_api"]) else "       n/a"
        print(f"  {ts.strftime('%Y-%m-%d %H:%M'):<26}  "
              f"{row['total_computed']:10.1f}  {api_s:>12}  gCO2/kWh")
    print_summary(hourly)

    # ── plots (each in a separate PNG) ────────────────────────────────────────
    label    = args.mix.replace(".json", "").replace("_", " ")
    suptitle = f"Carbon Intensity — {label}"

    plot_generation_mix(   mix_df,   fuel_cols, suptitle, "plot_1_generation_mix.png")
    plot_co2_contribution( co2_df,   fuel_cols, suptitle, "plot_2_co2_contribution.png")
    plot_total_intensity(  hourly,              suptitle, "plot_3_total_intensity.png")


if __name__ == "__main__":
    main()
