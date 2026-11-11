import os
import smtplib
import ssl
from email.message import EmailMessage


# INHERITANCE: custom error inherits from Exception.
class MailError(Exception):
    pass


def _deliver(to_email: str, subject: str, body: str, dev_note: str):
    """Send one plain-text email using the SMTP_* settings in credentials.env.

    With no SMTP_HOST configured nothing is sent: `dev_note` is printed to the console instead.
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        print(f"[DEV MODE] SMTP is not configured. {dev_note}")
        return
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
    except ValueError:
        raise MailError("SMTP_PORT must be a number.")
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    if not sender:
        raise MailError("Set SMTP_FROM or SMTP_USER in credentials.env.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls(context=context)
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"{type(e).__name__}: {e}") from e


def send_verification_email(to_email: str, code: str, minutes: int):
    _deliver(
        to_email,
        "Your Clinic Appointment System verification code",
        f"Your verification code is: {code}\n\n"
        f"It expires in {minutes} minutes. If you didn't try to register, you can ignore this email.",
        f"Verification code for {to_email}: {code}",
    )


def send_password_reset_email(to_email: str, code: str, minutes: int):
    _deliver(
        to_email,
        "Your Clinic Appointment System password reset code",
        f"Your password reset code is: {code}\n\n"
        f"It expires in {minutes} minutes. If you didn't request a password reset, you can ignore this email.",
        f"Password reset code for {to_email}: {code}",
    )


def send_appointment_email(to_email: str, subject: str, details: list[tuple[str, str]], intro: str):
    """Appointment confirmation / change / cancellation notice. `details` is a list of (label, value) rows."""
    lines = [intro, ""]
    lines += [f"{label}: {value}" for label, value in details if value]
    lines += ["", "Clinic Appointment System"]
    _deliver(
        to_email,
        f"Clinic Appointment System: {subject}",
        "\n".join(lines),
        f"Email to {to_email} - {subject}: " + "; ".join(f"{label}: {value}" for label, value in details if value),
    )
