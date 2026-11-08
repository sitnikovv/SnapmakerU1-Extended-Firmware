#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

# Extract the files the behavior tests load from a U1 rootfs image. Paths
# that an older image does not have are skipped.
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
    etc/hooks/moonraker.d \
    usr/local/bin/extended-config.py \
    usr/local/bin/spoollink_migrate.py \
    usr/local/share/firmware-config/functions \
    usr/local/share/firmware-config/extended \
    usr/local/filament-ui/html/script.js >/dev/null
sha256sum "$image" | cut -d' ' -f1 > "$target/image.sha256"
(cd "$target/rootfs" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$target/files.sha256"
echo "extracted $(wc -l < "$target/files.sha256") files from $(cat "$target/image.sha256")"
