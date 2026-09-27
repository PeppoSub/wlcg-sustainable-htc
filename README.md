# wlcg-sustainable-htc

# WLCG Green HPC: Monitoring, Power Accounting, and CO2-aware Scheduling

A collection of tools developed at ScotGrid Glasgow (a WLCG Tier-2 site)
to monitor computing infrastructure, track power usage by Virtual
Organisation (VO), and reduce carbon emissions through CO2-aware CPU
frequency management.

These tools were developed incrementally as part of a broader effort
to bring sustainability metrics into scientific High-Throughput Computing
(HTC) infrastructure. They are shared here as a reference implementation
for other HEP and research computing sites.

---

## Overview

Scientific computing sites in the WLCG grid run thousands of CPU cores
continuously for particle physics workloads. Power consumption is
significant and largely invisible: batch jobs are tracked by CPU hours
but not by energy consumed. Carbon intensity of the electricity grid
varies by hour and by day, creating opportunities to reduce emissions
with minimal impact on throughput.

This project addresses three problems:

1. **Visibility**: who is consuming power, when, and how much?
2. **Accountability**: how do we attribute energy use to VOs and experiments?
3. **Reduction**: can we reduce carbon emissions by adapting to grid conditions?

---

## Architecture

Worker nodes
|-- node_exporter (standard metrics)
|-- node_get_ipmi.sh -> ipminfo.prom (IPMI power + temperature)
|-- node_get_psinfo.sh -> psinfo.prom (running jobs by VO, from kernel)
|-- node_get_condorinfo.sh -> condoren.prom (HTCondor slot metrics by VO)
|-- node_get_audinfo.sh -> audinfo.prom (AUDITOR/PEAR power accounting)
|
v
Prometheus / VictoriaMetrics (central collector)
|
+--> Grafana dashboards (real-time display)
|
+--> power_plus.py (offline analysis: energy per VO, validation)
specyplot.py (HS23 throughput efficiency analysis)

Carbon Intensity API (api.carbonintensity.org.uk)
|
+--> co2_exporter.py -> Prometheus (CO2 forecast metrics)
|
+--> Grafana alerting
|
+--> co2_freq_manager.yml (Ansible: throttle/reset cluster frequency)


---

## Components

### 1. Exporters (`exporters/`)

Custom scripts that run as cron jobs on every worker node, writing
metrics in Prometheus textfile format to the node_exporter collector
directory (`/var/lib/node_exporter/textfile_collector/`).

| Script | Output | Description |
|---|---|---|
| `node_get_ipmi.sh` | `ipminfo.prom` | IPMI power (W) and ambient temperature |
| `node_get_psinfo.sh` | `psinfo.prom` | Running jobs per VO (from kernel ps) |
| `node_get_condorinfo.sh` | `condoren.prom` | HTCondor slot metrics per VO and CE |
| `node_get_audinfo.sh` | `audinfo.prom` | AUDITOR/PEAR power accounting metrics |

All scripts are designed for RHEL-family systems with HTCondor and
ipmitool installed. Cron configuration:

```cron
*/1 * * * * /var/lib/node_exporter/node_get_ipmi.sh
*/1 * * * * /var/lib/node_exporter/node_get_psinfo.sh
*/1 * * * * /var/lib/node_exporter/node_get_condorinfo.sh
*/1 * * * * /var/lib/node_exporter/node_get_audinfo.sh
```

**Adapting to your site:** the VO list in `node_get_psinfo.sh` and
`node_get_condorinfo.sh` must be updated to match your supported VOs.
Hardware group names in `power_plus.py` must match your Prometheus labels.

### 2. Analysis (`analysis/`)

Offline Python scripts for energy accounting and efficiency analysis.

**`power_plus.py`**: queries Prometheus or VictoriaMetrics via the API,
aggregates core usage by VO and hardware series, estimates energy per VO
by multiplying slot usage by measured per-core power, and validates
against direct IPMI measurements. Outputs a text report and PNG plots.

```bash
python power_plus.py          # default: last 4 weeks
python power_plus.py 8        # last 8 weeks
```

**`specyplot.py`** and **`claude_history.py`**: analysis of HS23
benchmark throughput across hardware generations and CPU frequency
operating points, used to quantify the efficiency trade-off of frequency
throttling. These scripts produced the benchmark data underpinning the
Watt Matters results (see Publications below).

### 3. CO2 Management (`co2-management/`)

Tools for tracking electricity grid carbon intensity and adjusting
cluster CPU frequency accordingly.

**`co2_exporter.py`**: fetches 24-hour carbon intensity forecasts from
the National Energy System Operator (NESO) API for the relevant grid
region, exports them as Prometheus metrics, and handles the
forward-timestamped nature of forecast data (using the Grafana Infinity
plugin for storage).

**`co2_freq_manager.yml`**: Ansible playbook that implements two modes:
- `throttle`: sets CPUs to a mid-frequency operating point across
  targeted hardware groups when carbon intensity exceeds threshold
- `reset`: restores default (maximum) frequency

All runs are intentional and manual: WLCG pilot jobs cannot be
interrupted, so automated scheduling is out of scope. The playbook is
invoked by a human operator following Grafana alerts.

**Key finding (Watt Matters, CHEP 2026):** at the mid-frequency
operating point, power consumption drops by approximately X% with a
throughput (HS23) reduction of approximately Y%. During high-carbon
periods, the CO2 reduction per kWh saved outweighs the throughput cost.
Scotland's grid was consistently below threshold during the study period,
so results are presented as counterfactual estimates validated against
two measured baselines. [fill in X and Y]

