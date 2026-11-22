"""Build a Postgres connection URL from separate settings, safely.

In a URL, some characters have a job: "@" ends the user:password part, ":"
separates user from password, "/" starts the database name, "?" starts the
options, "#" starts a fragment, and "%" starts an escape. A password that
contains one of them, pasted in as is, makes libpq split the URL in the wrong
place: with POSTGRES_PASSWORD "abc@123", the URL
postgresql://vendor:abc@123@localhost:5433/vendordb is read as host
"123@localhost" ("failed to resolve host").

urllib.parse.quote(value, safe="") percent-encodes every such character
("@" -> "%40", "#" -> "%23", "%" -> "%25"), and libpq decodes them back,
so the server receives exactly the password you set.

Used by seed.py and activity.py for their local defaults. URLs you paste in
whole (VENDOR_ADMIN_URL from Neon's console, VENDOR_ACTIVITY_URL) are used
as given: Neon's are already encoded.
"""

import sys
from pathlib import Path

# One implementation for the whole repo: tools/envconfig.py (layered settings).
# Kept here so vendor scripts and their tests can keep importing postgres_url.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from envconfig import postgres_url  # noqa: E402,F401
