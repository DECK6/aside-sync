#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
TOOLS_DIR="$HOME/.aside/tools"
TRASH_DIR="$HOME/.Trash"

mkdir -p "$TOOLS_DIR" "$TRASH_DIR"

for source in "$SCRIPT_DIR"/bin/*; do
    [ -f "$source" ] || continue
    name=$(basename "$source")
    destination="$TOOLS_DIR/$name"
    if [ -e "$destination" ]; then
        stamp=$(date +%Y%m%d-%H%M%S)
        backup="$TRASH_DIR/$name.aside-sync-backup.$stamp.$$"
        counter=0
        while [ -e "$backup" ]; do
            counter=$((counter + 1))
            backup="$TRASH_DIR/$name.aside-sync-backup.$stamp.$$.${counter}"
        done
        mv "$destination" "$backup"
        printf 'Moved existing %s to %s\n' "$destination" "$backup"
    fi
    cp "$source" "$destination"
    chmod 755 "$destination"
    printf 'Installed %s\n' "$destination"
done

printf '\nRun: %s setup\n' "$TOOLS_DIR/aside-syncd"
