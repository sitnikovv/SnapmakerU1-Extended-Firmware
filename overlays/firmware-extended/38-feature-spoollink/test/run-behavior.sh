#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

# Run the SpoolLink behavior tests on a built firmware image, once with each
# Spoolman setup: 0.26.1 and 0.27.0 with the card_uids store, and 0.27.0 with
# native tags. See README.md.
#
#   run-behavior.sh <upgrade.bin>             # all three setups
#   JOBS=3 run-behavior.sh <upgrade.bin>      # fewer tests at a time (default 6)
#
# Needs Linux, Python 3.11, Docker or podman, unsquashfs, and the image tools
# built with `make tools` in the repository root.

set -euo pipefail

TEST_DIR="$(dirname "$(realpath "$0")")"
REPO_DIR="$(realpath "$TEST_DIR/../../../..")"
IMAGE="$(realpath "${1:?usage: run-behavior.sh <upgrade.bin>}")"
JOBS="${JOBS:-6}"
if [[ -z "${U1BT_CONTAINER:-}" ]]; then
  U1BT_CONTAINER=docker
  command -v podman >/dev/null && U1BT_CONTAINER=podman
fi
export U1BT_CONTAINER

cd "$TEST_DIR"
if [[ ! -x .venv/bin/python ]]; then
  python3.11 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
for version in 0.26.1 0.27.0; do
  "$U1BT_CONTAINER" pull -q "ghcr.io/donkie/spoolman:$version" >/dev/null
done

UNPACKED="$(mktemp -d)"
trap 'rm -rf "$UNPACKED"' EXIT
"$REPO_DIR/scripts/extract_squashfs.sh" "$IMAGE" "$UNPACKED/image" >/dev/null
rm -rf firmware/built
tools/extract_firmware.sh "$UNPACKED/image/rk-unpacked/rootfs.img" firmware/built

failed=()
for setup in "0.26.1 card_uids" "0.27.0 card_uids" "0.27.0 native"; do
  read -r version store <<< "$setup"
  echo ">> Spoolman $version, tag store $store"
  rm -rf "runs/$version-$store"
  U1BT_SERVER_IMAGE="ghcr.io/donkie/spoolman:$version" U1BT_TAG_STORE="$store" \
    .venv/bin/python tools/run_suite.py built "runs/$version-$store" --jobs "$JOBS" \
    || failed+=("$version/$store")
  echo ""
done

if [[ ${#failed[@]} -gt 0 ]]; then
  echo ">> FAILED with ${failed[*]}"
  exit 1
fi
echo ">> All passed with Spoolman 0.26.1 and 0.27.0, both tag stores"
