#!/bin/bash

## node_get_static.sh ## v0.4 by Emanuele ##

EXPORTER_DIR="/var/lib/node_exporter/textfile_collector"
PROM_FILE="statics.prom"

## Remove old .prom file
#rm -rf $EXPORTER_DIR/$PROM_FILE

# Number of Cores
NCORES=`lscpu | grep '^CPU(s): ' | awk '{ print $2 }'`
case ${NCORES} in
    ''|*[!0-9]*) echo "node_static_cores NaN"  > $EXPORTER_DIR/$PROM_FILE ;; 
    *) echo "node_static_cores ${NCORES}"      > $EXPORTER_DIR/$PROM_FILE ;; 
esac

# Average Frequency
AFREQ=`lscpu | grep 'CPU max MHz:' | awk '{ print $4 }'`
case ${AFREQ} in
   ''|*[!0-9.]*|'.'|*.*.*) echo "node_static_frequency NaN" >> $EXPORTER_DIR/$PROM_FILE ;;
   *|*.*) echo "node_static_frequency ${AFREQ}"             >> $EXPORTER_DIR/$PROM_FILE ;; 
esac

# Total Memory (bytes)
RAMBYT=`vmstat -s -Sk | grep 'total memory' | awk '{ print $1 }'` 
case ${RAMBYT} in
    ''|*[!0-9]*) echo "node_static_ram NaN"     >> $EXPORTER_DIR/$PROM_FILE ;; 
    *) echo "node_static_ram ${RAMBYT}"         >> $EXPORTER_DIR/$PROM_FILE ;; 
esac

# Hard Disks Size (bytes)
DEVS=$(lsblk -o name,type | grep disk | cut -d' ' -f1)
for i in ${DEVS}
do
    SIZE=$(lsblk -o name,type,size -b /dev/${i} | grep disk | awk '{ print $3 }') 
    echo "node_static_disk{dev=\"/dev/${i}\"}  ${SIZE}" >> $EXPORTER_DIR/$PROM_FILE 
    unset SIZE
done
