"""One shared SQLite file for all HTTP test modules.

``app.main`` creates a module-level Database at import time from
``BATTERY_DB_PATH``. When unittest imports several test modules in one
process, the first import wins; a shared path keeps every module (and the
raw-sqlite assertions in test_integration) pointed at the same database.
Cell ids are unique across tests, so sharing is safe.
"""

import os
import tempfile

DB_PATH = os.path.join(tempfile.mkdtemp(prefix="battery-test-db-"), "test.db")
os.environ["BATTERY_DB_PATH"] = DB_PATH
