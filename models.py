import re
from abc import ABC, abstractmethod

SERVICES = ["General Checkup", "Dental", "Pediatrics", "Cardiology"]
STATUSES = ("scheduled", "completed", "cancelled", "no_show")
REASON_MAX_LENGTH = 300

def clean_reason(value: str | None) -> str:
    """Optional free-text reason for a visit: collapse whitespace and cap the length."""
    return " ".join((value or "").split())[:REASON_MAX_LENGTH]
NAME_CHARS = " .'-"

class InvalidInputError(Exception):
    pass

class DuplicateRecordError(Exception):
    pass

def validate_name_part(value: str, label: str, required: bool = True, extra_chars: str = "") -> str:
    value = " ".join(value.split())
    if not value:
        if required:
            raise InvalidInputError(f"{label} is required.")
        return ""
    allowed = NAME_CHARS + extra_chars
    if not value[0].isalpha() or not all(ch.isalpha() or ch in allowed for ch in value):
        raise InvalidInputError(f"{label} can only contain letters. Numbers and symbols are not allowed.")
    return value

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")

def validate_email(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        raise InvalidInputError("Email is required.")
    if len(value) > 254 or not EMAIL_RE.match(value):
        raise InvalidInputError("Please enter a valid email address.")
    return value

PASSWORD_MAX_LENGTH = 12
MAX_ACCOUNTS_PER_EMAIL = 3

def validate_password(value: str) -> str:
    """Passwords: at most PASSWORD_MAX_LENGTH characters and at least one number."""
    if not value:
        raise InvalidInputError("Password is required.")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise InvalidInputError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters long.")
    if not any(ch.isdigit() for ch in value):
        raise InvalidInputError("Password must contain at least one number.")
    return value

AVATAR_MAX_BYTES = 2 * 1024 * 1024

def validate_avatar(data: bytes) -> str:
    """Check an uploaded picture by its real contents (not the filename) and return its MIME type."""
    if not data:
        raise InvalidInputError("Please choose an image to upload.")
    if len(data) > AVATAR_MAX_BYTES:
        raise InvalidInputError("That image is too large. The limit is 2 MB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise InvalidInputError("Please upload a PNG, JPG, GIF or WebP image.")

def validate_full_name(value: str, label: str = "Name") -> str:
    return validate_name_part(value, label, extra_chars=",")

def build_full_name(last: str, first: str, middle: str, suffix: str = "") -> str:
    last = validate_name_part(last, "Last name")
    first = validate_name_part(first, "First name")
    middle = validate_name_part(middle, "Middle name")
    suffix = validate_name_part(suffix, "Suffix", required=False)
    full_name = f"{last}, {first} {middle}"
    return f"{full_name} {suffix}" if suffix else full_name

# ABSTRACTION: Record is an abstract class; it cannot be created directly.
class Record(ABC):
    def __init__(self, record_id: str, patient_name: str):
        # ENCAPSULATION: data is kept private (leading underscore).
        self._record_id = record_id
        self._patient_name = patient_name

    # ENCAPSULATION: read-only access through a property.
    @property
    def record_id(self) -> str:
        return self._record_id

    @property
    def patient_name(self) -> str:
        return self._patient_name

    # ENCAPSULATION: setter validates the value before changing it.
    @patient_name.setter
    def patient_name(self, value: str):
        if not value.strip():
            raise InvalidInputError("Patient name cannot be empty.")
        self._patient_name = value

    # ABSTRACTION: subclasses must provide their own to_dict().
    @abstractmethod
    def to_dict(self) -> dict:
        pass

# INHERITANCE: Appointment inherits from Record.
class Appointment(Record):
    def __init__(self, record_id: str, patient_name: str, doctor: str, service: str, datetime_str: str,
                 status: str = "scheduled", reason: str = "", patient_username: str | None = None):
        # INHERITANCE: reuse the parent's constructor.
        super().__init__(record_id, patient_name)
        self.doctor = doctor
        self.service = service
        self.datetime_str = datetime_str
        self.status = status
        self.reason = reason or ""
        self.patient_username = patient_username

    # POLYMORPHISM: overrides Record.to_dict() with its own version.
    def to_dict(self) -> dict:
        return {
            "id": self.record_id,
            "patient_name": self.patient_name,
            "doctor": self.doctor,
            "service": self.service,
            "datetime_str": self.datetime_str,
            "status": self.status,
            "reason": self.reason,
            "patient_username": self.patient_username,
        }
