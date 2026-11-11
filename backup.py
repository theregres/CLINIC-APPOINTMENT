"""Database backups.

A backup is a full copy of every table, saved as one compressed JSON file. Where the files are kept:

  * Supabase Storage (private bucket "backups") when SUPABASE_URL and SUPABASE_SERVICE_KEY are set. Use this on
    Render: files there survive redeploys and restarts, and are separate from the database tables themselves.
  * A `backups/` folder next to the app otherwise (change it with BACKUP_DIR). Fine on your own computer.

An admin can back up, view, download and restore from the Backups page. A backup is also made automatically
every BACKUP_INTERVAL_DAYS (default 7): the app checks about once an hour whenever it is in use, so it works on hosts
that put the app to sleep (data only changes while the app is in use, so nothing is missed). Restoring replaces the
current data with the contents of a backup, after saving a safety copy of what is there now.

File layout (gzip): line 1 = small JSON header (date, type, row counts, checksum), line 2 = the table data.
Next to every backup there is a tiny "<name>.meta.json" with the header so the list loads quickly.
"""
import gzip
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import psycopg

from database import DB_CONFIG, ALL_TABLES

LOCAL_DIR = os.environ.get("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
BUCKET = os.environ.get("SUPABASE_BACKUP_BUCKET", "backups")
INTERVAL_DAYS = max(1, int(os.environ.get("BACKUP_INTERVAL_DAYS", "7")))
CHECK_EVERY_SECONDS = 3600
KEEP = {"auto": 12, "pre-restore": 5}  # manual backups are never deleted automatically
KINDS = ("manual", "auto", "pre-restore")
FORMAT_VERSION = 1
NAME_RE = re.compile(r"^backup_(\d{8})_(\d{6})_(manual|auto|pre-restore)\.json\.gz$")
META_SUFFIX = ".meta.json"
SECRET_COLUMNS = {"password_hash", "code_hash"}
PREVIEW_ROWS = 25

_lock = threading.Lock()
_last_check = [0.0]
status = {"last_error": None}


# INHERITANCE: custom error inherits from Exception.
class BackupError(Exception):
    """A backup could not be made, read or restored."""


# --------------------------------------------------------------------------------------------- where files live

# POLYMORPHISM: LocalStore and SupabaseStore have the same methods (write, read, names, delete, meta),
# so the backup code can use either one the same way.
class LocalStore:
    label = "a folder on this server"

    def __init__(self, directory: str):
        self.directory = directory

    # ENCAPSULATION: private helper used only inside this class.
    def _path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def write(self, name: str, data: bytes):
        os.makedirs(self.directory, exist_ok=True)
        temp = self._path(name) + ".part"
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, self._path(name))

    def read(self, name: str) -> bytes:
        try:
            with open(self._path(name), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise BackupError("That backup file was not found.")

    def names(self) -> list[str]:
        return os.listdir(self.directory) if os.path.isdir(self.directory) else []

    def delete(self, name: str):
        try:
            os.remove(self._path(name))
        except FileNotFoundError:
            pass

    def meta(self, name: str) -> dict | None:
        try:
            return json.loads(self.read(name + META_SUFFIX))
        except (BackupError, ValueError):
            pass
        try:  # a backup made before the small meta file existed: read the header line inside it
            with gzip.open(self._path(name), "rt", encoding="utf-8") as handle:
                header = json.loads(handle.readline())
            header["size"] = os.path.getsize(self._path(name))
            return header
        except (OSError, ValueError, EOFError):
            return None


# POLYMORPHISM: same methods as LocalStore, different place to keep the files.
class SupabaseStore:
    label = "Supabase Storage"

    def __init__(self, url: str, key: str, bucket: str):
        self.url, self.key, self.bucket = url.rstrip("/"), key, bucket

    # ENCAPSULATION: private helper used only inside this class.
    def _call(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None,
              extra: dict | None = None) -> bytes:
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra or {})
        request = urllib.request.Request(f"{self.url}/storage/v1/{path}", data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            if error.code in (401, 403):
                raise BackupError("Supabase Storage refused the key. Check SUPABASE_URL and SUPABASE_SERVICE_KEY (it must be "
                                  "the service_role / secret key, not the anon or publishable key).")
            if error.code == 404 or '"statusCode":"404"' in detail.replace(" ", "") or "not found" in detail.lower():
                raise BackupError(f"Not found in Supabase Storage ({path.split('/')[-1]}). If this is the bucket, create a "
                                  f"private bucket named \"{self.bucket}\".")
            raise BackupError(f"Supabase Storage error {error.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise BackupError(f"Could not reach Supabase Storage: {getattr(error, 'reason', error)}")

    def _object(self, name: str) -> str:
        return f"{self.bucket}/{urllib.parse.quote(name)}"

    def write(self, name: str, data: bytes):
        self._call("POST", f"object/{self._object(name)}", data, "application/octet-stream", {"x-upsert": "true"})

    def read(self, name: str) -> bytes:
        try:
            return self._call("GET", f"object/authenticated/{self._object(name)}")
        except BackupError as error:
            if str(error).startswith("Not found"):
                raise BackupError("That backup file was not found.")
            raise

    def names(self) -> list[str]:
        found: list[str] = []
        offset = 0
        while True:
            body = json.dumps({"prefix": "", "limit": 100, "offset": offset,
                               "sortBy": {"column": "name", "order": "desc"}}).encode()
            page = json.loads(self._call("POST", f"object/list/{self.bucket}", body, "application/json"))
            found += [item["name"] for item in page if item.get("name")]
            if len(page) < 100:
                return found
            offset += 100

    def delete(self, name: str):
        body = json.dumps({"prefixes": [name]}).encode()
        self._call("DELETE", f"object/{self.bucket}", body, "application/json")

    def meta(self, name: str) -> dict | None:
        try:
            return json.loads(self.read(name + META_SUFFIX))
        except (BackupError, ValueError):
            return None


# POLYMORPHISM: returns either store; the caller does not need to know which one it got.
def get_store():
    """Supabase Storage when its settings are present, otherwise a local folder."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if url and key:
        return SupabaseStore(url, key, BUCKET)
    return LocalStore(LOCAL_DIR)


def storage_warning() -> str | None:
    """A hosting problem the admin should know about (local files on Render are erased on every deploy)."""
    if isinstance(get_store(), LocalStore) and os.environ.get("RENDER"):
        return ("Backups are being saved on this server's own disk, which Render erases whenever the app is redeployed or "
                "restarted. Add SUPABASE_URL and SUPABASE_SERVICE_KEY in Render's Environment settings so backups are kept "
                "in Supabase Storage instead.")
    return None


# --------------------------------------------------------------------------------------------------- the data

def _connect():
    return psycopg.connect(**DB_CONFIG, autocommit=True)


def _as_json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def snapshot(conn) -> dict:
    """Every table as {name: [row, ...]}, read in a single statement so the copy is consistent."""
    parts = ", ".join(
        f"'{table}', (SELECT COALESCE(jsonb_agg(to_jsonb(x)), '[]'::jsonb) FROM {table} x)" for table in ALL_TABLES
    )
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT jsonb_build_object({parts})")
        return _as_json(cursor.fetchone()[0])


def current_counts() -> dict:
    """Number of rows now in each table."""
    query = " UNION ALL ".join(f"SELECT '{table}', count(*) FROM {table}" for table in ALL_TABLES)
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(query)
            return {name: int(count) for name, count in cursor.fetchall()}
    finally:
        conn.close()


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise BackupError("That is not a valid backup file name.")
    return name


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def create_backup(kind: str = "manual", created_by: str = "") -> str:
    """Save a backup now and return its file name."""
    if kind not in KINDS:
        raise BackupError("Unknown backup type.")
    with _lock:
        store = get_store()
        conn = _connect()
        try:
            tables = snapshot(conn)
        finally:
            conn.close()
        body = json.dumps(tables, sort_keys=True, separators=(",", ":"))
        existing = set(store.names())
        moment = datetime.now().replace(microsecond=0)
        while True:
            name = f"backup_{moment:%Y%m%d_%H%M%S}_{kind}.json.gz"
            if name not in existing:
                break
            moment += timedelta(seconds=1)
        header = {
            "app": "clinic-appointments", "format": FORMAT_VERSION, "created_at": moment.isoformat(sep=" "),
            "kind": kind, "created_by": created_by,
            "counts": {table: len(tables.get(table) or []) for table in ALL_TABLES}, "sha256": _digest(body),
        }
        payload = gzip.compress((json.dumps(header) + "\n" + body).encode("utf-8"))
        store.write(name, payload)
        store.write(name + META_SUFFIX, json.dumps({**header, "size": len(payload)}).encode("utf-8"))
        try:
            _prune(kind)
        except BackupError:
            pass  # the backup itself was saved; tidying up can wait for next time
        return name


def _prune(kind: str):
    keep = KEEP.get(kind)
    if keep is None:
        return
    for item in [b for b in list_backups() if b["kind"] == kind][keep:]:
        delete_backup(item["name"])


def list_backups() -> list[dict]:
    """All backups, newest first."""
    store = get_store()
    names = sorted((n for n in store.names() if NAME_RE.match(n)), reverse=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        headers = list(pool.map(store.meta, names))
    items = []
    for name, header in zip(names, headers):
        match = NAME_RE.match(name)
        items.append({
            "name": name, "kind": match.group(3), "broken": header is None,
            "size": (header or {}).get("size", 0), "created_by": (header or {}).get("created_by", ""),
            "counts": (header or {}).get("counts", {}),
            "created_at": datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S"),
        })
    return items


def read_backup_file(name: str) -> bytes:
    """The raw (compressed) backup file, for downloading."""
    return get_store().read(_check_name(name))


def load_backup(name: str) -> tuple[dict, dict]:
    """(header, tables) of a backup, after checking that it is complete and unchanged."""
    raw = get_store().read(_check_name(name))
    try:
        header_line, _, body = gzip.decompress(raw).decode("utf-8").partition("\n")
        header, tables = json.loads(header_line), json.loads(body)
    except (OSError, ValueError, EOFError):
        raise BackupError("That backup file is damaged and cannot be read.")
    if header.get("app") != "clinic-appointments" or header.get("format") != FORMAT_VERSION or not isinstance(tables, dict):
        raise BackupError("That file is not a backup made by this system.")
    if header.get("sha256") != _digest(body):
        raise BackupError("That backup file failed its integrity check (it was changed or damaged).")
    return header, tables


def delete_backup(name: str):
    store = get_store()
    _check_name(name)
    store.read(name)  # raises "not found" for a name that does not exist
    store.delete(name)
    store.delete(name + META_SUFFIX)


def restore_backup(name: str, created_by: str = "") -> dict:
    """Replace all current data with the contents of a backup. Everything succeeds or nothing changes."""
    header, tables = load_backup(name)
    safety = None
    try:
        safety = create_backup("pre-restore", created_by)  # a copy of what is there now, in case this was a mistake
    except Exception:
        pass  # a badly damaged database must still be restorable

    conn = _connect()
    try:
        listed = ", ".join(f"'{table}'" for table in ALL_TABLES)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                f"AND is_generated = 'NEVER' AND table_name IN ({listed}) ORDER BY table_name, ordinal_position"
            )
            columns: dict[str, list[str]] = {}
            for table, column in cursor.fetchall():
                columns.setdefault(table, []).append(column)
        missing = [table for table in ALL_TABLES if table not in columns]
        if missing:
            raise BackupError("The database is missing tables (" + ", ".join(missing) + "). Start the app once to recreate them, then restore.")

        everything = ", ".join(f'"{table}"' for table in ALL_TABLES)
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(f"TRUNCATE {everything}")
                for table in ALL_TABLES:
                    rows = tables.get(table) or []
                    if not rows:
                        continue
                    cols = ", ".join(f'"{column}"' for column in columns[table])
                    cursor.execute(
                        f'INSERT INTO "{table}" ({cols}) SELECT {cols} FROM jsonb_populate_recordset(NULL::"{table}", %s::jsonb)',
                        (json.dumps(rows),),
                    )
                cursor.execute(
                    "SELECT setval('account_no_seq', COALESCE(m, 1), m IS NOT NULL) FROM (SELECT max(n) m FROM ("
                    "SELECT account_no n FROM admins UNION ALL SELECT account_no FROM doctors "
                    "UNION ALL SELECT account_no FROM patients UNION ALL SELECT account_no FROM doctor_requests) q) z"
                )
                cursor.execute(
                    "SELECT setval('notifications_id_seq', COALESCE(m, 1), m IS NOT NULL) "
                    "FROM (SELECT max(id) m FROM notifications) z"
                )
    except BackupError:
        raise
    except Exception as error:
        raise BackupError(f"The restore failed and nothing was changed: {error}")
    finally:
        conn.close()
    return {"counts": header.get("counts", {}), "safety_copy": safety}


def preview(tables: dict, limit: int = PREVIEW_ROWS) -> list[dict]:
    """Tables prepared for display: secrets hidden, binary data summarised, long text shortened."""
    shown = []
    for table in ALL_TABLES:
        rows = tables.get(table) or []
        first = ("account_id", "id", "username", "name")
        columns = ([c for c in first if c in rows[0]] + sorted(c for c in rows[0] if c not in first)) if rows else []
        body = []
        for row in rows[:limit]:
            cells = []
            for column in columns:
                value = row.get(column)
                if column in SECRET_COLUMNS and value:
                    value = "•••••• (hidden)"
                elif isinstance(value, str) and re.fullmatch(r"\\x[0-9a-fA-F]{8,}", value):
                    value = f"(binary data, {(len(value) - 2) // 2:,} bytes)"
                elif value is None:
                    value = ""
                else:
                    value = str(value)
                cells.append(value if len(value) <= 80 else value[:77] + "...")
            body.append(cells)
        shown.append({"name": table, "count": len(rows), "columns": columns, "rows": body, "more": max(0, len(rows) - limit)})
    return shown


# ------------------------------------------------------------------------------------------ weekly schedule

def latest(kind: str | None = None, items: list[dict] | None = None) -> dict | None:
    for item in items if items is not None else list_backups():
        if not item["broken"] and (kind is None or item["kind"] == kind):
            return item
    return None


def next_automatic(items: list[dict] | None = None) -> datetime:
    """When the next automatic backup is due (right away if there has never been one)."""
    last = latest("auto", items)
    return last["created_at"] + timedelta(days=INTERVAL_DAYS) if last else datetime.now()


def auto_enabled() -> bool:
    return os.environ.get("BACKUP_AUTO", "on").strip().lower() not in ("off", "0", "false", "no")


def run_scheduled() -> str | None:
    """Make an automatic backup if the last one is older than the interval. Returns the new file name, if any."""
    try:
        if next_automatic() <= datetime.now():
            name = create_backup("auto", "automatic")
            status["last_error"] = None
            return name
    except Exception as error:
        status["last_error"] = f"{datetime.now():%Y-%m-%d %H:%M} – {error}"
    return None


def maybe_run_in_background():
    """Called on web requests: at most once an hour, check in the background whether the weekly backup is due."""
    if not auto_enabled():
        return
    now = time.time()
    if now - _last_check[0] < CHECK_EVERY_SECONDS:
        return
    _last_check[0] = now
    threading.Thread(target=run_scheduled, name="backup-check", daemon=True).start()
