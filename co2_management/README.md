## CO2-aware Frequency Management

### How it works
co2_monitor.py fetches 24h carbon intensity forecasts from the NESO API
for a configurable UK grid region, exports them as Prometheus metrics,
and triggers Grafana alerts when intensity exceeds a threshold.
The operator then runs co2_freq_manager.yml manually to throttle or
reset CPU frequency across the cluster.
plot_intensity.py generates standalone plots of forecast vs. actual intensity.
The co2_monitor.service file is an Ansible-deployable systemd unit.

### Files
| File | Purpose |
|---|---|
| co2_monitor.py | NESO API fetcher + Prometheus exporter (runs as service) |
| co2_monitor.service | systemd unit template for deployment via Ansible |
| co2_freq_manager.yml | Ansible playbook: throttle or reset cluster CPU frequency |
| plot_intensity.py | standalone plot of carbon intensity data |

### Why manual operation
WLCG pilot jobs cannot be interrupted mid-run. All frequency changes
are therefore operator-initiated following a Grafana alert, not automated.
