#!/bin/bash

## Run any benchmark at various frequency steps
## v0.01 by Emanuele
##
## Pre-requisites:
##  make sure the IPMI dumper script 'jdump.sh' is running and updating '/tmp/ipminfo.json' (every N seconds),
##  make sure the IPMI importer script 'jget.sh' and the job script (if any) are in the same folder as this file.
##
## Usage:
##
##  $ nohup ./runAny_frqscan.sh &
##

# General
OUTFILENAME="ipmidump.csv"
DUMPFILE="/tmp/ipminfo.json"
COLLECTOR="jget.sh"
RUNNER="sleep"  # "./getSleep.sh"
PREFLAG=""      # if it is a custom script, add "./"
POSTFLAG="600"  # parameter to pass to the job, e.g. " 600"
WAIT="30"       # pause at least 30 seconds before & after every frequency step
BASEDIR=$(pwd)

# System settings
ARCH=$(uname -i)
SINVER=$(singularity --version)
PYTVER=$(python3 --version)
CORES=$(nproc)

# Check if the collector script exists
if [ ! -f "$COLLECTOR" ]; then
  echo "No collector found! Please place $COLLECTOR in this folder. Till then, I quit!"
  exit 1
fi

# Loop over the array of frequencies:
echo "* Starting loop: "
echo ""
for frq in {1000,2000,2100,2200,2300,2400,2500,2600,2700,2800,2900,3000}  # check CPUs allowed steps !
do
    # Set CPU frequency
    sudo cpupower frequency-set -f ${frq}MHz > /dev/null 2>&1
    ACTUALFRQ=$(cpupower frequency-info | grep 'Hz (ass')

    # Prepare execution folder  
    EXEDIR="${BASEDIR}/${frq}MHz"
    OUTFILE="${EXEDIR}/${OUTFILENAME}"
    mkdir "$EXEDIR"
    if [ ! -z "$PREFLAG" ]; then
      cp "$BASEDIR/$RUNNER" "$EXEDIR/."
    fi
    cp "$BASEDIR/$COLLECTOR" "$EXEDIR/."
    cd "$EXEDIR"

    # Start the job:
    JOBNAME="${RUNNER}_${POSTFLAG}_${ARCH}_${CORES}c_${frq}MHz"
    EXEJOB="${PREFLAG}${RUNNER} ${POSTFLAG}"
    echo "** executing job: $JOBNAME in $EXEDIR ..."
    echo "** $ACTUALFRQ"
    echo "** cd $EXEDIR ; nohup ${EXEJOB}"
    printf "*** executing job: $JOBNAME in $EXEDIR ... \n" > $EXEDIR/thisjob.out 2>&1
    printf "*** $ACTUALFRQ \n" >> $EXEDIR/thisjob.out 2>&1
    ./${COLLECTOR} ${OUTFILE} &
    sleep $WAIT
    nohup $EXEJOB >> $EXEDIR/thisjob.out 2>&1
    printf "*** done. \n" >> $EXEDIR/thisjob.out 2>&1
    sleep $WAIT
    pkill ${COLLECTOR}
    echo "** Done with the $frq MHz job:  $JOBNAME"
    echo ""
done
# Set governor back to conservative
sudo cpupower frequency-set -g conservative > /dev/null 2>&1
echo "* Done all."
echo ""
