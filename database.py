import os
import calendar
import hashlib
import hmac
import secrets
import psycopg
from datetime import date, datetime, time, timedelta
from psycopg import sql
from psycopg.abc import Params
from typing import LiteralString
from dotenv import load_dotenv
from models import (
    Record, Appointment, DuplicateRecordError, InvalidInputError, SERVICES,
    MAX_ACCOUNTS_PER_EMAIL, validate_password, STATUSES, clean_reason,
)

# Load credentials.env from the same folder as this file
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.env"))

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "port": 5432,
    "dbname": "postgres",
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "sslmode": "require",
}
ACCOUNT_TABLES = (("admin", "admins"), ("doctor", "doctors"), ("patient", "patients"))
ALL_TABLES = (
    "admins", "doctors", "patients", "doctor_requests", "appointments",
    "doctor_specializations", "doctor_schedules", "doctor_blocked_dates", "pending_registrations",
    "pending_password_resets", "profile_pictures", "notifications",
)
CODE_TTL_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SLOT_MINUTES = 30
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)
DEFAULT_START = time(9, 0)
DEFAULT_END = time(17, 0)
DATETIME_FORMAT = "%Y-%m-%d %H:%M"

Query = LiteralString | bytes | sql.SQL | sql.Composed

# ABSTRACTION: DataManager hides all the SQL behind simple methods (create_appointment, etc.).
class DataManager:
    def __init__(self):
        self._connect()
        self._healing = False
        self.repair_notice: list[str] = []  # tables that had to be recreated empty (shown to the admin)
        self._prepare_database()
        self._database: dict[str, Appointment] = {}
        self.load_from_file()

    def _prepare_database(self):
        """Create anything that is missing (tables, columns, indexes, the admin account). Safe to run again and again."""
        # A table someone recreated by hand with the wrong columns (and no rows) is replaced by the correct one.
        for _, table in ACCOUNT_TABLES:
            if self._fetch("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s", (table,)) \
                    and not self._fetch("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s AND column_name = 'username'", (table,)) \
                    and not self._fetch(sql.SQL("SELECT 1 FROM {t} LIMIT 1").format(t=sql.Identifier(table))):
                self._execute(sql.SQL("DROP TABLE {t}").format(t=sql.Identifier(table)))
        for role, table in ACCOUNT_TABLES:
            self._execute(
                sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {table} (
                        username      VARCHAR(50) PRIMARY KEY,
                        password_hash VARCHAR(255) NOT NULL,
                        name          VARCHAR(100) NOT NULL
                    )
                """).format(table=sql.Identifier(table))
            )
        self._execute("""
            CREATE TABLE IF NOT EXISTS doctor_requests (
                username        VARCHAR(50) PRIMARY KEY,
                password_hash   VARCHAR(255) NOT NULL,
                name            VARCHAR(100) NOT NULL,
                specializations VARCHAR(200) NOT NULL DEFAULT '',
                requested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("ALTER TABLE doctor_requests ADD COLUMN IF NOT EXISTS specializations VARCHAR(200) NOT NULL DEFAULT ''")
        for table in ("patients", "doctors", "doctor_requests"):
            self._execute(
                sql.SQL("ALTER TABLE {table} ADD COLUMN IF NOT EXISTS email VARCHAR(255)").format(
                    table=sql.Identifier(table)
                )
            )
            # An email may now be shared by up to MAX_ACCOUNTS_PER_EMAIL accounts, so drop the old unique index.
            self._execute(sql.SQL("DROP INDEX IF EXISTS {idx}").format(idx=sql.Identifier(f"{table}_email_key")))
            self._execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {table} (LOWER(email))").format(
                    idx=sql.Identifier(f"{table}_email_idx"), table=sql.Identifier(table)
                )
            )
        self._execute("""
            CREATE TABLE IF NOT EXISTS pending_registrations (
                email           VARCHAR(255) PRIMARY KEY,
                kind            VARCHAR(10) NOT NULL,
                username        VARCHAR(50) NOT NULL,
                password_hash   VARCHAR(255) NOT NULL,
                name            VARCHAR(100) NOT NULL,
                specializations VARCHAR(200) NOT NULL DEFAULT '',
                code_hash       VARCHAR(64) NOT NULL,
                expires_at      TIMESTAMPTZ NOT NULL,
                attempts        SMALLINT NOT NULL DEFAULT 0,
                last_sent       TIMESTAMPTZ NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS pending_password_resets (
                email           VARCHAR(255) PRIMARY KEY,
                code_hash       VARCHAR(64) NOT NULL,
                expires_at      TIMESTAMPTZ NOT NULL,
                attempts        SMALLINT NOT NULL DEFAULT 0,
                last_sent       TIMESTAMPTZ NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS profile_pictures (
                username     VARCHAR(50) PRIMARY KEY,
                content_type VARCHAR(50) NOT NULL,
                data         BYTEA NOT NULL,
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id         SERIAL PRIMARY KEY,
                username   VARCHAR(50) NOT NULL,
                message    VARCHAR(500) NOT NULL,
                created_at TIMESTAMP NOT NULL,
                is_read    BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        self._execute("CREATE INDEX IF NOT EXISTS notifications_user_idx ON notifications (username, is_read, id DESC)")
        self._execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id            VARCHAR(50) PRIMARY KEY,
                patient_name  VARCHAR(100) NOT NULL,
                doctor        VARCHAR(100) NOT NULL,
                service       VARCHAR(100) NOT NULL,
                datetime_str  VARCHAR(50) NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS doctor_specializations (
                username       VARCHAR(50) NOT NULL,
                specialization VARCHAR(100) NOT NULL,
                PRIMARY KEY (username, specialization)
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS doctor_schedules (
                username    VARCHAR(50) NOT NULL,
                weekday     SMALLINT NOT NULL,
                start_time  TIME NOT NULL,
                end_time    TIME NOT NULL,
                PRIMARY KEY (username, weekday)
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS doctor_blocked_dates (
                username     VARCHAR(50) NOT NULL,
                blocked_date DATE NOT NULL,
                PRIMARY KEY (username, blocked_date)
            )
        """)
        self._execute("CREATE SEQUENCE IF NOT EXISTS appointment_id_seq")
        self._execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'scheduled'")
        self._execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reason VARCHAR(300) NOT NULL DEFAULT ''")
        self._execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS patient_username VARCHAR(50)")
        # A cancelled appointment must free its slot, so the "one booking per doctor and time" rule only covers active ones.
        self._execute("DROP INDEX IF EXISTS appointments_doctor_slot_key")
        try:
            self._execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS appointments_active_slot_key ON appointments (doctor, datetime_str) "
                "WHERE status <> 'cancelled'"
            )
        except psycopg.Error:
            pass
        # Link older appointments to their patient account when the name matches exactly one patient.
        self._execute(
            "UPDATE appointments a SET patient_username = p.username FROM patients p "
            "WHERE a.patient_username IS NULL AND LOWER(p.name) = LOWER(a.patient_name) "
            "AND (SELECT COUNT(*) FROM patients p2 WHERE LOWER(p2.name) = LOWER(a.patient_name)) = 1"
        )
        # Every account gets a permanent number (shown as 000-000). One shared sequence keeps them unique across all roles.
        self._execute("CREATE SEQUENCE IF NOT EXISTS account_no_seq MINVALUE 1")
        for table in ("admins", "doctors", "patients", "doctor_requests"):
            t = sql.Identifier(table)
            self._execute(sql.SQL("ALTER TABLE {t} ADD COLUMN IF NOT EXISTS account_no INTEGER").format(t=t))
            self._execute(
                sql.SQL("UPDATE {t} a SET account_no = n.v FROM (SELECT username, nextval('account_no_seq') v FROM "
                        "(SELECT username FROM {t} WHERE account_no IS NULL ORDER BY username) s) n "
                        "WHERE a.username = n.username").format(t=t)
            )
            self._execute(sql.SQL("ALTER TABLE {t} ALTER COLUMN account_no SET DEFAULT nextval('account_no_seq')").format(t=t))
            # Readable copy (000-000) kept in step with account_no by the database, so it also shows in the table editor.
            self._execute(
                sql.SQL("ALTER TABLE {t} ADD COLUMN IF NOT EXISTS account_id TEXT GENERATED ALWAYS AS "
                        "(lpad((account_no / 1000)::text, 3, '0') || '-' || lpad((account_no % 1000)::text, 3, '0')) STORED").format(t=t)
            )
            self._execute(
                sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {t} (account_no)").format(
                    idx=sql.Identifier(f"{table}_account_no_key"), t=t)
            )
        for table in ALL_TABLES:
            self._execute(
                sql.SQL("ALTER TABLE {table} ENABLE ROW LEVEL SECURITY").format(
                    table=sql.Identifier(table)
                )
            )
        self._seed_admin()
        for row in self._fetch("SELECT username FROM doctors WHERE username NOT IN (SELECT username FROM doctor_schedules)"):
            self._set_default_schedule(row[0])
        for row in self._fetch("SELECT username FROM doctors WHERE username NOT IN (SELECT username FROM doctor_specializations)"):
            self._execute(
                "INSERT INTO doctor_specializations (username, specialization) VALUES (%s, %s)",
                (row[0], SERVICES[0])
            )
        for table, column in (("doctors", "name"), ("doctor_requests", "name"), ("appointments", "doctor")):
            self._execute(
                sql.SQL("UPDATE {table} SET {column} = regexp_replace({column}, %s, '') WHERE LOWER({column}) LIKE %s").format(
                    table=sql.Identifier(table), column=sql.Identifier(column)
                ),
                (r"^[Dd][Rr]\.\s+", "dr.%"),
            )

    def _heal(self, force: bool = True):
        """A table is missing (deleted by mistake or by an attacker): recreate the empty structure so the app and the admin login keep working."""
        present = {row[0] for row in self._fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        with_username = {row[0] for row in self._fetch("SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'username'")}
        missing = [table for table in ALL_TABLES if table not in present or (table in dict(ACCOUNT_TABLES).values() and table not in with_username)]
        if not force and not missing:
            return
        self._healing = True
        try:
            self._prepare_database()
        finally:
            self._healing = False
        for table in missing:
            if table not in self.repair_notice:
                self.repair_notice.append(table)

    def repair_schema(self):
        """Recreate any missing table now (used by the Backups pages, so a restore always has tables to fill)."""
        self._heal(force=False)

    def clear_repair_notice(self):
        self.repair_notice.clear()

    def _seed_admin(self):
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "").strip()
        if not username or not password:
            if not self._fetch("SELECT 1 FROM admins"):
                raise RuntimeError("No admin account exists. Add ADMIN_USERNAME and ADMIN_PASSWORD to credentials.env.")
            return
        rows = self._fetch("SELECT password_hash FROM admins WHERE username = %s", (username,))
        if not rows:
            if self._username_taken(username):
                raise RuntimeError("ADMIN_USERNAME is already used by another account. Choose a different admin username.")
            self._execute(
                "INSERT INTO admins (username, password_hash, name) VALUES (%s, %s, %s)",
                (username, self._hash_password(password), "Administrator")
            )
        elif not self._verify_password(password, rows[0][0]):
            self._execute(
                "UPDATE admins SET password_hash = %s WHERE username = %s",
                (self._hash_password(password), username)
            )

    def _connect(self):
        self._conn = psycopg.connect(**DB_CONFIG, autocommit=True)

    def _ensure_connection(self):
        if self._conn.closed:
            self._connect()

    # ENCAPSULATION: private helper (leading underscore), used only inside this class.
    def _execute(self, query: Query, params: Params | None = None):
        self._ensure_connection()
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)
        except psycopg.OperationalError:
            self._connect()
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            if self._healing:
                raise
            self._heal()
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)

    # ENCAPSULATION: private helper (leading underscore), used only inside this class.
    def _fetch(self, query: Query, params: Params | None = None) -> list:
        self._ensure_connection()
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        except psycopg.OperationalError:
            self._connect()
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            if self._healing:
                raise
            self._heal()
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return salt.hex() + ":" + digest.hex()

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        try:
            salt_hex, digest_hex = stored.split(":")
            salt = bytes.fromhex(salt_hex)
        except (ValueError, AttributeError):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(digest.hex(), digest_hex)

    def authenticate(self, identifier: str, password: str) -> dict | None:
        """Log in with either a username or an email address."""
        identifier = identifier.strip()
        candidates = [identifier]
        if "@" in identifier:
            for _, found_username in self._find_users_by_email(identifier.lower()):
                if found_username not in candidates:
                    candidates.append(found_username)
        for username in candidates:
            for role, table in ACCOUNT_TABLES:
                rows = self._fetch(
                    sql.SQL("SELECT password_hash, name, account_no FROM {table} WHERE username = %s").format(
                        table=sql.Identifier(table)
                    ),
                    (username,),
                )
                if rows and self._verify_password(password, rows[0][0]):
                    return {"role": role, "name": rows[0][1], "username": username, "account_no": rows[0][2]}
        return None

    def _username_taken(self, username: str) -> bool:
        for table in ("admins", "doctors", "patients", "doctor_requests"):
            if self._fetch(
                sql.SQL("SELECT 1 FROM {table} WHERE username = %s").format(table=sql.Identifier(table)),
                (username,),
            ):
                return True
        return False

    def _doctor_name_taken(self, name: str) -> bool:
        return bool(
            self._fetch("SELECT 1 FROM doctors WHERE LOWER(name) = LOWER(%s)", (name,)) or
            self._fetch("SELECT 1 FROM doctor_requests WHERE LOWER(name) = LOWER(%s)", (name,))
        )

    def add_user(self, username: str, password: str, name: str):
        if self._username_taken(username):
            raise DuplicateRecordError("Username already exists.")
        self._execute(
            "INSERT INTO patients (username, password_hash, name) VALUES (%s, %s, %s)",
            (username, self._hash_password(password), name)
        )

    def add_doctor_request(self, username: str, password: str, name: str, specializations: list[str]):
        specializations = list(dict.fromkeys(specializations))
        if not 1 <= len(specializations) <= 2:
            raise InvalidInputError("Please choose 1 or 2 specializations.")
        for specialization in specializations:
            if specialization not in SERVICES:
                raise InvalidInputError(f"'{specialization}' is not a valid specialization.")
        if self._username_taken(username):
            raise DuplicateRecordError("Username already exists.")
        if self._doctor_name_taken(name):
            raise DuplicateRecordError("A doctor with this name is already registered or pending approval.")
        self._execute(
            "INSERT INTO doctor_requests (username, password_hash, name, specializations) VALUES (%s, %s, %s, %s)",
            (username, self._hash_password(password), name, ",".join(specializations))
        )

    @staticmethod
    def _code_hash(email: str, code: str) -> str:
        return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()

    @staticmethod
    def _new_code() -> str:
        return f"{secrets.randbelow(10 ** 6):06d}"

    def _email_count(self, email: str) -> int:
        total = 0
        for table in ("patients", "doctors", "doctor_requests"):
            total += int(self._fetch(
                sql.SQL("SELECT COUNT(*) FROM {table} WHERE LOWER(email) = LOWER(%s)").format(table=sql.Identifier(table)),
                (email,),
            )[0][0])
        return total

    def _find_users_by_email(self, email: str) -> list[tuple[str, str]]:
        """Every (table, username) with this email; one email can have up to MAX_ACCOUNTS_PER_EMAIL accounts."""
        found: list[tuple[str, str]] = []
        for table in ("patients", "doctors"):
            for row in self._fetch(
                sql.SQL("SELECT username FROM {table} WHERE LOWER(email) = LOWER(%s) ORDER BY username").format(
                    table=sql.Identifier(table)
                ),
                (email,),
            ):
                found.append((table, row[0]))
        return found

    def count_accounts_for_email(self, email: str) -> int:
        return len(self._find_users_by_email(email.strip().lower()))

    def start_registration(self, kind: str, username: str, password: str, name: str, email: str,
                           specializations: list[str] | None = None) -> str:
        specializations = list(dict.fromkeys(specializations or []))
        if kind == "doctor":
            if not 1 <= len(specializations) <= 2:
                raise InvalidInputError("Please choose 1 or 2 specializations.")
            for specialization in specializations:
                if specialization not in SERVICES:
                    raise InvalidInputError(f"'{specialization}' is not a valid specialization.")
        validate_password(password)
        self._execute("DELETE FROM pending_registrations WHERE expires_at <= NOW()")
        if self._username_taken(username) or self._fetch(
            "SELECT 1 FROM pending_registrations WHERE username = %s AND email <> %s", (username, email)
        ):
            raise DuplicateRecordError("Username already exists.")
        if self._email_count(email) >= MAX_ACCOUNTS_PER_EMAIL:
            raise DuplicateRecordError(f"This email already has the maximum of {MAX_ACCOUNTS_PER_EMAIL} accounts. Please use a different email.")
        if kind == "doctor" and self._doctor_name_taken(name):
            raise DuplicateRecordError("A doctor with this name is already registered or pending approval.")
        wait = self._fetch(
            "SELECT CEIL(%s - EXTRACT(EPOCH FROM (NOW() - last_sent))) FROM pending_registrations "
            "WHERE email = %s AND NOW() - last_sent < make_interval(secs => %s)",
            (RESEND_COOLDOWN_SECONDS, email, RESEND_COOLDOWN_SECONDS),
        )
        if wait:
            raise InvalidInputError(f"A code was just sent. Please wait {int(wait[0][0])} seconds before trying again.")
        code = self._new_code()
        self._execute(
            "INSERT INTO pending_registrations "
            "(email, kind, username, password_hash, name, specializations, code_hash, expires_at, attempts, last_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + make_interval(mins => %s), 0, NOW()) "
            "ON CONFLICT (email) DO UPDATE SET kind = EXCLUDED.kind, username = EXCLUDED.username, "
            "password_hash = EXCLUDED.password_hash, name = EXCLUDED.name, "
            "specializations = EXCLUDED.specializations, code_hash = EXCLUDED.code_hash, "
            "expires_at = EXCLUDED.expires_at, attempts = 0, last_sent = EXCLUDED.last_sent",
            (email, kind, username, self._hash_password(password), name, ",".join(specializations),
             self._code_hash(email, code), CODE_TTL_MINUTES),
        )
        return code

    def has_pending(self, email: str) -> bool:
        return bool(self._fetch(
            "SELECT 1 FROM pending_registrations WHERE email = %s AND expires_at > NOW()", (email,)
        ))

    def cancel_pending(self, email: str):
        self._execute("DELETE FROM pending_registrations WHERE email = %s", (email,))

    def resend_code(self, email: str) -> str:
        rows = self._fetch(
            "SELECT CEIL(%s - EXTRACT(EPOCH FROM (NOW() - last_sent))) FROM pending_registrations "
            "WHERE email = %s AND expires_at > NOW()",
            (RESEND_COOLDOWN_SECONDS, email),
        )
        if not rows:
            raise InvalidInputError("This registration has expired. Please register again.")
        if rows[0][0] > 0:
            raise InvalidInputError(f"Please wait {int(rows[0][0])} seconds before requesting another code.")
        code = self._new_code()
        self._execute(
            "UPDATE pending_registrations SET code_hash = %s, attempts = 0, last_sent = NOW(), "
            "expires_at = NOW() + make_interval(mins => %s) WHERE email = %s",
            (self._code_hash(email, code), CODE_TTL_MINUTES, email),
        )
        return code

    def verify_registration(self, email: str, code: str) -> str:
        rows = self._fetch(
            "SELECT kind, username, password_hash, name, specializations, code_hash, attempts, expires_at <= NOW() "
            "FROM pending_registrations WHERE email = %s",
            (email,),
        )
        if not rows:
            raise InvalidInputError("No pending registration found. Please register again.")
        kind, username, password_hash, name, specializations, code_hash, attempts, expired = rows[0]
        if expired:
            self.cancel_pending(email)
            raise InvalidInputError("This code has expired. Please register again.")
        if not hmac.compare_digest(self._code_hash(email, code.strip()), code_hash):
            attempts += 1
            if attempts >= MAX_CODE_ATTEMPTS:
                self.cancel_pending(email)
                raise InvalidInputError("Too many incorrect attempts. Please register again.")
            self._execute("UPDATE pending_registrations SET attempts = %s WHERE email = %s", (attempts, email))
            raise InvalidInputError(f"Incorrect code. {MAX_CODE_ATTEMPTS - attempts} attempt(s) left.")
        try:
            if self._username_taken(username) or self._email_count(email) >= MAX_ACCOUNTS_PER_EMAIL:
                raise DuplicateRecordError("Username was taken or this email reached its account limit in the meantime. Please register again.")
            if kind == "patient":
                self._execute(
                    "INSERT INTO patients (username, password_hash, name, email) VALUES (%s, %s, %s, %s)",
                    (username, password_hash, name, email),
                )
            else:
                self._execute(
                    "INSERT INTO doctor_requests (username, password_hash, name, specializations, email) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (username, password_hash, name, specializations, email),
                )
        except psycopg.IntegrityError:
            self.cancel_pending(email)
            raise DuplicateRecordError("Username was taken in the meantime. Please register again.")
        except DuplicateRecordError:
            self.cancel_pending(email)
            raise
        self.cancel_pending(email)
        try:
            if kind == "patient":
                self.notify_admins(f"New patient registered: {name} (username: {username}).")
            else:
                self.notify_admins(f"New doctor registration from {name} (username: {username}). It is waiting for your approval under Doctor Requests.")
        except Exception:
            pass  # a notification problem must not undo a successful registration
        return kind

    def start_password_reset(self, email: str) -> str:
        email = email.strip().lower()
        if not self._find_users_by_email(email):
            raise InvalidInputError("No account found with this email address.")
        self._execute("DELETE FROM pending_password_resets WHERE expires_at <= NOW()")
        wait = self._fetch(
            "SELECT CEIL(%s - EXTRACT(EPOCH FROM (NOW() - last_sent))) FROM pending_password_resets "
            "WHERE email = %s AND NOW() - last_sent < make_interval(secs => %s)",
            (RESEND_COOLDOWN_SECONDS, email, RESEND_COOLDOWN_SECONDS),
        )
        if wait:
            raise InvalidInputError(f"A code was just sent. Please wait {int(wait[0][0])} seconds before trying again.")
        code = self._new_code()
        self._execute(
            "INSERT INTO pending_password_resets "
            "(email, code_hash, expires_at, attempts, last_sent) "
            "VALUES (%s, %s, NOW() + make_interval(mins => %s), 0, NOW()) "
            "ON CONFLICT (email) DO UPDATE SET code_hash = EXCLUDED.code_hash, "
            "expires_at = EXCLUDED.expires_at, attempts = 0, last_sent = EXCLUDED.last_sent",
            (email, self._code_hash(email, code), CODE_TTL_MINUTES),
        )
        return code

    def has_pending_reset(self, email: str) -> bool:
        return bool(self._fetch(
            "SELECT 1 FROM pending_password_resets WHERE email = %s AND expires_at > NOW()", (email,)
        ))

    def cancel_pending_reset(self, email: str):
        self._execute("DELETE FROM pending_password_resets WHERE email = %s", (email,))

    def resend_reset_code(self, email: str) -> str:
        rows = self._fetch(
            "SELECT CEIL(%s - EXTRACT(EPOCH FROM (NOW() - last_sent))) FROM pending_password_resets "
            "WHERE email = %s AND expires_at > NOW()",
            (RESEND_COOLDOWN_SECONDS, email),
        )
        if not rows:
            raise InvalidInputError("This password reset request has expired. Please request a new code.")
        if rows[0][0] > 0:
            raise InvalidInputError(f"Please wait {int(rows[0][0])} seconds before requesting another code.")
        code = self._new_code()
        self._execute(
            "UPDATE pending_password_resets SET code_hash = %s, attempts = 0, last_sent = NOW(), "
            "expires_at = NOW() + make_interval(mins => %s) WHERE email = %s",
            (self._code_hash(email, code), CODE_TTL_MINUTES, email),
        )
        return code

    def verify_and_reset_password(self, email: str, code: str, new_password: str):
        email = email.strip().lower()
        rows = self._fetch(
            "SELECT code_hash, attempts, expires_at <= NOW() FROM pending_password_resets WHERE email = %s",
            (email,),
        )
        if not rows:
            raise InvalidInputError("No pending password reset found. Please request a new code.")
        code_hash, attempts, expired = rows[0]
        if expired:
            self.cancel_pending_reset(email)
            raise InvalidInputError("This code has expired. Please request a new code.")
        if not hmac.compare_digest(self._code_hash(email, code.strip()), code_hash):
            attempts += 1
            if attempts >= MAX_CODE_ATTEMPTS:
                self.cancel_pending_reset(email)
                raise InvalidInputError("Too many incorrect attempts. Please request a new code.")
            self._execute("UPDATE pending_password_resets SET attempts = %s WHERE email = %s", (attempts, email))
            raise InvalidInputError(f"Incorrect code. {MAX_CODE_ATTEMPTS - attempts} attempt(s) left.")
        
        accounts = self._find_users_by_email(email)
        if not accounts:
            self.cancel_pending_reset(email)
            raise InvalidInputError("Account not found.")

        validate_password(new_password)
        new_password_hash = self._hash_password(new_password)
        for table in dict.fromkeys(table for table, _ in accounts):
            self._execute(
                sql.SQL("UPDATE {table} SET password_hash = %s WHERE LOWER(email) = LOWER(%s)").format(
                    table=sql.Identifier(table)
                ),
                (new_password_hash, email),
            )
        self.cancel_pending_reset(email)

    def set_avatar(self, username: str, content_type: str, data: bytes) -> int:
        rows = self._fetch(
            "INSERT INTO profile_pictures (username, content_type, data, updated_at) VALUES (%s, %s, %s, NOW()) "
            "ON CONFLICT (username) DO UPDATE SET content_type = EXCLUDED.content_type, "
            "data = EXCLUDED.data, updated_at = EXCLUDED.updated_at "
            "RETURNING EXTRACT(EPOCH FROM updated_at)::bigint",
            (username, content_type, data),
        )
        return int(rows[0][0])

    def get_avatar(self, username: str) -> tuple[str, bytes] | None:
        rows = self._fetch("SELECT content_type, data FROM profile_pictures WHERE username = %s", (username,))
        return (rows[0][0], bytes(rows[0][1])) if rows else None

    def get_avatar_version(self, username: str) -> int | None:
        rows = self._fetch(
            "SELECT EXTRACT(EPOCH FROM updated_at)::bigint FROM profile_pictures WHERE username = %s",
            (username,),
        )
        return int(rows[0][0]) if rows else None

    def delete_avatar(self, username: str):
        self._execute("DELETE FROM profile_pictures WHERE username = %s", (username,))

    def count_accounts(self) -> tuple[int, int]:
        """(number of patients, number of doctors)"""
        row = self._fetch("SELECT (SELECT COUNT(*) FROM patients), (SELECT COUNT(*) FROM doctors)")[0]
        return int(row[0]), int(row[1])

    def is_doctor(self, username: str) -> bool:
        return bool(self._fetch("SELECT 1 FROM doctors WHERE username = %s", (username,)))

    def get_doctor_avatar_map(self) -> dict[str, tuple[str, int | None]]:
        """{doctor name: (username, picture version or None)} for showing doctor pictures in lists."""
        rows = self._fetch(
            "SELECT d.name, d.username, EXTRACT(EPOCH FROM p.updated_at)::bigint "
            "FROM doctors d LEFT JOIN profile_pictures p ON p.username = d.username"
        )
        return {row[0]: (row[1], int(row[2]) if row[2] else None) for row in rows}

    def get_doctor_requests(self) -> list[tuple]:
        return self._fetch("SELECT username, name, specializations FROM doctor_requests ORDER BY requested_at, username")

    def is_pending_doctor(self, identifier: str, password: str) -> bool:
        identifier = identifier.strip()
        rows = self._fetch(
            "SELECT password_hash FROM doctor_requests WHERE username = %s OR LOWER(email) = LOWER(%s)",
            (identifier, identifier),
        )
        return any(self._verify_password(password, row[0]) for row in rows)

    def approve_doctor(self, username: str):
        self._ensure_connection()
        try:
            with self._conn.transaction():
                with self._conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO doctors (username, password_hash, name, email, account_no) "
                        "SELECT username, password_hash, name, email, account_no FROM doctor_requests WHERE username = %s",
                        (username,)
                    )
                    if cursor.rowcount == 0:
                        raise KeyError(f"Registration request '{username}' not found.")
                    cursor.execute("SELECT specializations FROM doctor_requests WHERE username = %s", (username,))
                    row = cursor.fetchone()
                    specializations = [s for s in (row[0].split(",") if row else []) if s] or [SERVICES[0]]
                    for specialization in specializations:
                        cursor.execute(
                            "INSERT INTO doctor_specializations (username, specialization) VALUES (%s, %s)",
                            (username, specialization)
                        )
                    for weekday in DEFAULT_WORK_DAYS:
                        cursor.execute(
                            "INSERT INTO doctor_schedules (username, weekday, start_time, end_time) VALUES (%s, %s, %s, %s)",
                            (username, weekday, DEFAULT_START, DEFAULT_END)
                        )
                    cursor.execute("DELETE FROM doctor_requests WHERE username = %s", (username,))
        except psycopg.IntegrityError:
            raise DuplicateRecordError("Username already exists.")
        try:
            self.add_notification(username, "Your doctor account was approved. Welcome! Set your weekly hours under My Schedule so patients can book you.")
        except Exception:
            pass

    def reject_doctor(self, username: str):
        if not self._fetch("SELECT 1 FROM doctor_requests WHERE username = %s", (username,)):
            raise KeyError(f"Registration request '{username}' not found.")
        self._execute("DELETE FROM doctor_requests WHERE username = %s", (username,))

    def get_doctors(self) -> list[str]:
        return [row[0] for row in self._fetch("SELECT name FROM doctors ORDER BY name")]

    def get_doctor_accounts(self) -> list[tuple]:
        return self._fetch(
            "SELECT d.username, d.name, COALESCE(string_agg(s.specialization, ', ' ORDER BY s.specialization), ''), d.account_no "
            "FROM doctors d LEFT JOIN doctor_specializations s ON s.username = d.username "
            "GROUP BY d.username, d.name, d.account_no ORDER BY d.name"
        )

    # ---- in-app notifications
    def add_notification(self, username: str | None, message: str):
        if username:
            self._execute(  # local time, like the appointment times
                "INSERT INTO notifications (username, message, created_at) VALUES (%s, %s, %s)",
                (username, message[:500], datetime.now().replace(microsecond=0)),
            )

    def notify_admins(self, message: str):
        for (username,) in self._fetch("SELECT username FROM admins"):
            self.add_notification(username, message)

    def count_unread_notifications(self, username: str) -> int:
        rows = self._fetch("SELECT COUNT(*) FROM notifications WHERE username = %s AND NOT is_read", (username,))
        return int(rows[0][0]) if rows else 0

    def get_notifications(self, username: str, limit: int = 50) -> list[tuple]:
        """Newest first: (id, message, created_at, is_read)."""
        return self._fetch(
            "SELECT id, message, to_char(created_at, 'YYYY-MM-DD HH24:MI'), is_read "
            "FROM notifications WHERE username = %s ORDER BY id DESC LIMIT %s",
            (username, limit),
        )

    def mark_notifications_read(self, username: str):
        self._execute("UPDATE notifications SET is_read = TRUE WHERE username = %s AND NOT is_read", (username,))

    def clear_notifications(self, username: str):
        self._execute("DELETE FROM notifications WHERE username = %s", (username,))

    def get_doctor_username(self, name: str) -> str | None:
        rows = self._fetch("SELECT username FROM doctors WHERE name = %s", (name,))
        return rows[0][0] if rows else None

    def get_account_no(self, role: str, username: str) -> int | None:
        table = dict(ACCOUNT_TABLES).get(role)
        if table is None:
            return None
        rows = self._fetch(sql.SQL("SELECT account_no FROM {t} WHERE username = %s").format(t=sql.Identifier(table)), (username,))
        return rows[0][0] if rows else None

    def get_account_number_map(self) -> dict[str, dict[str, int]]:
        """{"doctor": {name: no}, "patient": {lower name: no}}; a patient name shared by two accounts is left out."""
        doctors = {name: no for name, no in self._fetch("SELECT name, account_no FROM doctors")}
        patients: dict[str, int | None] = {}
        for name, no in self._fetch("SELECT name, account_no FROM patients"):
            key = name.strip().lower()
            patients[key] = None if key in patients else no
        return {"doctor": doctors, "patient": {k: v for k, v in patients.items() if v is not None}}

    def get_profile(self, role: str, username: str) -> dict | None:
        table = dict(ACCOUNT_TABLES).get(role)
        if table is None:
            return None
        rows = self._fetch(
            sql.SQL("SELECT name FROM {table} WHERE username = %s").format(table=sql.Identifier(table)),
            (username,),
        )
        if not rows:
            return None
        profile: dict = {"role": role, "username": username, "name": rows[0][0], "email": None, "specializations": [],
                         "account_no": self.get_account_no(role, username)}
        if role in ("patient", "doctor"):
            email_rows = self._fetch(
                sql.SQL("SELECT email FROM {table} WHERE username = %s").format(table=sql.Identifier(table)),
                (username,),
            )
            profile["email"] = email_rows[0][0] if email_rows else None
        if role == "doctor":
            profile["specializations"] = self.get_doctor_specializations(username)
        return profile

    def change_password(self, role: str, username: str, current_password: str, new_password: str):
        if role == "admin":
            raise InvalidInputError("The admin password is set in credentials.env (ADMIN_PASSWORD).")
        table = dict(ACCOUNT_TABLES).get(role)
        if table is None:
            raise InvalidInputError("Account not found.")
        rows = self._fetch(
            sql.SQL("SELECT password_hash FROM {table} WHERE username = %s").format(table=sql.Identifier(table)),
            (username,),
        )
        if not rows or not self._verify_password(current_password, rows[0][0]):
            raise InvalidInputError("Your current password is incorrect.")
        if not new_password:
            raise InvalidInputError("New password is required.")
        if new_password == current_password:
            raise InvalidInputError("Your new password must be different from the current one.")
        validate_password(new_password)
        self._execute(
            sql.SQL("UPDATE {table} SET password_hash = %s WHERE username = %s").format(table=sql.Identifier(table)),
            (self._hash_password(new_password), username),
        )

    def get_doctor_specializations(self, username: str) -> list[str]:
        rows = self._fetch(
            "SELECT specialization FROM doctor_specializations WHERE username = %s ORDER BY specialization",
            (username,)
        )
        return [row[0] for row in rows]

    def _set_default_schedule(self, username: str):
        for weekday in DEFAULT_WORK_DAYS:
            self._execute(
                "INSERT INTO doctor_schedules (username, weekday, start_time, end_time) VALUES (%s, %s, %s, %s)",
                (username, weekday, DEFAULT_START, DEFAULT_END)
            )

    def get_schedule(self, username: str) -> dict[int, tuple[time, time]]:
        rows = self._fetch(
            "SELECT weekday, start_time, end_time FROM doctor_schedules WHERE username = %s",
            (username,)
        )
        return {row[0]: (row[1], row[2]) for row in rows}

    def set_schedule(self, username: str, schedule: dict[int, tuple[str, str]]):
        parsed: dict[int, tuple[time, time]] = {}
        for weekday, (start_text, end_text) in schedule.items():
            try:
                start = datetime.strptime(start_text, "%H:%M").time()
                end = datetime.strptime(end_text, "%H:%M").time()
            except ValueError:
                raise InvalidInputError("Please choose valid start and end times.")
            if end <= start:
                raise InvalidInputError(f"{DAY_NAMES[weekday]}: the end time must be after the start time.")
            parsed[weekday] = (start, end)
        self._ensure_connection()
        with self._conn.transaction():
            self._execute("DELETE FROM doctor_schedules WHERE username = %s", (username,))
            for weekday, (start, end) in parsed.items():
                self._execute(
                    "INSERT INTO doctor_schedules (username, weekday, start_time, end_time) VALUES (%s, %s, %s, %s)",
                    (username, weekday, start, end)
                )

    def get_blocked_dates(self, username: str) -> set[date]:
        rows = self._fetch(
            "SELECT blocked_date FROM doctor_blocked_dates WHERE username = %s AND blocked_date >= %s",
            (username, date.today())
        )
        return {row[0] for row in rows}

    def block_date(self, username: str, day: date):
        if day < date.today():
            raise InvalidInputError("Past dates cannot be blocked.")
        rows = self._fetch("SELECT name FROM doctors WHERE username = %s", (username,))
        if not rows:
            raise KeyError(f"Doctor '{username}' not found.")
        count = self._fetch(
            "SELECT COUNT(*) FROM appointments WHERE doctor = %s AND datetime_str LIKE %s AND status <> 'cancelled'",
            (rows[0][0], day.isoformat() + " %")
        )[0][0]
        if count:
            raise InvalidInputError(f"You already have {count} appointment(s) on this date, so it cannot be blocked.")
        self._execute(
            "INSERT INTO doctor_blocked_dates (username, blocked_date) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (username, day)
        )

    def unblock_date(self, username: str, day: date):
        self._execute(
            "DELETE FROM doctor_blocked_dates WHERE username = %s AND blocked_date = %s",
            (username, day)
        )

    def _doctors_for(self, service: str | None, doctor: str | None) -> list:
        if doctor and service:
            return self._fetch(
                "SELECT d.username, d.name FROM doctors d JOIN doctor_specializations s ON s.username = d.username "
                "WHERE d.name = %s AND s.specialization = %s",
                (doctor, service)
            )
        if doctor:
            return self._fetch("SELECT username, name FROM doctors WHERE name = %s", (doctor,))
        if service:
            return self._fetch(
                "SELECT d.username, d.name FROM doctors d JOIN doctor_specializations s ON s.username = d.username "
                "WHERE s.specialization = %s ORDER BY d.name",
                (service,)
            )
        return self._fetch("SELECT username, name FROM doctors ORDER BY name")

    def _load_doctor_month(self, username: str, name: str, year: int, month: int, exclude_id: str | None):
        schedule = self.get_schedule(username)
        blocked = self.get_blocked_dates(username)
        booked = {
            row[0] for row in self._fetch(
                "SELECT datetime_str FROM appointments WHERE doctor = %s AND datetime_str LIKE %s AND id <> %s AND status <> 'cancelled'",
                (name, f"{year:04d}-{month:02d}-%", exclude_id or "")
            )
        }
        return schedule, blocked, booked

    @staticmethod
    def _day_slots(schedule: dict, blocked: set, booked: set, day: date) -> list[str]:
        if day < date.today() or day in blocked or day.weekday() not in schedule:
            return []
        start, end = schedule[day.weekday()]
        now = datetime.now()
        step = timedelta(minutes=SLOT_MINUTES)
        current = datetime.combine(day, start)
        finish = datetime.combine(day, end)
        slots = []
        while current + step <= finish:
            label = current.strftime("%H:%M")
            if current > now and f"{day.isoformat()} {label}" not in booked:
                slots.append(label)
            current += step
        return slots

    def get_available_dates(self, service: str | None, doctor: str | None, year: int, month: int, exclude_id: str | None = None) -> set[str]:
        available: set[str] = set()
        last_day = calendar.monthrange(year, month)[1]
        for username, name in self._doctors_for(service, doctor):
            schedule, blocked, booked = self._load_doctor_month(username, name, year, month, exclude_id)
            for number in range(1, last_day + 1):
                day = date(year, month, number)
                if day.isoformat() not in available and self._day_slots(schedule, blocked, booked, day):
                    available.add(day.isoformat())
        return available

    def get_available_slots(self, service: str | None, doctor: str | None, day: date, exclude_id: str | None = None) -> list[str]:
        slots: set[str] = set()
        for username, name in self._doctors_for(service, doctor):
            schedule, blocked, booked = self._load_doctor_month(username, name, day.year, day.month, exclude_id)
            slots.update(self._day_slots(schedule, blocked, booked, day))
        return sorted(slots)

    def assign_doctor(self, service: str, day: date, slot: str, exclude_id: str | None = None) -> str | None:
        best: tuple[int, str] | None = None
        for username, name in self._doctors_for(service, None):
            schedule, blocked, booked = self._load_doctor_month(username, name, day.year, day.month, exclude_id)
            if slot in self._day_slots(schedule, blocked, booked, day):
                workload = sum(1 for value in booked if value.startswith(day.isoformat()))
                if best is None or workload < best[0]:
                    best = (workload, name)
        return best[1] if best else None

    @staticmethod
    def _parse_datetime(datetime_str: str) -> tuple[date, str]:
        try:
            moment = datetime.strptime(datetime_str, DATETIME_FORMAT)
        except ValueError:
            raise InvalidInputError("Please pick a date and time from the calendar.")
        return moment.date(), moment.strftime("%H:%M")

    def _check_doctor_slot(self, service: str, doctor: str, day: date, slot: str, exclude_id: str | None = None):
        if not self._doctors_for(service, doctor):
            raise InvalidInputError(f"{doctor} does not handle {service}.")
        if slot not in self.get_available_slots(service, doctor, day, exclude_id):
            raise InvalidInputError(f"{doctor} is not available at the selected time.")

    def _next_appointment_id(self) -> str:
        rows = self._fetch("SELECT nextval('appointment_id_seq')")
        return f"APT-{rows[0][0]:04d}"

    def find_patient_username(self, name: str) -> str | None:
        """Username of the patient with this exact name, but only if exactly one patient has it."""
        rows = self._fetch("SELECT username FROM patients WHERE LOWER(name) = LOWER(%s)", (name.strip(),))
        return rows[0][0] if len(rows) == 1 else None

    def get_patient(self, username: str) -> tuple | None:
        """(username, name, account_no) of a patient account, or None."""
        rows = self._fetch("SELECT username, name, account_no FROM patients WHERE username = %s", (username,))
        return rows[0] if rows else None

    def list_patients(self) -> list[tuple]:
        """(username, name, account_no) of every patient, for the searchable patient dropdown."""
        return self._fetch("SELECT username, name, account_no FROM patients ORDER BY name")

    def search_patients(self, query: str, limit: int = 20) -> list[tuple]:
        """Patients matching a name, username or account ID (e.g. 000-004). An empty query lists the first patients."""
        query = (query or "").strip()
        if not query:
            return self._fetch("SELECT username, name, account_no FROM patients ORDER BY name LIMIT %s", (limit,))
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params: list = [like, like]
        condition = "name ILIKE %s OR username ILIKE %s"
        if query.replace("-", "").isdigit() and len(query.replace("-", "")) <= 6:
            condition += " OR account_no = %s"
            params.append(int(query.replace("-", "")))
        params.append(limit)
        return self._fetch(f"SELECT username, name, account_no FROM patients WHERE {condition} ORDER BY name LIMIT %s", tuple(params))

    def get_patient_email(self, username: str | None) -> str | None:
        if not username:
            return None
        rows = self._fetch("SELECT email FROM patients WHERE username = %s", (username,))
        return rows[0][0] if rows and rows[0][0] else None

    def create_appointment(self, patient_name: str, service: str, datetime_str: str, doctor: str | None = None,
                           reason: str = "", patient_username: str | None = None) -> Appointment:
        day, slot = self._parse_datetime(datetime_str)
        if patient_username is None:
            patient_username = self.find_patient_username(patient_name)
        if patient_username and self._fetch(
            "SELECT 1 FROM appointments WHERE patient_username = %s AND datetime_str = %s AND status = 'scheduled'",
            (patient_username, datetime_str),
        ):
            raise InvalidInputError("This patient already has an appointment at that time.")
        if doctor is None:
            doctor = self.assign_doctor(service, day, slot)
            if doctor is None:
                raise InvalidInputError("No doctor is available for that service at the selected time.")
        else:
            self._check_doctor_slot(service, doctor, day, slot)
        appointment = Appointment(self._next_appointment_id(), patient_name, doctor, service, datetime_str,
                                  "scheduled", clean_reason(reason), patient_username)
        self.add_record(appointment)
        return appointment

    def reschedule_appointment(self, record_id: str, patient_name: str, service: str, datetime_str: str,
                               doctor: str | None = None, reason: str | None = None):
        if record_id not in self._database:
            raise KeyError(f"Appointment ID '{record_id}' not found.")
        current = self._database[record_id]
        if current.status != "scheduled":
            raise InvalidInputError("Only scheduled appointments can be changed.")
        day, slot = self._parse_datetime(datetime_str)
        if doctor is None:
            doctor = self.assign_doctor(service, day, slot, record_id)
            if doctor is None:
                raise InvalidInputError("No doctor is available for that service at the selected time.")
        elif not (doctor == current.doctor and service == current.service and datetime_str == current.datetime_str):
            self._check_doctor_slot(service, doctor, day, slot, record_id)
        self.update_record(record_id, patient_name, doctor, service, datetime_str, reason)

    def set_status(self, record_id: str, status: str):
        if status not in STATUSES:
            raise InvalidInputError("Unknown appointment status.")
        if record_id not in self._database:
            raise KeyError(f"Appointment ID '{record_id}' not found.")
        appointment = self._database[record_id]
        if appointment.status != "scheduled":
            raise InvalidInputError("Only scheduled appointments can be updated.")
        self._execute("UPDATE appointments SET status = %s WHERE id = %s", (status, record_id))
        appointment.status = status

    def get_record(self, record_id: str) -> Appointment | None:
        return self._database.get(record_id)

    def save_to_file(self):
        self._conn.commit()

    def load_from_file(self):
        # ENCAPSULATION: the in-memory data is private to DataManager.
        # Big O: loading every row into the dict is O(n) time and O(n) memory.
        self._database = {}
        rows = self._fetch("SELECT id, patient_name, doctor, service, datetime_str, status, reason, patient_username FROM appointments")
        for row in rows:
            self._database[row[0]] = Appointment(*row)

    def add_record(self, record: Appointment):
        # POLYMORPHISM: works with any kind of Record, not just Appointment.
        if not isinstance(record, Record):
            raise TypeError("Only Record instances can be added.")
        # Big O: the duplicate check is a dict lookup, O(1) on average (the SQL insert depends on Postgres).
        if record.record_id in self._database:
            raise DuplicateRecordError(f"Appointment ID '{record.record_id}' already exists.")
        try:
            self._execute(
                "INSERT INTO appointments (id, patient_name, doctor, service, datetime_str, status, reason, patient_username) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (record.record_id, record.patient_name, record.doctor, record.service, record.datetime_str,
                 record.status, record.reason, record.patient_username)
            )
        except psycopg.IntegrityError as error:
            if error.diag.constraint_name == "appointments_active_slot_key":
                raise InvalidInputError("That time slot was just taken. Please pick another one.")
            raise DuplicateRecordError(f"Appointment ID '{record.record_id}' already exists.")
        self._database[record.record_id] = record

    def update_record(self, record_id: str, patient_name: str, doctor: str, service: str, datetime_str: str,
                      reason: str | None = None):
        if record_id not in self._database:
            raise KeyError(f"Appointment ID '{record_id}' not found.")
        if not datetime_str.strip():
            raise InvalidInputError("Date and Time cannot be empty.")
        appointment = self._database[record_id]
        new_reason = appointment.reason if reason is None else clean_reason(reason)
        new_username = appointment.patient_username
        if patient_name.strip().lower() != appointment.patient_name.strip().lower():
            new_username = self.find_patient_username(patient_name)
        try:
            self._execute(
                "UPDATE appointments SET patient_name = %s, doctor = %s, service = %s, datetime_str = %s, reason = %s, "
                "patient_username = %s WHERE id = %s",
                (patient_name, doctor, service, datetime_str, new_reason, new_username, record_id)
            )
        except psycopg.IntegrityError:
            self.load_from_file()
            raise InvalidInputError("That time slot was just taken. Please pick another one.")
        appointment.patient_name = patient_name
        appointment.doctor = doctor
        appointment.service = service
        appointment.datetime_str = datetime_str
        appointment.reason = new_reason
        appointment.patient_username = new_username

    def delete_record(self, record_id: str):
        # Big O: dict lookup and delete are O(1) on average.
        if record_id in self._database:
            del self._database[record_id]
            self._execute("DELETE FROM appointments WHERE id = %s", (record_id,))
        else:
            raise KeyError(f"Appointment ID '{record_id}' not found.")

    def search_records(self, keyword: str, patient_filter: str | None = None) -> list[Appointment]:
        # Big O: linear search, O(n) - checks every appointment once (best case Omega(1), worst case O(n)).
        keyword = keyword.lower()
        results = []
        for appt in self._database.values():
            if patient_filter and appt.patient_name.lower() != patient_filter.lower():
                continue
            if (keyword in appt.patient_name.lower() or 
                keyword in appt.doctor.lower() or 
                keyword in appt.service.lower() or
                keyword in appt.record_id.lower()):
                results.append(appt)
        return results

    def get_sorted_records(self, sort_by: str = "id", reverse: bool = False, patient_filter: str | None = None,
                           doctor_filter: str | None = None, patient_username: str | None = None) -> list[Appointment]:
        # Big O: copying and filtering the list is O(n); sorted() (Timsort) is O(n log n), so the total is O(n log n).
        records = list(self._database.values())
        if patient_username:
            records = [r for r in records if r.patient_username == patient_username]
        if patient_filter:
            records = [r for r in records if r.patient_name.lower() == patient_filter.lower()]
        if doctor_filter:
            records = [r for r in records if r.doctor.lower() == doctor_filter.lower()]
        
        if sort_by == "patient name":
            return sorted(records, key=lambda x: x.patient_name.lower(), reverse=reverse)
        elif sort_by == "doctor":
            return sorted(records, key=lambda x: x.doctor.lower(), reverse=reverse)
        elif sort_by == "service":
            return sorted(records, key=lambda x: x.service.lower(), reverse=reverse)
        elif sort_by == "date/time":
            return sorted(records, key=lambda x: x.datetime_str, reverse=reverse)
        else:
            return sorted(records, key=lambda x: x.record_id, reverse=reverse)