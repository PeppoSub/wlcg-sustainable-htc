#!/bin/bash

## node_get_psinfo.sh ## v1.4 by Emanuele ##

EXPORTER_DIR="/var/lib/node_exporter/textfile_collector"
PROM_FILE=psinfo.prom
LOCK="/tmp/node_get_psinfo.lock"

if ( set -o noclobber; echo "$$" > "$LOCK" ) 2> /dev/null
then
    trap "rm -f $LOCK; exit" INT TERM EXIT

    # Get process info
    QUERY=$(ps r -C condor -eo pid,user,cmd,label | grep condor_startd)
    CONDORJOBS=$(echo "${QUERY}" | wc -l)                                # running HTCondor jobs (total)

    ## Parse VOs from pool users id (...probably not the best way): 
    ALICEJOBS=$(echo "${QUERY}" | grep -i alic | wc -l)
    ATLASJOBS=$(echo "${QUERY}" | grep -i atla | wc -l)
    CMSJOBS=$(echo ${QUERY} | grep -i cms | wc -l) 
    LHCBJOBS=$(echo ${QUERY} | grep -i lhc | wc -l) 
    # ... add the VOs supported at your site!

    # Export all info to $PROM_FILE (only if values are filled)
    echo "node_ps_running{vo=\"ALICE\"} ${ALICEJOBS}"        > $EXPORTER_DIR/$PROM_FILE   #first line 
    echo "node_ps_running{vo=\"ATLAS\"} ${ATLASJOBS}"        >> $EXPORTER_DIR/$PROM_FILE
    echo "node_ps_running{vo=\"CMS\"} ${CMSJOBS}"            >> $EXPORTER_DIR/$PROM_FILE
    echo "node_ps_running{vo=\"LHCb\"} ${LHCBJOBS}"          >> $EXPORTER_DIR/$PROM_FILE
    # ... add the VOs supported at your site!

    rm -f $LOCK
    trap - INT TERM EXIT
else
    exit 1
fi