### 4. Dashboards (`dashboards/`)

Grafana dashboard JSON exports for direct import:

| File | Description |
|---|---|
| `cluster_overview.json` | Site-wide cluster throughput and power efficiency |
| `htc_jobs.json` | Per-VO and per-instance HTCondor job metrics |
| `node_monitor.json` | Single-node detail view |

Built on Prometheus/VictoriaMetrics. The Grafana Infinity plugin is
required for CO2 forecast visualisation.

---

## Prerequisites

- RHEL-family OS (RHEL 8/9, AlmaLinux, Rocky Linux)
- node_exporter >= 1.5
- HTCondor (for condorinfo script)
- ipmitool (for IPMI script)
- Prometheus >= 2.x or VictoriaMetrics >= 1.x
- Grafana >= 10.x
- Python 3.9+ with: requests, pandas, numpy, matplotlib
- Ansible >= 2.12 (for co2_freq_manager.yml)

---

## Configuration

See `docs/configuration.md` for full configuration reference.
Key site-specific parameters to set before deployment:

- VO list in exporter scripts
- Hardware group names and per-core power figures in `power_plus.py`
- Prometheus/VictoriaMetrics endpoint URLs
- NESO API region identifier in `co2_exporter.py`
- CPU frequency targets per hardware group in `co2_freq_manager.yml`

---

## Publications and Presentations

This work has been presented at:

---

### 2026

**Watt Matters: cluster-level frequency control for lower CO2 in HEP** *(main author)*
CHEP 2026, in Bangkok (Thailand)
[Slides](https://indico.cern.ch/event/1471803/contributions/6966477/) | Proceedings: submitted

---

### 2025

**The environmental impact, carbon emissions and sustainability of computing in the ATLAS experiment** *(co-author)*
The European Physical Journal C, October 2025
[Paper](https://doi.org/10.1140/epjc/s10052-025-14976-3)

**Power Accounting in Heterogeneous Compute Clusters** *(main author)*
WLCG/HSF Workshop, IJCLab, May 2025 in Paris (France)
[Slides](https://indico.cern.ch/event/1484669/contributions/6480491/)

---

### 2024

**Taking on RISC for Energy-Efficient Computing in HEP** *(main author)*
CHEP 2024, in Krakow (Poland)
[Slides](https://indico.cern.ch/event/1338689/contributions/6011565/) | [Paper](https://doi.org/10.1051/epjconf/202533701163)

**Heterogeneous Computing and Power Efficiency in HEP** *(main author)*
CHEP 2024, in Krakow (Poland)
[Slides](https://indico.cern.ch/event/1338689/contributions/6011562/) | [Paper](https://doi.org/10.1051/epjconf/202533701162)

**Simulating the Carbon Cost of Grid Sites** *(co-author)*
CHEP 2024, in Krakow (Poland)
[Slides](https://indico.cern.ch/event/1338689/contributions/6011579/)

**Carbon Compromises: Minimising the Carbon-Cost and Power-Use of Grid Sites** *(co-author)*
CHEP 2024, in Krakow (Poland)
[Paper](https://doi.org/10.1051/epjconf/202533701314)

**HEP Benchmark Suite: Enhancing Efficiency and Sustainability in Worldwide LHC Computing Infrastructures** *(co-author)*
ACAT 2024, in New York (USA)
[Paper](https://doi.org/10.48550/arXiv.2408.12445)

**ARM Compute Testing and Provision at Glasgow's Tier-2** *(co-author)*
HEPiX Spring Workshop, April 2024 in Paris (France):
[Slides](https://indico.cern.ch/event/1377701/contributions/5902147/)

---

### 2023

**ARMing HEP for the future: Energy Efficiency of WLCG sites (ARM vs. x86)** *(main author)*
CHEP 2023, in Norfolk (USA)
[Poster](https://indico.jlab.org/event/459/contributions/11660/) | [Paper](https://doi.org/10.1051/epjconf/202429511007)

**Glasgow's ARM cluster: Experience and Findings** *(co-author)*
Journees LCG-France, CC-IN2P3, in Villeurbanne (France)
[Slides](https://indico.in2p3.fr/event/30831/contributions/130216/)

---

### 2022

**Power Efficiency in HEP (x86 vs. ARM)** *(main author)*
ACAT 2022, Bari (Italy)
[Slides](https://indico.cern.ch/event/1106990/contributions/4991256/) | [Paper](https://doi.org/10.1088/1742-6596/3206/1/012038)

---

### 2021

**A hybrid system for monitoring and automated recovery at the Glasgow Tier-2 cluster** *(main author)*
vCHEP 2021 (online)
[Slides](https://indico.cern.ch/event/948465/contributions/4323666/) | [Paper](https://doi.org/10.1051/epjconf/202125102047)

---

Related community page:
https://gridpp.atlassian.net/wiki/spaces/public/pages/250281986/GridPP+Sustainability+Page

---

## Other Publications and Presentations

A full list is available on [InspireHEP](https://inspirehep.net/authors/1068730).

---

## Related Projects

- [EnSuRe](https://cordis.europa.eu/project/id/101292847/it):
  ENvironmentally SUstainable digital services and practices for REsearch infrastructures 
  (EU grant)

- [AUDITOR](https://github.com/ALU-Schumacher/AUDITOR): WLCG job
  accounting extended with hardware and power metrics

---

## Authors

Emanuele Simili (ScotGrid Glasgow / University of Glasgow)

Scripts co-developed with Claude (Anthropic) where indicated in file headers.

---

## Licence

MIT Licence. See LICENSE file.

---
