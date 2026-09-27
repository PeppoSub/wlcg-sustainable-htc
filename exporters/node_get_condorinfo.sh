#!/bin/bash

## node_get_condorinfo.sh ## v2.2 by Emanuele & Gordon ##

TMP_FILE=$(mktemp)
EXPORTER_DIR="/var/lib/node_exporter/textfile_collector"
PROM_FILE="condoren.prom"
DEBUG_FILE="debugfile.txt"

PATH=$PATH:/usr/local/bin
THISHOST=$(hostname -s)

# Check for numbers 
re='^[0-9]+([.][0-9]+)?$'

# Check if HTCondor is runing and accepts jobs
CONDOREN=$(condor_config_val -startd StartJobs)
if [[ $? -eq 0 ]]; then
    if [[ "$CONDOREN" == "True" ]]; then
        TVAL=1
    else
        TVAL=0
    fi
else
    # If condor_config_val exited with non-zero code, assume Condor is not running and exit script
    echo "node_condor_enabled 0"  > $TMP_FILE   # first line (always has data)
    exit 0
fi

# Get system info from local node
HTCQUERY=$(condor_status -slot -direct $(hostname -s) -autoformat:t Name CPUs Memory LoadAvg State Activity TotalJobRunTime RemoteUser ClientMachine TotalCPUs TotalMemory)

# Get node info (redundant: already in 'node_static_')
TOTCPU=$(echo "${HTCQUERY}" | grep "slot1@" | awk '{ print $10 }' | cut -d'.' -f1)   # Total CPU (integer)
TOTRAM=$(echo "${HTCQUERY}" | grep "slot1@" | awk '{ print $11 }')                   # Total Mem (MB)

# Write the first metrics to file
echo "node_condor_enabled ${TVAL}"  > $TMP_FILE   # first line (always has data)
if [[ $TOTCPU ]]; then echo "node_condor_cpu_total ${TOTCPU}"  >> $TMP_FILE ; fi;
if [[ $TOTRAM ]]; then echo "node_condor_ram_total ${TOTRAM}"  >> $TMP_FILE ; fi;
echo ""                             >> $TMP_FILE  # line break 

promfile_fill () {
  SLOTNUMBER=$1
  SLOTQUERY="$2"

  # Get slots metrics (number or NaN)
  USECPU=$(echo "${SLOTQUERY}" | awk '{ print $2 }')
  if ! [[ $USECPU =~ $re ]] ; then USECPU="NaN" ; fi
  USEMEM=$(echo "${SLOTQUERY}" | awk '{ print $3 }')
  if ! [[ $USEMEM =~ $re ]] ; then USEMEM="NaN" ; fi
  LOADAV=$(echo "${SLOTQUERY}" | awk '{ print $4 }')
  if ! [[ $LOADAV =~ $re ]] ; then LOADAV="NaN" ; fi
  RUNTIME=$(echo "${SLOTQUERY}" | awk '{ print $7 }')      # TotalJobRunTime[sec]
  if ! [[ $RUNTIME =~ $re ]] ; then RUNTIME="NaN" ; fi

  # Get slot state (string)
  STATE=$(echo "${SLOTQUERY}" | awk '{ print $5 }')
  ACTITY=$(echo "${SLOTQUERY}" | awk '{ print $6 }')
  # If slot is "Unclaimed", then skip
  if [[ "${STATE}" == "Unclaimed" ]]; then return ; fi;
 
  # Get what CE is submitting ...
  CE=$(echo "${SLOTQUERY}" | awk '{ print $9 }'  | cut -d'.' -f1)

  # Get which VO is using the slot ... 
  VOUSER=$(echo "${SLOTQUERY}" | awk '{ print $8 }' | cut -d'@' -f1)
  #echo "*** VOUSER: $VOUSER ***"
  if [[ "${VOUSER}" == *"alice"* ]]; then VO="ALICE"
  elif [[ "${VOUSER}" == *"atla"* ]]; then VO="ATLAS"
  elif [[ "${VOUSER}" == *"cms"* ]]; then VO="CMS"
  elif [[ "${VOUSER}" == *"lhc"* ]]; then VO="LHCb"
  # ... add the VOs supported at your site!
  else VO="undefined"; fi
  
  # Catch undefined VOs
  if [[ "${VO}" == "undefined" ]] 
    then echo "VO marked as ${VO} is:  ${VOUSER}" >> $EXPORTER_DIR/$DEBUG_FILE ;
  fi
  # Write metrics to prom file
  echo "node_condor_cpu{slot=\"$SLOTNUMBER\",vo=\"$VO\",ce=\"$CE\"} ${USECPU}"      >> $TMP_FILE ;
  echo "node_condor_ram{slot=\"$SLOTNUMBER\",vo=\"$VO\",ce=\"$CE\"} ${USEMEM}"      >> $TMP_FILE ;
  echo "node_condor_load{slot=\"$SLOTNUMBER\",vo=\"$VO\",ce=\"$CE\"} ${LOADAV}"     >> $TMP_FILE ;
  echo "node_condor_runtime{slot=\"$SLOTNUMBER\",vo=\"$VO\",ce=\"$CE\"} ${RUNTIME}" >> $TMP_FILE ;
  # Unset variables
  unset SLOTQUERY SLOTNUMBER USECPU USEMEM LOADAV RUNTIME FROMCE VOUSER VO CE
}

# Get job info for unpartitioned slot (if any)
SLOT_QUERY=$(echo "${HTCQUERY}" | grep "slot1@")
SLOT_NUMBER=0
promfile_fill ${SLOT_NUMBER} "${SLOT_QUERY}"

# Get job info for every slot partitions
for i in $(seq $TOTCPU)
do
  SLOT_QUERY=$(echo "${HTCQUERY}" | grep "slot1_$i@")
  SLOT_NUMBER=$i
  # If slot $i does not exist: continue to next
  if [[ -z "${SLOT_QUERY}" ]]; then continue; fi  
  promfile_fill ${SLOT_NUMBER} "${SLOT_QUERY}"
done

# Create or replace existing .prom file
chmod a+r $TMP_FILE
mv $TMP_FILE $EXPORTER_DIR/$PROM_FILE
