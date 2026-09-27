## 'power_plus.py' v0.2(k) by Emanuele ##
# it splits the query in manageable time chunks (10 days),
# it queries Prometheus / VictoriaMetrics via the API and combines results,
# it aggregates the core usage data by VOs and node series,
# it estimates the power usage per VO by adding up the power drain on each node serie,  
# it extracts IPMI power per node series to validate to the estimated power per VO.

import sys
import os
import requests
import datetime
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from requests_toolbelt.utils import dump

# --- CONFIG ---

# Configuration
DEBUG = False  # Set to True to enable debug prints
PROMETHEUS_URL = "http://prometheus.local:9090/api/v1/query_range"        # Prometheus
# PROMETHEUS_URL = "http://victoriametrics.local:8428/api/v1/query_range"  # VictoriaMetrics
DWEEKS = 4     # default time period to plot/integrate
STEP = "300"   # seconds per sample (600 = 10 min)
CHUNKYS = 10   # days per chunk when splitting
# Forward-fill settings:
# =0 => unlimited forward-fill; >0 => limit to that many steps; <0 => no forward-fill
FWD_FILL_STEPS = 1

# Static Server Data
static_data = {
    "HP2017":       {"threads": 40,  "pow_max": 229,  "pow_qnt": 212},   # h17 (x86)
    "DELL2020":     {"threads": 128, "pow_max": 474,  "pow_qnt": 446},   # d20 (x86)
    "Ampere2024":   {"threads": 128, "pow_max": 450,  "pow_qnt": 430},   # m24 (ARM)
    # ... add the hardware at your site and fill groups below!
}

# Hardware groups 
ARM_GROUPS = {"HP2017", "DELL2020", "..."}
x86_GROUPS = {"Ampere2024", "..."}

# Pre-calculate power per core
for grp, d in static_data.items():
    d['w_per_core'] = d['pow_max'] / d['threads']

# Utility: fetch a range of metrics
def fetch_range(query, start, end, step):
    params = { 'query': query,
               'start': start.isoformat()+"Z",
               'end':   end.isoformat()+"Z",
               'step':  f"{step}s" }
    r = requests.get(PROMETHEUS_URL, params=params)
    if DEBUG:
        print(dump.dump_all(r).decode())
    r.raise_for_status()
    return r.json()['data']['result']

# Command-line parameter for weeks
try:
    WEEKS = int(sys.argv[1]) if len(sys.argv) > 1 else DWEEKS
except ValueError:
    print(f"Invalid weeks parameter, using default: {DWEEKS}")
    WEEKS = DWEEKS

# --- MAIN ---

# Define overall period
end = datetime.datetime.utcnow()
start = end - datetime.timedelta(weeks=WEEKS)
# Filename tag e.g. 18Feb25-15Apr25
period_tag = f"{start:%d%b%y}-{end:%d%b%y}"
# Prepare output folder
dirname = f"report_{period_tag}"
os.makedirs(dirname, exist_ok=True)

# Common timeline for all series
times = pd.date_range(start=start, end=end, freq=f"{int(STEP)}s")
# Split into chunks
chunks = []
cs = start
while cs < end:
    ce = min(cs + datetime.timedelta(days=CHUNKYS), end)
    chunks.append((cs, ce))
    cs = ce

# Fetch VO slot usage by (vo, group)
utilization_query = 'count(node_condor_cpu{job="workernodes"}) by (vo, group)'
series = {}
for cs, ce in chunks:
    res = fetch_range(utilization_query, cs, ce, STEP)
    for s in res:
        vo = s['metric'].get('vo','undefined')
        grp = s['metric'].get('group')
        if grp not in static_data:
            continue
        idx = [datetime.datetime.utcfromtimestamp(float(v[0])) for v in s['values']]
        vals = [float(v[1]) for v in s['values']]
        df = pd.DataFrame({'slots': vals}, index=idx)
        series.setdefault((vo,grp), []).append(df)

