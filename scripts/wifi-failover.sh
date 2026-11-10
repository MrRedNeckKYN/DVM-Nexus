#!/usr/bin/env bash
# ==============================================================================
# DVM Nexus - Automatic Failover Access Point Watchdog Daemon
# Target: Raspberry Pi 5 / MMDVM Repeater & Hotspot Controller
# File: /usr/local/bin/wifi-failover.sh
# ==============================================================================

set -u

INTERFACE="wlan0"
HOTSPOT_CON="Field-Hotspot"
HOTSPOT_SSID="MMDVM-Field-AP"
CHECK_INTERVAL=20          # Poll every 20 seconds
DISCONNECT_THRESHOLD=45    # 45 seconds disconnected before failover to AP
OVERRIDE_FLAG="/tmp/wifi-manual.override"

# Counter for consecutive seconds of disconnected client state
DISCONNECT_SEC=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [wifi-failover] $*"
}

is_manual_override() {
    if [[ -f "$OVERRIDE_FLAG" ]]; then
        local exp_time
        exp_time=$(cat "$OVERRIDE_FLAG" 2>/dev/null || echo 0)
        local cur_time
        cur_time=$(date +%s)
        if (( cur_time < exp_time )); then
            return 0
        else
            rm -f "$OVERRIDE_FLAG"
        fi
    fi
    return 1
}

get_active_con_name() {
    nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v dev="$INTERFACE" '$2 == dev {print $1}' | head -n1
}

has_gateway_connectivity() {
    # Check if wlan0 has an assigned IP address
    local ip
    ip=$(ip -4 addr show "$INTERFACE" 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1)
    if [[ -z "$ip" ]]; then
        return 1
    fi

    # Check if a default route exists via wlan0
    local gw
    gw=$(ip route show default dev "$INTERFACE" 2>/dev/null | awk '{print $3}' | head -n1)
    if [[ -z "$gw" ]]; then
        return 1
    fi

    # Quick ping test to default gateway or DNS (1 second timeout)
    if ping -q -c 1 -W 1 "$gw" >/dev/null 2>&1 || ping -q -c 1 -W 1 1.1.1.1 >/dev/null 2>&1; then
        return 0
    fi

    return 0  # Gateway present and IP assigned is considered connected
}

get_known_connections() {
    # Returns list of saved Wi-Fi connections formatted as NAME:SSID excluding hotspot
    nmcli -t -f NAME,802-11-wireless.ssid,TYPE connection show 2>/dev/null | awk -F: -v hot="$HOTSPOT_CON" '$3 == "802-11-wireless" && $1 != hot {
        ssid = ($2 != "" ? $2 : $1);
        print $1 ":" ssid;
    }'
}

log "Starting Wi-Fi Failover Watchdog Daemon on interface $INTERFACE..."

while true; do
    # Check if manual override from Web UI is currently active
    if is_manual_override; then
        log "Manual Web UI override active. Pausing automated watchdog failover."
        DISCONNECT_SEC=0
        sleep "$CHECK_INTERVAL"
        continue
    fi

    ACTIVE_CON=$(get_active_con_name)

    if [[ "$ACTIVE_CON" == "$HOTSPOT_CON" ]]; then
        # ----------------------------------------------------------------------
        # Currently in Field-Hotspot Mode: Scan for known home/station SSIDs
        # ----------------------------------------------------------------------
        DISCONNECT_SEC=0
        log "Field-Hotspot is ACTIVE. Running background scan for known client networks..."

        # Trigger background scan
        nmcli dev wifi rescan >/dev/null 2>&1 || true
        AVAILABLE_SSIDS=$(nmcli -t -f SSID dev wifi list 2>/dev/null | grep -v '^--$' | sort -u)

        KNOWN_LIST=$(get_known_connections)
        TARGET_CON=""
        TARGET_SSID=""

        while IFS=: read -r con_name con_ssid; do
            [[ -z "$con_name" ]] && continue
            [[ -z "$con_ssid" ]] && con_ssid="$con_name"
            if echo "$AVAILABLE_SSIDS" | grep -Fqx "$con_ssid" || echo "$AVAILABLE_SSIDS" | grep -Fqx "$con_name"; then
                TARGET_CON="$con_name"
                TARGET_SSID="$con_ssid"
                break
            fi
        done <<< "$KNOWN_LIST"

        if [[ -n "$TARGET_CON" ]]; then
            log "Known network '$TARGET_SSID' (profile '$TARGET_CON') detected in scan! Switching from Field-Hotspot back to client Wi-Fi..."
            nmcli connection down "$HOTSPOT_CON" >/dev/null 2>&1 || true
            sleep 2
            if nmcli connection up "$TARGET_CON" timeout 20 >/dev/null 2>&1; then
                log "Successfully reconnected to client Wi-Fi '$TARGET_SSID'."
            else
                log "Failed to connect to '$TARGET_CON'. Restoring Field-Hotspot to maintain connectivity..."
                nmcli connection up "$HOTSPOT_CON" timeout 25 >/dev/null 2>&1 || true
            fi
        fi

    elif [[ -n "$ACTIVE_CON" ]] && has_gateway_connectivity; then
        # ----------------------------------------------------------------------
        # Healthy Client Wi-Fi Connection
        # ----------------------------------------------------------------------
        DISCONNECT_SEC=0

    else
        # ----------------------------------------------------------------------
        # Disconnected or lost connectivity
        # ----------------------------------------------------------------------
        DISCONNECT_SEC=$((DISCONNECT_SEC + CHECK_INTERVAL))
        log "Wi-Fi disconnected or degraded on $INTERFACE ($DISCONNECT_SEC / $DISCONNECT_THRESHOLD sec)..."

        if (( DISCONNECT_SEC >= DISCONNECT_THRESHOLD )); then
            log "Wi-Fi disconnected for > $DISCONNECT_THRESHOLD seconds. Activating failover hotspot '$HOTSPOT_CON' ($HOTSPOT_SSID)..."
            
            # Deactivate any lingering stuck client connection
            if [[ -n "$ACTIVE_CON" ]]; then
                nmcli connection down "$ACTIVE_CON" >/dev/null 2>&1 || true
            fi

            # Bring up Field-Hotspot AP
            if nmcli connection up "$HOTSPOT_CON" timeout 25 >/dev/null 2>&1; then
                log "Field-Hotspot successfully brought UP at 10.42.0.1. SSID: $HOTSPOT_SSID"
                DISCONNECT_SEC=0
            else
                log "ERROR: Failed to bring up Field-Hotspot connection '$HOTSPOT_CON'!"
            fi
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
