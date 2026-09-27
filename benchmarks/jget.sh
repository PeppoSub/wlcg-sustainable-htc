#!/bin/bash

## jget.sh v1.1 by Emanuele
##
## Grabs the IPMI info (and other metrics) from a JSON dump file and appends them to a CSV file, indefinitely.
## It works with the new JSON output from 'jdump.sh' (hardcoded as JFILE).
## Command line options are:
## - the output file name (if we want a different name than default), 
## - the sampling interval (if we want it different than the system exporter).

## Normally, this is run as a sub-process by the script running the actual benchmark. 
## Usage: 
## Dump runtime metric to default CSV file:
##  $ ./jget.sh &
## Dump runtime metric to custom CSV file every 30 seconds:
##  $ ./jget.sh customfile.csv 30 &
##

JFILE="/tmp/ipminfo.json"
OUTFILE="ipminfo.csv"
SAMPLING="-1"   # Set a sampling interval ONLY if you want it to be different (>) than the exporter interval

# Regular expression to test for integers
re='^[0-9]+$'

json_to_csv() 
{
  local json_file="$1"
  local csv_file="$2"

  if [ ! -s "$csv_file" ]; then
    # Extract keys using grep and format them as CSV line & Remove ":" and the trailing comma & Write the values to CSV file
    keys=$(grep -oP '"[^"]+":' "$json_file" | tr -d '":' | tr '\n' ',')
    keys=${keys%,}
    echo "$keys" > "$csv_file"
  fi

  # Extract values using grep and format them as CSV line & Remove the trailing comma & Write the values to CSV file
  values=$(grep -oP '"[^"]+":\s*"\K[^"]+' "$json_file" | tr '\n' ',')
  values=${values%,}
  echo "$values" >> "$csv_file"
}

# Check if dump file exists
if [ ! -f "$JFILE" ]; then
  echo "No IPMI dump file was found: $JFILE. I quit!"
  exit 1
fi

# Check if command line agruments are given:  $1=output_file , $2=sampling_interval
if [ $# -eq 0 ]; then
  echo "No output filename supplied. Using default: $OUTFILE ..."
else
  OUTFILE=$1
  echo "Output filename: $OUTFILE "
  if [ $# -ge 2 ]; then
    if [[ $2 =~ $re ]] ; then
      SAMPLING=$2
      echo "Sampling time: $SAMPLING "
    fi
  fi
fi

# Check if old output file exists (just print a warning and overwrite it)
if [ -f "$OUTFILE" ]; then
  echo "Previous IPMI file found: $OUTFILE. It will be replaced!"
  rm $OUTFILE
fi

if [ $SAMPLING -ge 1 ]; then
  # Read the dump file every $SAMPLING seconds 
  while true
  do
    sleep $SAMPLING &
    json_to_csv $JFILE $OUTFILE
    wait
  done
else
  # Read the dump file when it changes (requires 'ifnotify-tools') 
  while inotifywait -q -e close_write ${JFILE} 1>/dev/null
  do
    json_to_csv $JFILE $OUTFILE
  done
fi
