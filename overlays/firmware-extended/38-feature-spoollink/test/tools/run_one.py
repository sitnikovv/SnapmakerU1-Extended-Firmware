# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Run one test and write its outcome as JSON.

Usage: run_one.py <test-id> <result.json>

The result lists the names of all failed checks.
"""
import json
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))


def check_name(test, trace):
    params = getattr(test, 'params', None)
    if params is not None and 'check' in params:
        return params['check']
    match = re.search(r'^\w*(?:Error|Exception): \[([^\]]+)\]', trace, re.MULTILINE)
    return match.group(1) if match else 'unnamed'


def last_line(trace):
    lines = [line for line in trace.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ''


def main():
    test_id, output = sys.argv[1:3]
    suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
    result = unittest.TestResult()
    started = time.time()
    suite.run(result)
    failures = [{'check': check_name(test, trace), 'detail': last_line(trace), 'trace': trace}
                for test, trace in result.failures]
    errors = [{'check': 'error', 'detail': last_line(trace), 'trace': trace}
              for test, trace in result.errors]
    if errors:
        status = 'error'
    elif failures:
        status = 'fail'
    elif result.skipped:
        status = 'skip'
    else:
        status = 'pass'
    detail = '; '.join('%s: %s' % (row['check'], row['detail']) for row in errors + failures)
    if status == 'skip':
        detail = result.skipped[0][1]
    Path(output).write_text(json.dumps({
        'id': test_id, 'status': status,
        'failed': sorted({row['check'] for row in failures}),
        'detail': detail, 'failures': errors + failures,
        'seconds': round(time.time() - started, 1)}, indent=1))


if __name__ == '__main__':
    main()
