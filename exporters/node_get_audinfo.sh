#!/bin/bash

## node_get_audinfo.sh ## v0.21 by Emanuele ##

EXPORTER_DIR="${EXPORTER_DIR:-/tmp/audinfo_test}"     # node_exporter folder, usually: '/var/lib/node_exporter/textfile_collector'
mkdir -p "$EXPORTER_DIR"
PROM_FILE="audinfo.prom"                              # anything '.prom' will be exported

# Get IPMI info (temperatures)
IPMIQUERY=$(ipmitool sdr)

# Ambient Temperature can be either: 'Ambient' , 'Sys Temp' , 'System Temp' , 'Inlet' , ...
AMBIENT=$(echo "${IPMIQUERY}" | grep 'Ambient' | head -1)
if [ -z "$AMBIENT" ]; then
   AMBIENT=$(echo "${IPMIQUERY}" | grep 'Sys Temp' | head -1)
fi
if [ -z "$AMBIENT" ]; then
   AMBIENT=$(echo "${IPMIQUERY}" | grep 'System Temp' | head -1)
fi
if [ -z "$AMBIENT" ] ; then
   AMBIENT=$(echo "${IPMIQUERY}" | grep 'Inlet' | head -1)
fi
if [ -z "$AMBIENT" ] ; then
   AMBIENT=$(echo "${IPMIQUERY}" | grep 'FRONT_PANEL_TEMP' | head -1)
fi
if [ -z "$AMBIENT" ] ; then
   AMBIENT=$(echo "${IPMIQUERY}" | grep 'INLET_AIR_TEMP' | head -1)
fi
if [ -z "$AMBIENT" ] ; then
   AMBIENT='novalue | NaN'
fi
AMBT=$(echo "${AMBIENT}" | cut -d'|' -f2  | awk '{ print $1 }')
#echo "Ambient Temperature = ${AMBT}"

# If I get a number, write it to file. Else, write 'NaN'
case ${AMBT} in
   ''|*[!0-9.]*|'.'|*.*.*) echo "node_sensors_ambient NaN" > $EXPORTER_DIR/$PROM_FILE ;;
   *|*.*) echo "node_sensors_ambient ${AMBT}" > $EXPORTER_DIR/$PROM_FILE ;; 
esac

# Get IPMI info: Power Consumption is the first metric in the output, e.g. 'Instantaneous power reading: 138 Watts'
IPMIQUERY=$(ipmitool dcmi power reading)
arr=($IPMIQUERY) 
POWER=$(echo ${arr[3]})
#echo "node_sensor_power ${POWER}" 

# If POWER is a number, write it to file. Else, write 'NaN'
case ${POWER} in
   ''|*[!0-9.]*|'.'|*.*.*) echo "node_sensor_power NaN" >> $EXPORTER_DIR/$PROM_FILE ;;
   *|*.*) echo "node_sensor_power ${POWER}" >> $EXPORTER_DIR/$PROM_FILE ;; 
esac

# Active thread = logical CPU with idle% below IDLE_THRESHOLD over a short sampling window
IDLE_THRESHOLD=90    # lower = stricter definition of "active"
SAMPLE_SLEEP=1       # seconds between /proc/stat samples
read_percpu_stat() {
   awk '/^cpu[0-9]+/ { idle=$5; total=0; for(i=2;i<=NF;i++) total+=$i; print $1, idle, total }' /proc/stat
}
SAMPLE1=$(read_percpu_stat)
sleep ${SAMPLE_SLEEP}
SAMPLE2=$(read_percpu_stat)

ACTIVE_CORES=$(awk -v thresh="${IDLE_THRESHOLD}" '
   NR==FNR { idle1[$1]=$2; total1[$1]=$3; next }
   {
      didle  = $2 - idle1[$1]
      dtotal = $3 - total1[$1]
      if (dtotal > 0) {
         idle_pct = (didle/dtotal)*100
         if (idle_pct < thresh) active++
      }
   }
   END { print active+0 }
' <(echo "${SAMPLE1}") <(echo "${SAMPLE2}")
)
#echo "node_active_threads ${ACTIVE_CORES}" 

# Compute power per active thread, guarding against NaN power / zero active cores
case ${POWER} in
   ''|*[!0-9.]*|'.'|*.*.*)
      PWR_PER_THREAD='NaN' ;;
   *|*.*)
      if [ "${ACTIVE_CORES}" -gt 0 ] 2>/dev/null; then
         PWR_PER_THREAD=$(awk -v p="${POWER}" -v c="${ACTIVE_CORES}" 'BEGIN { printf "%.3f", p/c }')
      else
         PWR_PER_THREAD='NaN'
      fi
      ;;
esac
#echo "node_power_per_active_thread_watts ${PWR_PER_THREAD}" 

# Two separate metrics, one can be commented out as redundant
echo "node_active_threads ${ACTIVE_CORES}" >> $EXPORTER_DIR/$PROM_FILE
echo "node_power_per_active_thread_watts ${PWR_PER_THREAD}" >> $EXPORTER_DIR/$PROM_FILE

exit 0