# Merge and forward-fill slot series per (vo,grp)
data = {}
for (vo,grp), lst in series.items():
    df = pd.concat(lst).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    if FWD_FILL_STEPS == 0:
        df = df.reindex(times, method='ffill').fillna(0)
    elif FWD_FILL_STEPS > 0:
        df = df.reindex(times, method='ffill', limit=FWD_FILL_STEPS).fillna(0)
    else:
        df = df.reindex(times).fillna(0)
    data.setdefault(vo, {})[grp] = df

# Build per-VO power series
vo_ts = {}
for vo, grs in data.items():
    df = pd.DataFrame(0, index=times, columns=['x86','ARM'])
    for grp, ddf in grs.items():
        ps = ddf['slots'] * static_data[grp]['w_per_core']
        df['ARM' if grp in ARM_GROUPS else 'x86'] += ps
    vo_ts[vo] = df

# Prepare report lines
txt_lines = []
txt_lines.append(f"Data period: {start:%Y-%m-%d %H:%M:%S}Z to {end:%Y-%m-%d %H:%M:%S}Z\n")
# Compute VO results
results = []
for vo, df in vo_ts.items():
    df = df.fillna(0)
    hrs = (df.index - start).total_seconds()/3600
    e_x86 = np.trapz(df['x86'], hrs)
    e_arm = np.trapz(df['ARM'], hrs)
    total_e = (e_x86 + e_arm) / 1000
    grp_e = {}
    for grp, ddf in data[vo].items():
        ps = ddf['slots'] * static_data[grp]['w_per_core']
        grp_e[grp] = np.trapz(ps.values, hrs)/1000
    now = df.iloc[-1]
    p_x86, p_arm = round(now['x86']), round(now['ARM'])
    p_tot = p_x86 + p_arm
    non0 = (df['x86'] + df['ARM']) != 0
    if non0.any():
        ds = df.index[non0.argmax()].strftime('%Y-%m-%d %H:%M:%S')
        de = df.index[len(df)-1-non0[::-1].argmax()].strftime('%Y-%m-%d %H:%M:%S')
    else:
        ds, de = 'No data','No data'
    results.append((vo, ds, de, total_e, grp_e, p_tot, p_x86, p_arm))
results = sorted(results, key=lambda x: x[3], reverse=True)

# VO report
for vo, ds, de, total_e, grp_e, p_tot, p_x86, p_arm in results:
    txt_lines.append(f"VO {vo} data available period: {ds}Z to {de}Z")
    txt_lines.append(f"VO {vo}:  Integrated Energy over {WEEKS} week(s): {total_e:.0f} kWh")
    x86b = ' + '.join(f"{round(grp_e[g])} kWh ({g})" for g in grp_e if g not in ARM_GROUPS)
    armb = ' + '.join(f"{round(grp_e[g])} kWh ({g})" for g in grp_e if g in ARM_GROUPS)
    txt_lines.append(f"         x86:   {round(sum(grp_e[g] for g in grp_e if g not in ARM_GROUPS))} kWh = {x86b}")
    txt_lines.append(f"         ARM:   {round(sum(grp_e[g] for g in grp_e if g in ARM_GROUPS))} kWh = {armb}")
    txt_lines.append(f"VO {vo}:  Total Power now = {p_tot} W")
    x86p = ' + '.join(f"{static_data[g]['w_per_core']*data[vo][g].iloc[-1]['slots']:.0f} W ({g})" for g in data[vo] if g not in ARM_GROUPS)
    armp = ' + '.join(f"{static_data[g]['w_per_core']*data[vo][g].iloc[-1]['slots']:.0f} W ({g})" for g in data[vo] if g in ARM_GROUPS)
    txt_lines.append(f"         x86:   {p_x86} W = {x86p}")
    txt_lines.append(f"         ARM:   {p_arm} W = {armp}\n")

