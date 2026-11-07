# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Log every SQLite write statement of the Moonraker process (test B43).

Only observes: each connection gets a trace callback, statements run unchanged.
"""
import os
import sqlite3
import threading
import time

_path = os.environ.get('U1BT_SQL_TRACE')
if _path:
    _lock = threading.Lock()
    _connect = sqlite3.connect

    def _trace(statement):
        if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'REPLACE', 'DELETE')):
            with _lock, open(_path, 'a') as stream:
                stream.write('%.3f %s\n' % (time.time(), ' '.join(statement.split())[:300]))

    def connect(*args, **kwargs):
        connection = _connect(*args, **kwargs)
        connection.set_trace_callback(_trace)
        return connection

    sqlite3.connect = connect
