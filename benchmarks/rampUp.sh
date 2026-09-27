#!/bin/bash

## rampUp.sh v0.11 by Emanuele
##
## Metrics collection with increasing CPU usage: 'sleep' , ramp-up , 'stress'
## needs: 'jdump.sh' (running locally by root)
##        'jget.sh'  (started by this script)
## Usage:
## $ nohup ./rampUp.sh [RUNTIME] [WAIT] &

OUTFILE="rampUp.csv"
COLLECTOR="jget.sh"
EXEDIR=$(pwd)
chmod a+rw "${EXEDIR}"

CORES=$(nproc)
MAXMEM=$(( 2*${CORES} ))
RUNTIME="600"
if [ $# -ge 1 ]; then
  RUNTIME=$1
fi
WAIT="0"    
if [ $# -ge 2 ]; then
  WAIT=$2
fi

STRESSECUTABLE="stress"
SLEEPSECUTABLE="sleep"
FLAGS="-c ${CORES} -i 1 -m 1 --vm-bytes ${MAXMEM}G" 

## output the versions in use & other stuff
echo "* ---"
echo "* User: $(whoami)"
echo "* Architecture: $(uname -i) "
echo "* System: $(uname -a)"
echo "* Number of Threads: ${CORES} , Max Memory: ${MAXMEM} Gb "
echo "* Frequency: $(cpupower frequency-info | grep 'Hz (ass')"
echo "* Governor: $(cpupower frequency-info | grep 'The governor')"
echo "* Date-Time: $(date)"
echo "* Testing for:  ${RUNTIME} sec , Waiting: ${WAIT} sec"
echo "* ---"

## check & start the collector 
if [ ! -f "$COLLECTOR" ]; then
  echo "No collector found! Please place $COLLECTOR in this folder. Till then, I quit!"
  exit 1
fi
echo "* Starting collector"
./${COLLECTOR} ${OUTFILE} &

## wait before start
echo "* Waiting half minute to let the collector wake up ..."
sleep 30
echo "* ---"

## Sleep ##
echo "* (1) Waiting ${WAIT} sec. to start"
sleep ${WAIT}
echo "* (1) Starting the nap:  ${SLEEPSECUTABLE} ${RUNTIME}"
${SLEEPSECUTABLE} ${RUNTIME}
echo "* (1) Done: $SLEEPSECUTABLE !"

## Ramp-Up ##
echo "* (2) Waiting ${WAIT} sec. to start"
sleep ${WAIT}
echo "* (2) Starting ramp-up:"
for ((i=1; i<=CORES; i++)); do
  stress -c 1 --vm-bytes 2G &   # Launch one new stress worker per iteration (1 core , 2G RAM)
  PID=($!)
  echo "* * Ramp-up step ${i}/${CORES}: stressing ${i} core(s) with PID = $PID"
  sleep $(( RUNTIME / CORES ))  # Pause before adding the next core
done
# Wait for the full ramp-up time to elapse to ensures the ramp-up lasts RUNTIME in total
sleep $(( RUNTIME - (RUNTIME / CORES) * CORES ))  
pkill stress                    # Kill all the stress processes at the end of the ramp-up phase
echo "* (2) Done: ramp-up !"

## Stress ##
echo "* (3) Waiting ${WAIT} sec. to start"
sleep ${WAIT}
echo "* (3) Starting the job:  ${STRESSECUTABLE} ${FLAGS} -t ${RUNTIME}s"
${STRESSECUTABLE} ${FLAGS} -t ${RUNTIME}s
echo "* (3) Done: $STRESSECUTABLE !"

echo "* Killing the collector"
pkill ${COLLECTOR}

echo "* Done! ***"
echo "* ---"
