#!/bin/sh
# Extract the files the behavior tests load from a U1 rootfs image.
# Usage: extract_firmware.sh <rootfs.img> <target-dir>
set -eu
image=$1
target=$2
if [ -e "$target" ]; then
    echo "target exists: $target" >&2
    exit 1
fi
mkdir -p "$target"
unsquashfs -q -n -d "$target/rootfs" "$image" \
    home/lava/klipper/klippy \
    home/lava/moonraker/moonraker \
    usr/local/share/spoollink \
    etc/hooks/klipper.d \
    etc/hooks/moonraker.d >/dev/null
sha256sum "$image" | cut -d' ' -f1 > "$target/image.sha256"
(cd "$target/rootfs" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$target/files.sha256"
echo "extracted $(wc -l < "$target/files.sha256") files from $(cat "$target/image.sha256")"
