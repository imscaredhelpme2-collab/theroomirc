"""Imported first by every test module: a throw-away database, no webhooks, no auto-start, and a fake IRC server on 127.0.0.1."""
import os
import sys
import tempfile

if "ROOM_DB_DIR" not in os.environ or not os.environ.get("ROOM_TEST_ENV"):
    os.environ["ROOM_DB_DIR"] = tempfile.mkdtemp(prefix="room-test-")
    os.environ["ROOM_TEST_ENV"] = "1"
os.environ["MACRODROID_WEBHOOK_URL"] = ""
os.environ["ROOM_IRC_ALLOW_LOCAL"] = "1"
os.environ["ROOM_IRC_PORTS"] = "any"
os.environ["ROOM_IRC_AUTOSTART"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, ".."), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
TMP = os.environ["ROOM_DB_DIR"]
