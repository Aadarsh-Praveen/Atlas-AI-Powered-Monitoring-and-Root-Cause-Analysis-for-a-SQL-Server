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

from urllib.parse import quote, urlencode


def postgres_url(user: str, password: str, host: str, port: int, dbname: str, **options: str) -> str:
    """postgresql://user:password@host:port/dbname[?options], every part encoded."""
    def encode(value) -> str:
        return quote(str(value), safe="")

    url = f"postgresql://{encode(user)}:{encode(password)}@{host}:{port}/{encode(dbname)}"
    if options:
        url += "?" + urlencode(options)          # e.g. sslmode=require
    return url