# IPMI actual power per group via a single sum-by-group query --- not working !!!
groups = list(static_data.keys())
ipmi = {}
ipmi_query = 'sum by(group) (node_sensor_power > 0)'
# one range‐query for all groups
res = fetch_range(ipmi_query, start, end, STEP)
for series in res:
    grp = series['metric']['group']
    # build a pandas Series of power values over time
    times_ip = [datetime.datetime.utcfromtimestamp(float(v[0])) for v in series['values']]
    vals_ip  = [float(v[1]) for v in series['values']]
    df_ip = pd.Series(vals_ip, index=times_ip, name=grp)
    # align to your common timeline & fill gaps
    df_ip = df_ip.reindex(times)
    df_ip = df_ip.fillna(method='ffill').fillna(method='bfill').fillna(0)
    ipmi[grp] = df_ip

# Estimated power per group (sum of VO slots * w_per_core)
est = {}
for grp in groups:
    est_series = np.zeros(len(times))
    for vo in data:
        if grp in data[vo]:
            est_series += data[vo][grp]['slots'] * static_data[grp]['w_per_core']
    est[grp] = pd.Series(est_series, index=times)

# Append group summary to report
txt_lines.append("Group power summary:")
hrs = (times - start).total_seconds()/3600
for grp in groups:
    ip = ipmi.get(grp, pd.Series(0, index=times))
    ei = np.trapz(ip.values, hrs)/1000
    es = est.get(grp, pd.Series(0, index=times))
    ee = np.trapz(es.values, hrs)/1000
    txt_lines.append(f"  {grp}:   IPMI={ei:.0f} kWh  ,  Estimated={ee:.0f} kWh")

# write report
with open(os.path.join(dirname, f"report_{period_tag}.txt"), 'w') as f:
    f.write("\n".join(txt_lines))
print("".join(line+"\n" for line in txt_lines))

# --- PLOTTING ---

# Color map for consistency
group_colors = {
    'HP2017': 'lime',
    'DELL2020': 'blue',
    'Ampere2024': 'magenta'
    # ... define colors based on your hardware groups!
}

# generate plots for core usage and est. power 
for vo, _, _, _, _, _, _, _ in results:
    df = vo_ts[vo]
    plt.figure(figsize=(10,5))
    df['x86'].plot(color='blue', label='x86')
    df['ARM'].plot(color='red', label='ARM')
    plt.ylim(0)
    plt.legend()
    plt.title(f"VO {vo} Power Usage Over Last {WEEKS} Week(s)")
    plt.tight_layout()
    plt.savefig(os.path.join(dirname, f"power_usage_{period_tag}_{vo}.png"))
    plt.close()
    plt.figure(figsize=(10,5))
    for grp, ddf in data[vo].items():
        d = ddf.reindex(times, method='ffill' if FWD_FILL_STEPS>0 else None, limit=(FWD_FILL_STEPS if FWD_FILL_STEPS>0 else None)).fillna(0)
        plt.plot(times, d['slots'], label=grp, color=group_colors.get(grp))
    plt.ylim(0)
    plt.legend()
    plt.title(f"VO {vo} Core Usage by Series Over Last {WEEKS} Week(s)")
    plt.tight_layout()
    plt.savefig(os.path.join(dirname, f"core_usage_{period_tag}_{vo}.png"))
    plt.close()

    # Save detailed core‐usage time series for this VO
    df_slots = pd.DataFrame({grp: data[vo][grp]['slots'] for grp in data[vo]}, index=times)
    df_slots.to_csv(os.path.join(dirname, f"core_usage_{period_tag}_{vo}.csv"))

# IPMI Power plot
if ipmi:
    plt.figure(figsize=(10,5))
    for grp, series_ip in ipmi.items():
        plt.plot(series_ip.index, series_ip.values, label=grp, color=group_colors.get(grp))
    plt.ylim(0)
    plt.legend()
    plt.title(f"IPMI Power by Node Series ({period_tag})")
    plt.tight_layout()
    plt.savefig(os.path.join(dirname, f"ipmi_power_{period_tag}.png"))
    plt.close()
