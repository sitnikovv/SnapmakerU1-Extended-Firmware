# SpoolLink behavior tests

These tests check how a built firmware image records filament usage in Spoolman. They run the SpoolLink, Spoolman and Klipper files taken from the image, together with a real Spoolman server. Each test checks what a user can see: the used length of each spool in Spoolman, the spool assigned to each feeder channel, and the messages on the console.

## Terms

- A channel is one of the four feeder slots. Each channel feeds one print head.
- A tag is the RFID tag of a spool. Spoolman keeps the tag UID in an extra field of the spool.
- A link is the spool assigned to a channel. A channel gets a link when its tag is found in Spoolman or when a user selects a spool in Filament Manager.
- Clear is the Filament Manager action that removes the link of a channel and the tag from its spool in Spoolman.
- Waiting usage is filament that was fed while the spool was not known yet.

## Accounting rules

The tests check these rules.

1. A channel linked to a Spoolman spool charges its usage to that spool. A channel without a link charges nothing, except in the cases of rule 2.
2. Usage without a known spool waits until the spool is known.
   - 2a. If a new tag has a UID that no spool has, the usage is kept under this UID until a spool in Spoolman gets the tag. Any way of adding the tag works: creating a spool from the tag in Filament Manager, adding the UID to a spool in Spoolman, or reading the tag again. Then all saved usage is charged to that spool, and the channel is linked to it without another read.
   - 2b. If the spool of a channel disappears from Spoolman, the link is removed and the console shows a message. This happens when the spool is deleted during work or when `SET_SPOOL_ID` names a spool number that does not exist. If the channel has a tag, the usage waits under its UID as in 2a. This includes usage that was still unsent when the spool was deleted. If the channel has no tag, the usage waits until a spool is selected for the channel.
   - 2c. Waiting usage survives pauses, removal of a spool with a tag, and restarts of Moonraker and of the printer.
   - 2d. Clear, or removal of a spool without a tag, drops the usage that waits for a selection on that channel.
   - 2e. SpoolLink looks for the missing spool when Spoolman reports a spool change and at a growing interval. It does not poll Spoolman all the time.
3. A spool without a tag gets usage only after it is selected in Filament Manager.
4. If the first read after Klipper starts finds no tag on a channel that was linked through a tag, the link is removed. A manual link without a tag stays. Empty reads during work keep the link.
5. Clear keeps the usage that waits under a tag UID that no spool has. When a spool gets the tag, the usage goes to that spool.
6. If Spoolman cannot complete Clear and the same tag is read later, the channel stays unlinked and the previous spool gets nothing. The usage waits under the UID and goes to the next spool selected for the channel. The tag is removed from the previous spool once Spoolman is available again. This also holds after a Moonraker restart.
7. A link removed with `FILAMENT_DT_CLEAR` or `SET_PRINT_FILAMENT_CONFIG FILAMENT_SPOOL_ID=0` also stops the charging. Feed on that channel is not charged.
8. When the printer moves to another Spoolman server, unsent usage for the old server is never sent to the new server. Spool numbers do not carry over to the new server. Tags link the channels again.
9. If a spool is deleted and a new spool gets the same number, the channel stays unlinked from the new spool and charges it nothing.

Every usage report must reach Spoolman exactly once. This must hold when Spoolman is unavailable, when it answers with errors, when a reply is lost after Spoolman has recorded the usage, and when Moonraker restarts or is killed. A retraction that is fed back after a restart must not be counted as new filament.

## How the stand works

- `stand/simklippy.py` runs the Klipper reactor, `Printer`, the API server, the G-code dispatcher and the U1 modules `filament_detect`, `print_task_config` and `spoollink` from the image. Only hardware objects are replaced by small models: the toolhead, the extruder heaters and steppers, the filament sensors, the built-in RFID reader and `print_stats`. Extruder moves call the firmware's own `PrinterExtruder.move`, so the firmware's usage hook runs.
- Moonraker from the image runs as its own process with the `spoolman` and `spoollink` components.
- Each test starts a new Spoolman container. `stand/proxy.py` sits between Moonraker and Spoolman. It can refuse connections, return errors, hold a request or a reply, and let Spoolman record a request but drop the reply.
- `stand/rig.py` models OpenRFID in the `openrfid` mode. When a channel state becomes 1, it posts the tag of the spool in that channel to `filament_detect/set`.

The tests are grouped by rule: `test_rule1.py`, `test_rule2.py`, `test_rules34.py`, `test_edge_cases.py` (rule 2 and delivery), `test_delivery.py` (restarts, lost replies and outages) and `test_other.py` (Moonraker without Klipper SpoolLink, the plain Moonraker Spoolman component, the generic vendor option, the built-in reader and database writes).

## Running the tests

The stand needs Linux, Python 3.11, podman and `unsquashfs`.

```sh
cd overlays/firmware-extended/38-feature-spoollink/test
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
podman pull ghcr.io/donkie/spoolman:0.26.1
```

Build the firmware image. Then, from the repository root, build the image tools and unpack the image:

```sh
make tools
scripts/extract_squashfs.sh <upgrade.bin> /tmp/u1-unpacked
```

Extract the files the tests load and run the suite:

```sh
cd overlays/firmware-extended/38-feature-spoollink/test
tools/extract_firmware.sh /tmp/u1-unpacked/rk-unpacked/rootfs.img firmware/built
.venv/bin/python tools/run_suite.py built runs/1 --jobs 6
```

The runner prints one line per test and writes the logs of every process to `runs/1`. `U1BT_SERVER_IMAGE` selects another Spoolman image.
