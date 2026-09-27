#!/bin/bash

## node_get_smart.sh ## v0.1 by Emanuele ##

PATH=$PATH:/usr/local/bin:/usr/sbin
EXPORTER_DIR="/var/lib/node_exporter/textfile_collector"
PROM_FILE=smart.prom

# Remove old .prom file
rm -rf $EXPORTER_DIR/$PROM_FILE

# Get list of HDs
DEVS=$(lsblk -o NAME,type | grep disk | cut -d' ' -f1)

# Loop over HDs & get S.M.A.R.T. info
for i in ${DEVS}; do
  SMARTY=$(smartctl -a /dev/${i})

  # Parse values
  HEALTH=$(echo "${SMARTY}" | grep 'SMART overall-health self-assessment' | cut -d':' -f2-)  # Health (1 or 0)
  if [[ $HEALTH =~ "PASSED" ]]; then HBIT=1  
  else HBIT=0
  fi
  SPINUP=$(echo "${SMARTY}" | grep 'Spin_Up_Time' |  awk '{ print $10 }')        # SpinUp (?)
  POWHOU=$(echo "${SMARTY}" | grep 'Power_On_Hours' |  awk '{ print $10 }')      # PowHou (?)
  HDTEMP=$(echo "${SMARTY}" | grep 'Temperature_Celsius' |  awk '{ print $10 }') # HDtemp (again)

  # Write to .prom file (if metric not empty)
  echo "smart_overall_health{dev=\"/dev/$i\"} ${HBIT}"                        >> $EXPORTER_DIR/$PROM_FILE
  if [ $SPINUP ]; then echo "smart_spin_up{dev=\"/dev/$i\"} ${SPINUP}"        >> $EXPORTER_DIR/$PROM_FILE ; fi
  if [ $POWHOU ]; then echo "smart_power_on_hours{dev=\"/dev/$i\"} ${POWHOU}" >> $EXPORTER_DIR/$PROM_FILE ; fi
  if [ $HDTEMP ]; then echo "smart_hd_temp{dev=\"/dev/$i\"} ${HDTEMP}"        >> $EXPORTER_DIR/$PROM_FILE ; fi
 
  unset HEALTH HBIT SPINUP POWHOU HDTEMP
done