else:
    print("No IPMI series found; skipping IPMI plot.")

# Generate Estimated Power plot
plt.figure(figsize=(10,5))
for grp, series_est in est.items():
    plt.plot(times, series_est, label=grp, color=group_colors.get(grp))
plt.ylim(0)
plt.legend()
plt.title(f"Estimated Power by Node Series ({period_tag})")
plt.tight_layout()
plt.savefig(os.path.join(dirname, f"estimated_power_{period_tag}.png"))
plt.close()

# Estimated Power per Node Series individually
for grp, series_est in est.items():
    plt.figure(figsize=(10,5))
    plt.plot(times, series_est, label=grp, color=group_colors.get(grp))
    plt.ylim(0)
    plt.legend(loc='upper left', bbox_to_anchor=(1.02, 1), borderaxespad=0)
    plt.title(f"Estimated Power for {grp} ({period_tag})")
    plt.xlabel("Time")
    plt.ylabel("Power (W)")
    plt.tight_layout(rect=[0,0,0.85,1])
    plt.savefig(os.path.join(dirname, f"estimated_power_{grp}_{period_tag}.png"), bbox_inches='tight')
    plt.close()

# Build a DataFrame of x86 power for all VOs
df_x86 = pd.DataFrame(
    {vo: vo_ts[vo]['x86'] for vo in vo_ts},
    index=times
)
plt.figure(figsize=(10,5))
df_x86.plot.area(ax=plt.gca(), stacked=True, cmap='tab20')
plt.gca().legend(loc='upper left', bbox_to_anchor=(1.02, 1), borderaxespad=0)
plt.tight_layout(rect=[0, 0, 0.85, 1])   # leave 15% margin on the right for the legend
plt.ylim(0)
plt.xlabel('Time')
plt.ylabel('x86 Power (W)')
plt.title(f"Stacked x86 Power Usage per VO ({period_tag})")
plt.savefig(os.path.join(dirname, f"stacked_x86_{period_tag}.png"), bbox_inches='tight')
plt.close()

# Build a DataFrame of ARM power for all VOs
df_arm = pd.DataFrame(
    {vo: vo_ts[vo]['ARM'] for vo in vo_ts},
    index=times
)
plt.figure(figsize=(10,5))
df_arm.plot.area(ax=plt.gca(), stacked=True, cmap='tab20')
plt.gca().legend(loc='upper left', bbox_to_anchor=(1.02, 1), borderaxespad=0)
plt.tight_layout(rect=[0, 0, 0.85, 1])
plt.ylim(0)
plt.xlabel('Time')
plt.ylabel('ARM Power (W)')
plt.title(f"Stacked ARM Power Usage per VO ({period_tag})")
plt.savefig(os.path.join(dirname, f"stacked_arm_{period_tag}.png"), bbox_inches='tight')
plt.close()

# Stacked Cumulative x86 Power
x86_groups = [g for g in est if g not in ARM_GROUPS]
df_stack = pd.DataFrame({g: est[g] for g in x86_groups}, index=times)
plt.figure(figsize=(10,5))
df_stack.plot.area(
    ax=plt.gca(),
    stacked=True,
    color=[group_colors.get(g) for g in x86_groups]
)
plt.ylim(0)
plt.xlabel("Time")
plt.ylabel("Power (W)")
plt.title(f"Stacked x86 Power per Node Series (excluding HP_2016) ({period_tag})")
plt.gca().legend(loc='upper left', bbox_to_anchor=(1.02,1), borderaxespad=0)
plt.tight_layout(rect=[0,0,0.85,1])
plt.savefig(os.path.join(dirname, f"stacked_cumulative_x86_{period_tag}.png"), bbox_inches='tight')
plt.close()
