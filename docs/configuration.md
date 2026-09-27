## Exporters
| Parameter | File | Default | Description |
|---|---|---|---|
| EXPORTER_DIR | all exporters | /var/lib/node_exporter/textfile_collector | textfile collector path |
| VO list | node_get_psinfo.sh, node_get_condorinfo.sh | ALICE,ATLAS,CMS,LHCb | add your site VOs |

## Analysis
| Parameter | File | Default | Description |
|---|---|---|---|
| PROMETHEUS_URL | power_plus.py | http:/your-prometheus-host:9090 | your Prometheus endpoint |
| static_data | power_plus.py | see file | hardware groups, thread counts, max power per group |

## CO2 Management
| Parameter | File | Default | Description |
|---|---|---|---|
| REGION | co2_monitor.py | South Scotland | NESO region for your site |
| THRESHOLD | co2_freq_manager.yml | 150 gCO2/kWh | carbon intensity trigger |
| frequency targets | co2_freq_manager.yml | per group | mid-freq values from your benchmark |
