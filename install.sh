#!/usr/bin/env bash
# Link virtual_bed_temp.py into Klipper's extras directory and restart Klipper.
# Idempotent; also used as Moonraker's update_manager install_script.
#   ./install.sh               install / refresh link
#   ./install.sh --uninstall   remove link and git exclude entry
# Env: KLIPPER_PATH (default ~/klipper), KLIPPER_SERVICE (default klipper),
#      MOONRAKER_URL (default http://localhost:7125), NO_RESTART=1 skips the
#      service restart. Restart: passwordless sudo systemctl, else Moonraker's
#      service API; failing both only prints a hint (exit status stays 0).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="virtual_bed_temp.py"
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
MOONRAKER_URL="${MOONRAKER_URL:-http://localhost:7125}"
EXTRAS="$KLIPPER_PATH/klippy/extras"
EXCLUDE="$KLIPPER_PATH/.git/info/exclude"
ENTRY="klippy/extras/$MODULE"

die() { echo "error: $*" >&2; exit 1; }

[ -d "$EXTRAS" ] || die "$EXTRAS not found — set KLIPPER_PATH"

# A service restart is required: Klipper's own RESTART keeps the old module
# imported. Never fails the script (Moonraker treats that as a failed update).
restart_klipper() {
    [ "${NO_RESTART:-0}" = 1 ] && return 0
    if command -v systemctl >/dev/null && sudo -n true 2>/dev/null; then
        echo "restarting $KLIPPER_SERVICE (systemctl)"
        sudo -n systemctl restart "$KLIPPER_SERVICE" && return 0
    fi
    if command -v curl >/dev/null && curl -fsS -m 10 -X POST \
            "$MOONRAKER_URL/machine/services/restart?service=$KLIPPER_SERVICE" >/dev/null 2>&1; then
        echo "restarting $KLIPPER_SERVICE (Moonraker)"
        return 0
    fi
    echo "could not restart Klipper — restart the $KLIPPER_SERVICE service to load the change"
}

case "${1:-}" in
    --uninstall)
        rm -f "$EXTRAS/$MODULE"
        [ -f "$EXCLUDE" ] && sed -i "\#^$ENTRY\$#d" "$EXCLUDE"
        echo "removed $EXTRAS/$MODULE — remove [virtual_bed_temp] from printer.cfg before restarting"
        exit 0 ;;
    "") ;;
    *) die "unknown argument: $1" ;;
esac

ln -sfn "$SRC_DIR/$MODULE" "$EXTRAS/$MODULE"
echo "linked $EXTRAS/$MODULE -> $SRC_DIR/$MODULE"

# Untracked file in the Klipper repo would make Moonraker report Klipper as dirty.
if [ -d "$KLIPPER_PATH/.git" ] && ! grep -qxF "$ENTRY" "$EXCLUDE" 2>/dev/null; then
    mkdir -p "$(dirname "$EXCLUDE")"
    echo "$ENTRY" >> "$EXCLUDE"
fi

restart_klipper
