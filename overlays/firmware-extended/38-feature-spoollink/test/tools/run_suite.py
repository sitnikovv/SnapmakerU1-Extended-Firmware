# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Run the behavior suite on one firmware tree.

Usage: run_suite.py <firmware> <run-dir> [--jobs N] [--only B01,B02]

<firmware> is a folder under firmware/ made by extract_firmware.sh.
U1BT_SERVER_IMAGE and U1BT_TAG_STORE select the Spoolman image and the tag
store; the run is recorded as the variant <firmware>@<store>@<image tag>.
The exit code is 1 when any test fails.
"""
import argparse
import concurrent.futures
import json
import os
import queue
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv' / 'bin' / 'python'
# Each parallel test takes ports from its own block below 32768.
PORT_FIRST = 20000
PORT_BLOCK = 400


def test_ids():
    sys.path.insert(0, str(ROOT / 'tests'))
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='*_test.py')
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(suite)
    return sorted(ids, key=lambda name: re.search(r'test_([A-Z]\d+)', name).group(1))


def short(test_id):
    return re.search(r'test_([A-Z]\d+)', test_id).group(1)


def run(test_id, firmware, run_dir, env):
    result = run_dir / 'results' / (short(test_id) + '.json')
    log = run_dir / 'results' / (short(test_id) + '.log')
    with open(log, 'wb') as stream:
        subprocess.run([str(PYTHON), '-B', '-W', 'ignore', str(ROOT / 'tools' / 'run_one.py'),
                        test_id, str(result)], cwd=ROOT / 'tests', env=env,
                       stdout=stream, stderr=stream, timeout=900)
    if not result.exists():
        return {'id': test_id, 'status': 'error', 'failed': [], 'detail': 'runner produced no result'}
    return json.loads(result.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('firmware')
    parser.add_argument('run_dir')
    parser.add_argument('--jobs', type=int, default=6)
    parser.add_argument('--only', default='')
    args = parser.parse_args()
    tree = ROOT / 'firmware' / args.firmware
    if (tree / 'files.sha256').exists():
        checked = subprocess.run(['sha256sum', '--check', '--quiet', '../files.sha256'],
                                 cwd=tree / 'rootfs', capture_output=True, text=True)
        if checked.returncode != 0:
            sys.exit('Firmware tree %s differs from files.sha256:\n%s' % (args.firmware, checked.stdout))
    run_dir = Path(args.run_dir).resolve()
    (run_dir / 'results').mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, U1BT_FIRMWARE=args.firmware, U1BT_RUN_DIR=str(run_dir))
    ids = test_ids()
    if args.only:
        wanted = set(args.only.split(','))
        ids = [test_id for test_id in ids if short(test_id) in wanted]
    if PORT_FIRST + args.jobs * PORT_BLOCK > 32768:
        sys.exit('Too many jobs for the port blocks below 32768')
    slots = queue.Queue()
    for slot in range(args.jobs):
        slots.put(slot)

    def run_in_slot(test_id):
        slot = slots.get()
        try:
            slot_env = dict(env, U1BT_PORT_BASE=str(PORT_FIRST + slot * PORT_BLOCK))
            return run(test_id, args.firmware, run_dir, slot_env)
        finally:
            slots.put(slot)
    started = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(run_in_slot, test_id): test_id for test_id in ids}
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            results.append(row)
            print('%-4s %-5s %6.1fs %s' % (short(row['id']), row['status'], row.get('seconds', 0),
                                          ', '.join(row.get('failed', [])) or row['detail'][:150]),
                  flush=True)
    results.sort(key=lambda row: short(row['id']))
    store = os.environ.get('U1BT_TAG_STORE', 'card_uids')
    image = os.environ.get('U1BT_SERVER_IMAGE', '')
    variant = '%s@%s@%s' % (args.firmware, store, image.rsplit(':', 1)[-1] if image else 'default')
    summary = {'firmware': args.firmware, 'variant': variant, 'tag_store': store,
               'server_image': image, 'started': started, 'seconds': round(time.time() - started),
               'results': results}
    (run_dir / 'summary.json').write_text(json.dumps(summary, indent=1))
    failed = [row for row in results if row['status'] not in ('pass', 'skip')]
    print('%d tests, %d not passed, %d s' % (len(results), len(failed), summary['seconds']))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
