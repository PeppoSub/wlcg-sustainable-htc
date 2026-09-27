#!/bin/bash

## jdump.sh v1.5 by Emanuele & Claude
##
## Dumps system metrics to a JSON (& CSV) file, updated every INTERVAL seconds.
##
## v1.5: RAPL overflow correction.
##       max_energy_range_uj is read at startup per domain (hardware-defined, varies by CPU).
##       On counter wraparound (Delta_E < 0):  Delta_E_corrected = (max_energy_range_uj - E_last) + E_now
##       Single-wrap per interval is physically guaranteed at server TDP and 60s windows.
##       No NaN on wraparound anymore; NaN is now reserved for genuine read failures only.
##       No non-ASCII characters in code (microjoules written as "uj", deltas as "Delta_").
## v1.4: unified RAPL/IPMI averaging window; /proc/stat CPU metric; write only at boundary.
## v1.3: unified sampling strategy; IPMI accumulator; independent RAPL timer removed.
## v1.2: RAPL average Watts via Delta_E/Delta_t; independent 60s RAPL timer.
## v1.1: RAPL domain discovery at startup; cumulative Joules output.
## v1.0: when A.I. was not a thing ...
##
## Output JSON/CSV fields:
##  date_yyyymmdd       -- timestamp of boundary write
##  time_hhmmss         -- timestamp of boundary write
##  cpu_usage_percent   -- 1s /proc/stat delta, sampled at boundary
##  memory_usage_gb     -- instantaneous at boundary
##  cpu_frequency_ghz   -- instantaneous at boundary
##  ipmi_power_watt     -- mean of all IPMI readings accumulated in the window
##  ipmi_samples        -- number of valid IPMI readings in the window
##  rapl_package_watt   -- Delta_E/Delta_t over window, all sockets summed, overflow-corrected
##  rapl_dram_watt      -- same for DRAM domain (NaN on AMD typically: domain absent)
##
## Run as root background process:
##  $ sudo ./jdump.sh [sampling_sec [interval_sec]] &
##  e.g. $ sudo ./jdump.sh 5 60 &    (recommended)
##       $ sudo ./jdump.sh 30 30 &   (for testing)
##
## Root crontab for boot start:
##  @reboot /opt/jdump.sh 5 60
##

EXPORT_DIR="/tmp"
VAR_DIR="/var"
EXPORT_JSON="ipminfo.json"
EXPORT_CSV="ipminfo.csv"
SAMPLING="5"
INTERVAL="60"

re='^[0-9]+([.][0-9]+)?$'

