#!/bin/bash

## node_get_ipmi.sh ## v1.3 by Emanuele ##

EXPORTER_DIR="/var/lib/node_exporter/textfile_collector"
PROM_FILE="ipminfo.prom"

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

# Get IPMI info (power consumption) - available on new DELLs (>=2020) and HPs (>=2017)
# Power Consumption is the first metric in the output, e.g. 'Instantaneous power reading: 138 Watts'
IPMIQUERY=$(ipmitool dcmi power reading)
arr=($IPMIQUERY)
POWER=$(echo ${arr[3]})
#echo "node_sensor_power ${POWER}" 

# If I get a number, write it to file. Else, write 'NaN'
case ${POWER} in
   ''|*[!0-9.]*|'.'|*.*.*) echo "node_sensor_power NaN" >> $EXPORTER_DIR/$PROM_FILE ;;
   *|*.*) echo "node_sensor_power ${POWER}" >> $EXPORTER_DIR/$PROM_FILE ;; 
esac
