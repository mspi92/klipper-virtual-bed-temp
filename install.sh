#!/usr/bin/env bash
# Link virtual_bed_temp.py into Klipper's extras directory and restart Klipper.
# Idempotent; also used as Moonraker's update_manager install_script.
#   ./install.sh               install / refresh link
#   ./install.sh --uninstall   remove link and git exclude entry
# Env: KLIPPER_PATH (default ~/klipper), KLIPPER_SERVICE (default klipper),
#      NO_RESTART=1 skips the service restart.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="virtual_bed_temp.py"
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
EXTRAS="$KLIPPER_PATH/klippy/extras"
EXCLUDE="$KLIPPER_PATH/.git/info/exclude"
ENTRY="klippy/extras/$MODULE"

die() { echo "error: $*" >&2; exit 1; }

[ -d "$EXTRAS" ] || die "$EXTRAS not found — set KLIPPER_PATH"

restart_klipper() {
    [ "${NO_RESTART:-0}" = 1 ] && return 0
    if command -v systemctl >/dev/null && systemctl list-unit-files "$KLIPPER_SERVICE.service" >/dev/null 2>&1; then
        echo "restarting $KLIPPER_SERVICE"
        sudo systemctl restart "$KLIPPER_SERVICE"
    else
        echo "restart Klipper manually to load the change"
    fi
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
