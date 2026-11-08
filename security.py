"""Login attempt limiting and a persistent SECRET_KEY."""
import os
import secrets
import threading
import time

MAX_FAILED_ATTEMPTS = 5      # wrong passwords allowed for one account before it is locked
MAX_FAILED_PER_IP = 20       # wrong passwords allowed from one address (higher, since a school/home network shares one)
LOCKOUT_SECONDS = 5 * 60     # how long the lock lasts
WINDOW_SECONDS = 15 * 60     # failures older than this are forgotten


class LoginLimiter:
    """Counts failed logins per account and per IP address (kept in memory, thread-safe)."""

    def __init__(self, max_account=MAX_FAILED_ATTEMPTS, max_ip=MAX_FAILED_PER_IP,
                 lockout=LOCKOUT_SECONDS, window=WINDOW_SECONDS, clock=time.time):
        self.max_account, self.max_ip = max_account, max_ip
        self.lockout, self.window, self.clock = lockout, window, clock
        self._lock = threading.Lock()
        self._state = {}  # key -> [failure_count, first_failure_time, locked_until]

    def _limit(self, key):
        return self.max_ip if key.startswith("ip:") else self.max_account

    def _prune(self, now):
        stale = [k for k, (n, first, until) in self._state.items() if until <= now and now - first > self.window]
        for k in stale:
            del self._state[k]

    def seconds_locked(self, *keys) -> int:
        """Seconds left on the longest active lock among `keys` (0 = not locked)."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            return max([int(self._state[k][2] - now) + 1 for k in keys if k in self._state and self._state[k][2] > now] or [0])

    def record_failure(self, *keys):
        now = self.clock()
        with self._lock:
            self._prune(now)
            for k in keys:
                n, first, until = self._state.get(k, [0, now, 0])
                if until and until <= now:            # an earlier lock has expired: start fresh
                    n, first, until = 0, now, 0
                n += 1
                if n >= self._limit(k):
                    until = now + self.lockout
                self._state[k] = [n, first, until]

    def record_success(self, *keys):
        with self._lock:
            for k in keys:
                self._state.pop(k, None)


def load_secret_key(env_path: str) -> str:
    """SECRET_KEY from the environment / credentials.env. If none is set, create one and save it to
    credentials.env so that sessions (logins) survive a server restart."""
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    key = secrets.token_hex(32)
    try:
        existing = open(env_path, "rb").read() if os.path.exists(env_path) else b""
        needs_newline = bool(existing) and not existing.endswith(b"\n")
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(("\n" if needs_newline else "") + f"SECRET_KEY={key}\n")
        print(f"[security] Created a SECRET_KEY and saved it to {env_path}")
    except OSError as e:
        print(f"[security] WARNING: could not save SECRET_KEY ({e}). Logins will reset when the server restarts.")
    return key