if [ $# -ge 1 ] && [[ $1 =~ $re ]]; then SAMPLING=$1; fi
if [ $# -ge 2 ] && [[ $2 =~ $re ]]; then INTERVAL=$2; fi

if [ "$INTERVAL" -lt "$SAMPLING" ]; then
  echo "Warning: INTERVAL ($INTERVAL) < SAMPLING ($SAMPLING), setting INTERVAL=SAMPLING"
  INTERVAL=$SAMPLING
fi
echo "jdump.sh v1.5: SAMPLING=${SAMPLING}s, INTERVAL=${INTERVAL}s"

## ---------------------------------------------------------------------------
## RAPL domain discovery (once at startup)
## Matches on sysfs "name" file content, not on :X:Y index (index not stable
## across kernel/BIOS versions). Sums all sockets.
## Reads max_energy_range_uj per domain for overflow correction.
## ---------------------------------------------------------------------------
RAPL_BASE="/sys/class/powercap/intel-rapl"
RAPL_PKG_FILES=()
RAPL_DRAM_FILES=()
RAPL_PKG_MAX_UJ=()
RAPL_DRAM_MAX_UJ=()
RAPL_AVAILABLE=0

if [ -d "$RAPL_BASE" ]; then
  for zone in "$RAPL_BASE"/intel-rapl:*; do
    [ -d "$zone" ] || continue
    zname=$(basename "$zone")
    if [[ "$zname" =~ ^intel-rapl:[0-9]+$ ]]; then
      if [ -r "$zone/energy_uj" ]; then
        dname=$(cat "$zone/name" 2>/dev/null)
        if [[ "$dname" == package-* ]]; then
          RAPL_PKG_FILES+=("$zone/energy_uj")
          max_val=$(cat "$zone/max_energy_range_uj" 2>/dev/null)
          if [[ "$max_val" =~ ^[0-9]+$ ]]; then
            RAPL_PKG_MAX_UJ+=("$max_val")
          else
            RAPL_PKG_MAX_UJ+=("0")   # 0 disables overflow correction for this socket
          fi
          RAPL_AVAILABLE=1
        fi
      fi
      for sub in "$zone"/intel-rapl:*; do
        [ -d "$sub" ] || continue
        [ -r "$sub/energy_uj" ] || continue
        sdname=$(cat "$sub/name" 2>/dev/null)
        if [[ "$sdname" == "dram" ]]; then
          RAPL_DRAM_FILES+=("$sub/energy_uj")
          max_val=$(cat "$sub/max_energy_range_uj" 2>/dev/null)
          if [[ "$max_val" =~ ^[0-9]+$ ]]; then
            RAPL_DRAM_MAX_UJ+=("$max_val")
          else
            RAPL_DRAM_MAX_UJ+=("0")
          fi
        fi
      done
    fi
  done
fi

if [ "$RAPL_AVAILABLE" -eq 1 ]; then
  echo "RAPL: found ${#RAPL_PKG_FILES[@]} package domain(s), ${#RAPL_DRAM_FILES[@]} dram domain(s)."
  for i in "${!RAPL_PKG_FILES[@]}"; do
    echo "  package[$i]: ${RAPL_PKG_FILES[$i]}  max_range=${RAPL_PKG_MAX_UJ[$i]} uj"
  done
  for i in "${!RAPL_DRAM_FILES[@]}"; do
    echo "  dram[$i]:    ${RAPL_DRAM_FILES[$i]}  max_range=${RAPL_DRAM_MAX_UJ[$i]} uj"
  done
else
  echo "RAPL: not available on this system. Will report NaN."
fi

## ---------------------------------------------------------------------------
## sum_rapl_uj FILES_ARRAY
## Sums energy_uj across a list of sysfs files.
## Returns integer microjoules as a space-separated list (one per file),
## so the caller can do per-file overflow correction before summing.
## Returns "NaN" on any read failure.
## ---------------------------------------------------------------------------
read_rapl_uj()
{
  local files=("$@")
  local vals=()
  local v
  if [ ${#files[@]} -eq 0 ]; then echo "NaN"; return; fi
  for f in "${files[@]}"; do
    v=$(cat "$f" 2>/dev/null)
    if ! [[ "$v" =~ ^[0-9]+$ ]]; then echo "NaN"; return; fi
    vals+=("$v")
  done
  echo "${vals[@]}"
}

## ---------------------------------------------------------------------------
## compute_delta_with_overflow LAST_VALS NOW_VALS MAX_VALS
## Args: three space-separated lists of equal length (per-socket values).
## Returns total Delta_E in microjoules (integer), overflow-corrected.
## Returns "NaN" if any input is "NaN" or max_range is 0 (uncorrectable).
## ---------------------------------------------------------------------------
compute_delta_uj()
{
  local last_str="$1"
  local now_str="$2"
  local max_str="$3"

  if [[ "$last_str" == "NaN" || "$now_str" == "NaN" ]]; then echo "NaN"; return; fi

  read -ra last_arr <<< "$last_str"
  read -ra now_arr  <<< "$now_str"
  read -ra max_arr  <<< "$max_str"

  local total=0
  local n=${#last_arr[@]}
  local i delta_e

  for (( i=0; i<n; i++ )); do
    delta_e=$(( now_arr[i] - last_arr[i] ))
    if [ "$delta_e" -lt 0 ]; then
      if [ "${max_arr[$i]}" -eq 0 ]; then
        # max_range unknown: cannot correct, discard
        echo "NaN"; return
      fi
      # Single-wrap correction: Delta_E = (max - last) + now
      delta_e=$(( max_arr[i] - last_arr[i] + now_arr[i] ))
    fi
    total=$(( total + delta_e ))
  done
  echo "$total"
}

## ---------------------------------------------------------------------------
## Seed RAPL at startup
## ---------------------------------------------------------------------------
RAPL_LAST_PKG_UJ=$(read_rapl_uj "${RAPL_PKG_FILES[@]}")
RAPL_LAST_DRAM_UJ=$(read_rapl_uj "${RAPL_DRAM_FILES[@]}")
RAPL_PKG_MAX_STR="${RAPL_PKG_MAX_UJ[*]}"
RAPL_DRAM_MAX_STR="${RAPL_DRAM_MAX_UJ[*]}"
RAPL_LAST_TIME=$(date +%s)
NEXT_BOUNDARY=$(( RAPL_LAST_TIME + INTERVAL ))

RAPL_PKG_W="NaN"
RAPL_DRAM_W="NaN"
IPMI_AVG_W="NaN"
IPMI_NSAMPLES="0"
IPMI_SUM="0"
IPMI_COUNT="0"

## ---------------------------------------------------------------------------
## Main loop
## ---------------------------------------------------------------------------
while true
do
  sleep ${SAMPLING} &

  DATE=$(date +"%F")
  TIME=$(date +"%T")
  NOW=$(date +%s)

  # Accumulate IPMI into running sum (every SAMPLING seconds)
  IPMIQUERY=$(ipmitool dcmi power reading | grep "Instantaneous power reading:")
  arr=($IPMIQUERY)
  POWER=${arr[3]}
  if [[ ${POWER} =~ $re ]]; then
    IPMI_SUM=$(awk -v s="$IPMI_SUM" -v p="$POWER" 'BEGIN { printf "%.6f", s + p }')
    IPMI_COUNT=$(( IPMI_COUNT + 1 ))
  fi

  # At interval boundary: compute and publish averages
  if [ "$NOW" -ge "$NEXT_BOUNDARY" ]; then

    DT=$(( NOW - RAPL_LAST_TIME ))

    # CPU usage: 1s /proc/stat delta (done here, once per interval)
    _read_stat() { awk '/^cpu / {idle=$5+$6; t=0; for(i=2;i<=NF;i++) t+=$i; print idle, t}' /proc/stat; }
    _s1=$(_read_stat); sleep 1; _s2=$(_read_stat)
    CPUSE=$(echo "$_s1 $_s2" | awk '{id=$3-$1; tot=$4-$2; if(tot>0) printf "%.1f", 100-(id/tot*100); else print "NaN"}')
    if ! [[ ${CPUSE} =~ $re ]]; then CPUSE="NaN"; fi

    MEMUSE=$(free -tm | awk 'FNR == 2 {printf("%.2f"), $3/1024}')
    if ! [[ ${MEMUSE} =~ $re ]]; then MEMUSE="NaN"; fi

    FREQ=$(cpupower frequency-info -f | grep "current CPU frequency:" | awk '{print $4 / 1000000}')
    if ! [[ ${FREQ} =~ $re ]]; then FREQ="NaN"; fi

    # IPMI mean over window
    if [ "$IPMI_COUNT" -gt 0 ]; then
      IPMI_AVG_W=$(awk -v s="$IPMI_SUM" -v n="$IPMI_COUNT" 'BEGIN { printf "%.3f", s / n }')
      IPMI_NSAMPLES="$IPMI_COUNT"
    else
      IPMI_AVG_W="NaN"
      IPMI_NSAMPLES="0"
    fi

    # RAPL package: read, overflow-correct, convert to Watts
    RAPL_NOW_PKG_UJ=$(read_rapl_uj "${RAPL_PKG_FILES[@]}")
    DE_PKG=$(compute_delta_uj "$RAPL_LAST_PKG_UJ" "$RAPL_NOW_PKG_UJ" "$RAPL_PKG_MAX_STR")
    if [[ "$DE_PKG" != "NaN" && "$DT" -gt 0 ]]; then
      RAPL_PKG_W=$(awk -v de="$DE_PKG" -v dt="$DT" 'BEGIN { printf "%.3f", de / 1000000 / dt }')
    else
      RAPL_PKG_W="NaN"
    fi

    # RAPL DRAM: same
    RAPL_NOW_DRAM_UJ=$(read_rapl_uj "${RAPL_DRAM_FILES[@]}")
    DE_DRAM=$(compute_delta_uj "$RAPL_LAST_DRAM_UJ" "$RAPL_NOW_DRAM_UJ" "$RAPL_DRAM_MAX_STR")
    if [[ "$DE_DRAM" != "NaN" && "$DT" -gt 0 ]]; then
      RAPL_DRAM_W=$(awk -v de="$DE_DRAM" -v dt="$DT" 'BEGIN { printf "%.3f", de / 1000000 / dt }')
    else
      RAPL_DRAM_W="NaN"
    fi

    # Update state
    RAPL_LAST_PKG_UJ="$RAPL_NOW_PKG_UJ"
    RAPL_LAST_DRAM_UJ="$RAPL_NOW_DRAM_UJ"
    RAPL_LAST_TIME="$NOW"
    NEXT_BOUNDARY=$(( NOW + INTERVAL ))
    IPMI_SUM="0"
    IPMI_COUNT="0"

    # Write JSON
    cat > $EXPORT_DIR/$EXPORT_JSON <<EOF
{
  "date_yyyymmdd": "$DATE" ,
  "time_hhmmss": "$TIME" ,
  "cpu_usage_percent": "$CPUSE" ,
  "memory_usage_gb": "$MEMUSE" ,
  "cpu_frequency_ghz": "$FREQ" ,
  "ipmi_power_watt": "$IPMI_AVG_W" ,
  "ipmi_samples": "$IPMI_NSAMPLES" ,
  "rapl_package_watt": "$RAPL_PKG_W" ,
  "rapl_dram_watt": "$RAPL_DRAM_W"
}
EOF

    # Copy JSON to "VAR_DIR"" folder, if defined (for pool-users to read)
    if [ -n "$VAR_DIR" ]; then cp "$EXPORT_DIR/$EXPORT_JSON" "$VAR_DIR/"; fi

    # Write CSV
    if [ -n "$EXPORT_CSV" ]; then
      echo "${DATE} , ${TIME} , ${CPUSE} , ${IPMI_AVG_W} , ${MEMUSE} , ${FREQ} , ${RAPL_PKG_W} , ${RAPL_DRAM_W}" > "$EXPORT_DIR/$EXPORT_CSV"
    fi

  fi

  wait
done
