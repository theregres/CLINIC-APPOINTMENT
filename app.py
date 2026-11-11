import calendar
import os
import secrets
import threading
from datetime import date, datetime
from functools import wraps
from typing import Any
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from database import DataManager, DAY_NAMES, CODE_TTL_MINUTES
from icons import icon, service_icon, service_class
from mailer import send_verification_email, send_password_reset_email, send_appointment_email, MailError
import backup
from security import LoginLimiter, load_secret_key
from models import (
    SERVICES, AVATAR_MAX_BYTES, InvalidInputError, DuplicateRecordError,
    build_full_name, validate_full_name, validate_email, validate_avatar, clean_reason,
)

app = Flask(__name__)
app.secret_key = load_secret_key(os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.env"))
login_limiter = LoginLimiter()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=AVATAR_MAX_BYTES + 512 * 1024,  # headroom for the multipart envelope
)

manager = DataManager()

TIME_CHOICES = [f"{hour:02d}:{minute:02d}" for hour in range(6, 22) for minute in (0, 30)]
HOME_ENDPOINTS = {"admin": "admin_dashboard", "patient": "patient_dashboard", "doctor": "doctor_dashboard"}
SORT_KEYS = {"id": "id", "patient": "patient name", "doctor": "doctor", "service": "service", "datetime": "date/time"}


app.jinja_env.globals.update(icon=icon, service_icon=service_icon, service_class=service_class)


def doctor_avatar_url(name):
    """URL of a doctor's profile picture (by display name), or None if they have not uploaded one."""
    if "doctor_avatars" not in g:
        g.doctor_avatars = manager.get_doctor_avatar_map()
    username, version = g.doctor_avatars.get((name or "").strip(), (None, None))
    return url_for("avatar", username=username, v=version) if username and version else None


app.jinja_env.globals["doctor_avatar_url"] = doctor_avatar_url


def format_account_no(number):
    """1234 -> '001-234'. Empty when the account has no number yet."""
    if not number:
        return ""
    return f"{number // 1000:03d}-{number % 1000:03d}"


def account_id_for(role, name):
    """Formatted number of the doctor or patient with this display name ('' if unknown or ambiguous)."""
    if "account_numbers" not in g:
        g.account_numbers = manager.get_account_number_map()
    key = (name or "").strip()
    return format_account_no(g.account_numbers[role].get(key if role == "doctor" else key.lower()))


PER_PAGE = 10


def paginate(items):
    """The slice of `items` for the page in ?page=N (10 per page), plus what the pager needs."""
    # Big O: len() is O(1) and slicing a page of 10 is O(1) (a fixed size), so paging adds almost no cost.
    total = len(items)
    pages = max(1, -(-total // PER_PAGE))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)
    start = (page - 1) * PER_PAGE
    info = {"page": page, "pages": pages, "total": total, "first": start + 1 if total else 0, "last": min(start + PER_PAGE, total)}
    return items[start:start + PER_PAGE], info


def page_url(number):
    """Link to another page of the current list, keeping the search and sort."""
    args = {key: value for key, value in request.args.items() if key != "page"}
    if number > 1:
        args["page"] = number
    return url_for(request.endpoint, **args)


def page_window(page, pages):
    """Page numbers to show: the first, the last, and the ones around the current page (None = a gap)."""
    # Big O: only about 5 numbers are sorted, so this is constant time, O(1).
    wanted = sorted({1, pages, page - 1, page, page + 1} & set(range(1, pages + 1)))
    shown, previous = [], 0
    for number in wanted:
        if previous and number - previous > 1:
            shown.append(None)
        shown.append(number)
        previous = number
    return shown


app.jinja_env.globals["page_url"] = page_url
app.jinja_env.globals["page_window"] = page_window


def patient_options():
    """Patients prepared for the searchable dropdown (see templates/_combobox.html)."""
    return [{"u": username, "n": name, "id": format_account_no(no)} for username, name, no in manager.list_patients()]


app.jinja_env.filters["acct_no"] = format_account_no
app.jinja_env.globals["account_id_for"] = account_id_for


def appt_when(record):
    try:
        return datetime.strptime(record.datetime_str, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def appt_future(record):
    when = appt_when(record)
    return when is not None and when > datetime.now()


def appt_started(record):
    when = appt_when(record)
    return when is not None and when <= datetime.now()


def can_cancel(record):
    """Patients and doctors can cancel (and patients reschedule) a scheduled appointment that has not started."""
    return record.status == "scheduled" and appt_future(record)


def can_mark_done(record):
    """Completed / no-show can be recorded once the appointment time has arrived."""
    return record.status == "scheduled" and appt_started(record)


def status_info(record):
    """(css class, label, icon) for an appointment's status chip."""
    if record.status == "completed":
        return "completed", "Completed", "check-circle"
    if record.status == "cancelled":
        return "cancelled", "Cancelled", "x-circle"
    if record.status == "no_show":
        return "no-show", "No-show", "user-x"
    return ("upcoming", "Upcoming", "clock") if appt_future(record) else ("past", "Past", "check")


app.jinja_env.globals.update(
    appt_future=appt_future, appt_started=appt_started, can_cancel=can_cancel,
    can_mark_done=can_mark_done, status_info=status_info,
)


def push_notifications(record, patient_msg=None, doctor_msg=None, admin_msg=None):
    """In-app notifications (the bell in the top bar) for the patient, the doctor and/or the administrators."""
    try:
        if admin_msg:
            manager.notify_admins(admin_msg)
        if patient_msg:
            manager.add_notification(record.patient_username, patient_msg)
        if doctor_msg:
            manager.add_notification(manager.get_doctor_username(record.doctor), doctor_msg)
    except Exception as e:  # a notification problem must never break booking or cancelling
        app.logger.warning("Could not save notification: %s", e)


def push_done_notifications(record, status, by_admin):
    """Tell the patient (and the doctor, when the clinic did it) that a visit was completed or missed."""
    if status == "completed":
        patient_msg = f"Your {appt_text(record)} was marked as completed. Thank you for visiting."
        doctor_msg = f"The clinic marked the {appt_text(record)} with {record.patient_name} as completed."
    else:
        patient_msg = f"Your {appt_text(record)} was marked as a no-show. You can book a new appointment if you still need to be seen."
        doctor_msg = f"The clinic marked the {appt_text(record)} with {record.patient_name} as a no-show."
    push_notifications(
        record, patient_msg, doctor_msg if by_admin else None,
        None if by_admin or status == "completed" else f"{record.patient_name} did not show up for the {appt_text(record)} with {doctor_label(record.doctor)}.",
    )


def appt_text(record):
    return f"{record.service} appointment {record.record_id} on {pretty_datetime(record.datetime_str)}"


def notify_patient(record, intro, subject):
    """Email the patient about an appointment (if the booking is linked to an account with an email address)."""
    to_email = manager.get_patient_email(record.patient_username)
    if not to_email:
        return
    details = [
        ("Appointment", record.record_id),
        ("Doctor", doctor_label(record.doctor)),
        ("Service", record.service),
        ("When", pretty_datetime(record.datetime_str)),
        ("Reason", record.reason),
    ]

    def send():
        try:
            send_appointment_email(to_email, subject, details, intro)
        except MailError as e:
            app.logger.warning("Could not send appointment email to %s: %s", to_email, e)

    if app.config.get("EMAIL_ASYNC", True):
        threading.Thread(target=send, daemon=True).start()  # keep slow mail servers from delaying the page
    else:
        send()


@app.template_filter("time12")
def time12(value):
    try:
        return datetime.strptime(value, "%H:%M").strftime("%I:%M %p")
    except (ValueError, TypeError):
        return value


@app.template_filter("doctor_label")
def doctor_label(value):
    """Display-only: prefix a doctor's name with 'Dr.' (stored names are unchanged)."""
    value = (value or "").strip()
    if not value or value.lower().startswith("dr."):
        return value
    return f"Dr. {value}"


@app.template_filter("pretty_date")
def pretty_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%a, %b %d, %Y")
    except (ValueError, TypeError):
        return value


@app.template_filter("pretty_md")
def pretty_md(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%b %d")
    except (ValueError, TypeError):
        return value


@app.template_filter("appt_status")
def appt_status(value):
    try:
        return "upcoming" if datetime.strptime(value, "%Y-%m-%d %H:%M") >= datetime.now() else "past"
    except (ValueError, TypeError):
        return "past"


@app.template_filter("pretty_datetime")
def pretty_datetime(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").strftime("%b %d, %Y  %I:%M %p")
    except (ValueError, TypeError):
        return value


@app.before_request
def prepare_request():
    if request.endpoint == "static":
        return
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400)
    if session.get("role"):
        manager.load_from_file()


@app.context_processor
def inject_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    pending = len(manager.get_doctor_requests()) if session.get("role") == "admin" else 0
    unread = 0
    if session.get("role"):
        try:
            unread = manager.count_unread_notifications(session["username"])
        except Exception:
            unread = 0
    my_id = ""
    if session.get("role") and session.get("username"):
        try:
            my_id = format_account_no(manager.get_account_no(session["role"], session["username"]))
        except Exception:
            pass
    return {"csrf_token": session["csrf_token"], "pending_count": pending, "unread_count": unread, "my_id": my_id}


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", message="Your session expired or the form was invalid. Please go back and try again."), 400


@app.errorhandler(413)
def too_large(error):
    flash("That image is too large. The limit is 2 MB.", "error")
    return redirect(url_for("profile") if session.get("role") else url_for("login"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("role"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def build_calendar(year, month, state_for_day, url_for_day=None, url_for_month=None):
    today = date.today()
    weeks = []
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(year, month):
        row = []
        for day in week:
            if day.month != month:
                row.append(None)
                continue
            state = state_for_day(day)
            clickable = state not in ("past", "unavailable")
            row.append({
                "day": day.day,
                "iso": day.isoformat(),
                "state": state,
                "clickable": clickable,
                "url": url_for_day(day.isoformat()) if (url_for_day and clickable) else None,
            })
        weeks.append(row)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    can_prev = (year, month) > (today.year, today.month)
    return {
        "year": year,
        "month": month,
        "label": date(year, month, 1).strftime("%B %Y"),
        "weeks": weeks,
        "prev_url": url_for_month(prev_year, prev_month) if (url_for_month and can_prev) else None,
        "next_url": url_for_month(next_year, next_month) if url_for_month else None,
    }


def month_from_request():
    today = date.today()
    year = request.args.get("year", today.year, type=int)
    month = request.args.get("month", today.month, type=int)
    if year is None or month is None or not 1 <= month <= 12 or (year, month) < (today.year, today.month) or year > today.year + 5:
        return today.year, today.month
    return year, month


def booking_calendar(endpoint, params, service, doctor, exclude_id=None):
    year, month = month_from_request()
    selected = request.args.get("date", "")
    available = manager.get_available_dates(service, doctor, year, month, exclude_id)
    if selected not in available:
        selected = ""
    slots = manager.get_available_slots(service, doctor, date.fromisoformat(selected), exclude_id) if selected else []

    def state_for_day(day):
        iso = day.isoformat()
        if iso == selected:
            return "selected"
        return "available" if iso in available else "unavailable"

    cal = build_calendar(
        year, month, state_for_day,
        lambda iso: url_for(endpoint, **params, year=year, month=month, date=iso),
        lambda y, m: url_for(endpoint, **params, year=y, month=m),
    )
    return cal, slots, selected


def read_name_form():
    return build_full_name(
        request.form.get("last", ""),
        request.form.get("first", ""),
        request.form.get("middle", ""),
        request.form.get("suffix", ""),
    )


def read_account_form():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    confirm = request.form.get("confirm_password", "").strip()
    email = validate_email(request.form.get("email", ""))
    if not username or not password:
        raise InvalidInputError("All fields are required.")
    if password != confirm:
        raise InvalidInputError("Passwords do not match.")
    return username, email, password


def begin_verification(kind, username, password, name, email, specializations=None):
    code = manager.start_registration(kind, username, password, name, email, specializations)
    try:
        send_verification_email(email, code, CODE_TTL_MINUTES)
    except MailError as e:
        app.logger.error("Could not send verification email: %s", e)
        manager.cancel_pending(email)
        raise InvalidInputError("We couldn't send the verification email. Check the address and try again.")
    session["pending_email"] = email
    flash(f"We sent a 6-digit code to {email}. It expires in {CODE_TTL_MINUTES} minutes.", "info")
    return redirect(url_for("verify"))


@app.route("/")
def index():
    role = session.get("role")
    if role in HOME_ENDPOINTS:
        return redirect(url_for(HOME_ENDPOINTS[role]))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()  # may be a username or an email
        password = request.form.get("password", "").strip()
        keys = (f"user:{username.lower()}", f"ip:{request.remote_addr}")
        wait = login_limiter.seconds_locked(*keys)
        if wait:
            flash(f"Too many failed login attempts. Please try again in {(wait + 59) // 60} minute(s).", "error")
            return render_template("login.html"), 429
        user = manager.authenticate(username, password)
        if user:
            login_limiter.record_success(*keys)
            session.clear()
            session["role"] = user["role"]
            session["name"] = user["name"]
            session["username"] = user["username"]
            avatar_v = manager.get_avatar_version(user["username"])
            if avatar_v:
                session["avatar_v"] = avatar_v
            return redirect(url_for(HOME_ENDPOINTS[user["role"]]))
        login_limiter.record_failure(*keys)
        if manager.is_pending_doctor(username, password):
            flash("Your doctor registration is still waiting for admin approval.", "info")
        elif login_limiter.seconds_locked(*keys):
            flash("Too many failed login attempts. This account is locked for a few minutes.", "error")
        else:
            flash("Invalid username/email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile")
@login_required
def profile():
    info = manager.get_profile(session["role"], session["username"])
    if info is None:
        session.clear()
        return redirect(url_for("login"))
    return render_template("profile.html", info=info)


@app.route("/avatar", defaults={"username": None})
@app.route("/avatar/<username>")
@login_required
def avatar(username):
    # Your own picture, or a doctor's picture (shown to patients/admins in lists). Nobody else's.
    if username is None or username == session["username"]:
        username = session["username"]
    elif session.get("role") != "admin" and not manager.is_doctor(username):
        abort(404)
    picture = manager.get_avatar(username)
    if picture is None:
        abort(404)
    content_type, data = picture
    response = Response(data, mimetype=content_type)
    response.headers["Cache-Control"] = "private, max-age=86400"  # the ?v= in the URL busts this on change
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    upload = request.files.get("avatar")
    try:
        data = upload.read(AVATAR_MAX_BYTES + 1) if upload else b""
        content_type = validate_avatar(data)
        session["avatar_v"] = manager.set_avatar(session["username"], content_type, data)
        flash("Profile picture updated.", "success")
    except InvalidInputError as e:
        flash(str(e), "error")
    return redirect(url_for("profile"))


@app.route("/profile/avatar/remove", methods=["POST"])
@login_required
def remove_avatar():
    manager.delete_avatar(session["username"])
    session.pop("avatar_v", None)
    flash("Profile picture removed.", "success")
    return redirect(url_for("profile"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current = request.form.get("current_password", "").strip()
        new = request.form.get("new_password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()
        try:
            if not current or not new:
                raise InvalidInputError("All fields are required.")
            if new != confirm:
                raise InvalidInputError("New passwords do not match.")
            manager.change_password(session["role"], session["username"], current, new)
            flash("Your password has been changed.", "success")
        except InvalidInputError as e:
            flash(str(e), "error")
        return redirect(url_for("settings"))
    return render_template("settings.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        try:
            email = validate_email(email)
            code = manager.start_password_reset(email)
            send_password_reset_email(email, code, CODE_TTL_MINUTES)
            session["reset_email"] = email
            flash(f"We sent a 6-digit password reset code to {email}. It expires in {CODE_TTL_MINUTES} minutes.", "info")
            return redirect(url_for("forgot_password_verify"))
        except (InvalidInputError, MailError) as e:
            flash(str(e), "error")
    return render_template("forgot_password.html")


@app.route("/forgot-password/verify", methods=["GET", "POST"])
def forgot_password_verify():
    email = session.get("reset_email")
    if not email or not manager.has_pending_reset(email):
        session.pop("reset_email", None)
        flash("No password reset request is waiting. Please request a new code.", "error")
        return redirect(url_for("forgot_password"))
    
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        password = request.form.get("password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()
        try:
            if not code or not password:
                raise InvalidInputError("All fields are required.")
            if password != confirm:
                raise InvalidInputError("Passwords do not match.")
            manager.verify_and_reset_password(email, code, password)
            session.pop("reset_email", None)
            flash("Your password has been successfully reset. You can now log in.", "success")
            return redirect(url_for("login"))
        except InvalidInputError as e:
            flash(str(e), "error")
            if not manager.has_pending_reset(email):
                session.pop("reset_email", None)
                return redirect(url_for("login"))
    return render_template("forgot_password_verify.html", email=email, account_count=manager.count_accounts_for_email(email))


@app.route("/forgot-password/resend", methods=["POST"])
def forgot_password_resend():
    email = session.get("reset_email")
    if not email:
        return redirect(url_for("forgot_password"))
    try:
        code = manager.resend_reset_code(email)
        send_password_reset_email(email, code, CODE_TTL_MINUTES)
        flash(f"A new password reset code was sent to {email}.", "info")
    except MailError as e:
        app.logger.error("Could not send password reset email: %s", e)
        flash("We couldn't send the email. Please try again.", "error")
    except InvalidInputError as e:
        flash(str(e), "error")
    return redirect(url_for("forgot_password_verify"))


@app.route("/register", methods=["GET", "POST"])
def register():
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            name = read_name_form()
            username, email, password = read_account_form()
            return begin_verification("patient", username, password, name, email)
        except (InvalidInputError, DuplicateRecordError) as e:
            flash(str(e), "error")
    return render_template("register.html", form=form)


@app.route("/register/doctor", methods=["GET", "POST"])
def register_doctor():
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            name = read_name_form()
            username, email, password = read_account_form()
            specializations = [request.form.get("specialization1", "")]
            if request.form.get("specialization2", "None") != "None":
                specializations.append(request.form.get("specialization2", ""))
            if not specializations[0]:
                raise InvalidInputError("Please choose at least one specialization.")
            return begin_verification("doctor", username, password, name, email, specializations)
        except (InvalidInputError, DuplicateRecordError) as e:
            flash(str(e), "error")
    return render_template("register_doctor.html", form=form, services=SERVICES)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = session.get("pending_email")
    if not email or not manager.has_pending(email):
        session.pop("pending_email", None)
        flash("No registration is waiting for verification. Please register again.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        try:
            kind = manager.verify_registration(email, request.form.get("code", ""))
            session.pop("pending_email", None)
            if kind == "doctor":
                flash("Email verified! You can log in once an admin approves your account.", "success")
            else:
                flash("Email verified! You can now log in.", "success")
            return redirect(url_for("login"))
        except (InvalidInputError, DuplicateRecordError) as e:
            flash(str(e), "error")
            if not manager.has_pending(email):
                session.pop("pending_email", None)
                return redirect(url_for("login"))
    return render_template("verify.html", email=email)


@app.route("/verify/resend", methods=["POST"])
def verify_resend():
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("login"))
    try:
        code = manager.resend_code(email)
        send_verification_email(email, code, CODE_TTL_MINUTES)
        flash(f"A new code was sent to {email}.", "info")
    except MailError as e:
        app.logger.error("Could not send verification email: %s", e)
        flash("We couldn't send the verification email. Please try again.", "error")
    except InvalidInputError as e:
        flash(str(e), "error")
    return redirect(url_for("verify"))


def split_appointments(records):
    """Split (date-sorted) appointments into upcoming, past and today's. Cancelled ones are left out."""
    now = datetime.now()
    upcoming, past, today = [], [], []
    for record in records:
        if record.status == "cancelled":
            continue
        when = appt_when(record)
        if when is None:
            continue
        (upcoming if record.status == "scheduled" and when >= now else past).append(record)
        if when.date() == now.date():
            today.append(record)
    return upcoming, past, today


@app.route("/admin/dashboard")
@role_required("admin")
def admin_dashboard():
    records = manager.get_sorted_records(sort_by="date/time")
    upcoming, _, today = split_appointments(records)
    patients, doctors = manager.count_accounts()
    return render_template(
        "admin_dashboard.html", total=len(records), upcoming=upcoming[:6], upcoming_count=len(upcoming),
        today_count=len(today), patients=patients, doctors=doctors,
    )


@app.route("/patient/dashboard")
@role_required("patient")
def patient_dashboard():
    records = manager.get_sorted_records(sort_by="date/time", patient_username=session["username"])
    upcoming, past, _ = split_appointments(records)
    return render_template("patient_dashboard.html", upcoming=upcoming[:4], upcoming_count=len(upcoming), past_count=len(past))


@app.route("/doctor/dashboard")
@role_required("doctor")
def doctor_dashboard():
    records = manager.get_sorted_records(sort_by="date/time", doctor_filter=session["name"])
    upcoming, past, today = split_appointments(records)
    specializations = manager.get_doctor_specializations(session["username"])
    completed_count = sum(1 for r in records if r.status == "completed")
    return render_template(
        "doctor_dashboard.html", today=today, upcoming=upcoming[:5], upcoming_count=len(upcoming),
        completed_count=completed_count, specializations=specializations,
    )


@app.route("/admin/appointments")
@role_required("admin")
def admin_appointments():
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "id")
    direction = request.args.get("dir", "asc")
    if sort not in SORT_KEYS:
        sort = "id"
    records = manager.get_sorted_records(sort_by=SORT_KEYS[sort], reverse=(direction == "desc"))
    if q:
        matching = {record.record_id for record in manager.search_records(q)}
        records = [record for record in records if record.record_id in matching]
    records, page_info = paginate(records)
    return render_template("admin_appointments.html", records=records, q=q, sort=sort, direction=direction, page_info=page_info)


def admin_appointment_page(editing):
    rid = (request.args.get("rid") or request.form.get("rid") or "").strip() if editing else ""
    record = manager.get_record(rid) if editing else None
    if editing and record is None:
        flash("Appointment not found.", "error")
        return redirect(url_for("admin_appointments"))
    if editing and record.status != "scheduled":
        flash("Only scheduled appointments can be edited.", "error")
        return redirect(url_for("admin_appointments"))
    endpoint = "admin_edit_appointment" if editing else "admin_new_appointment"

    if request.method == "POST":
        reason = clean_reason(request.form.get("reason"))
        old_text = appt_text(record) if record is not None else ""
        old_doctor = record.doctor if record is not None else ""
        patient_name = request.form.get("patient_name", "").strip()
        service = request.form.get("service", SERVICES[0])
        doctor = request.form.get("doctor") or None
        keep_time = request.form.get("keep_time") == "1"
        datetime_str = record.datetime_str if (editing and record is not None and keep_time) else f"{request.form.get('date', '')} {request.form.get('slot', '')}"
        try:
            if not patient_name:
                raise InvalidInputError("Patient name is required.")
            validate_full_name(patient_name, "Patient name")
            if editing:
                manager.reschedule_appointment(rid, patient_name, service, datetime_str, doctor, reason)
                updated = manager.get_record(rid)
                notify_patient(updated, "Your appointment was updated.", "Appointment updated")
                push_notifications(
                    updated,
                    f"The clinic updated your {old_text}. It is now {appt_text(updated)} with {doctor_label(updated.doctor)}.",
                    f"The clinic updated {updated.patient_name}'s {old_text}. It is now {appt_text(updated)}."
                    if updated.doctor == old_doctor else
                    f"{updated.patient_name}'s {old_text} was moved to another doctor.",
                )
                if updated.doctor != old_doctor:
                    manager.add_notification(manager.get_doctor_username(updated.doctor), f"A {appt_text(updated)} with {updated.patient_name} was assigned to you.")
                flash("Appointment updated successfully!", "success")
            else:
                if not request.form.get("date") or not request.form.get("slot"):
                    raise InvalidInputError("Please pick a date and a time.")
                appointment = manager.create_appointment(patient_name, service, datetime_str, doctor, reason)
                notify_patient(appointment, "An appointment was booked for you.", "Appointment confirmed")
                push_notifications(
                    appointment,
                    f"The clinic booked a {appt_text(appointment)} for you with {doctor_label(appointment.doctor)}.",
                    f"New {appt_text(appointment)} with {appointment.patient_name}.",
                )
                flash(f"Appointment {appointment.record_id} added with {appointment.doctor}.", "success")
            return redirect(url_for("admin_appointments"))
        except (InvalidInputError, DuplicateRecordError, KeyError) as e:
            flash(str(e), "error")
        params: dict[str, Any] = {"patient_name": patient_name, "service": service, "doctor": doctor or ""}
        if editing:
            params["rid"] = rid
        return redirect(url_for(endpoint, **params))

    default_name = record.patient_name if record else ""
    default_doctor = record.doctor if record else ""
    default_service = record.service if record else SERVICES[0]
    patient_name = request.args.get("patient_name", default_name)
    service = request.args.get("service", default_service)
    if service not in SERVICES:
        service = SERVICES[0]
    doctor = request.args.get("doctor", default_doctor)
    params: dict[str, Any] = {"patient_name": patient_name, "service": service, "doctor": doctor}
    if editing:
        params["rid"] = rid
    doctors = manager.get_doctors()
    cal, slots, selected = booking_calendar(endpoint, params, service, doctor or None, rid or None)
    return render_template(
        "admin_appointment_form.html", editing=editing, rid=rid, record=record, endpoint=endpoint,
        patient_name=patient_name, service=service, doctor=doctor, doctors=doctors, services=SERVICES,
        patients=patient_options(), cal=cal, slots=slots, selected=selected, reason=record.reason if record else "",
    )


@app.route("/admin/appointments/new", methods=["GET", "POST"])
@role_required("admin")
def admin_new_appointment():
    return admin_appointment_page(editing=False)


@app.route("/admin/appointments/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_edit_appointment():
    return admin_appointment_page(editing=True)


@app.route("/admin/appointments/status", methods=["POST"])
@role_required("admin")
def admin_set_status():
    rid = request.form.get("rid", "")
    status = request.form.get("status", "")
    record = manager.get_record(rid)
    try:
        if record is None:
            raise KeyError(f"Appointment ID '{rid}' not found.")
        if status == "cancelled" and record.status == "scheduled":
            manager.set_status(rid, "cancelled")
            notify_patient(record, "Your appointment was cancelled by the clinic.", "Appointment cancelled")
            push_notifications(
                record,
                f"The clinic cancelled your {appt_text(record)}.",
                f"The clinic cancelled the {appt_text(record)} with {record.patient_name}.",
            )
            flash(f"Appointment {rid} cancelled. The time slot is free again.", "success")
        elif status in ("completed", "no_show") and can_mark_done(record):
            manager.set_status(rid, status)
            push_done_notifications(record, status, by_admin=True)
            flash(f"Appointment {rid} marked as {'completed' if status == 'completed' else 'no-show'}.", "success")
        else:
            raise InvalidInputError("That update is not allowed for this appointment right now.")
    except (InvalidInputError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/appointments/delete", methods=["POST"])
@role_required("admin")
def admin_delete_appointment():
    rid = request.form.get("rid", "")
    try:
        manager.delete_record(rid)
        flash("Appointment removed successfully.", "success")
    except KeyError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/doctor-requests")
@role_required("admin")
def admin_requests():
    requests_list = [
        {"username": username, "name": name, "specializations": specializations.replace(",", ", ")}
        for username, name, specializations in manager.get_doctor_requests()
    ]
    return render_template("admin_requests.html", requests_list=requests_list)


@app.route("/admin/doctor-requests/approve", methods=["POST"])
@role_required("admin")
def admin_approve_doctor():
    try:
        manager.approve_doctor(request.form.get("username", ""))
        flash("Doctor account created successfully.", "success")
    except (KeyError, DuplicateRecordError) as e:
        flash(str(e), "error")
    return redirect(url_for("admin_requests"))


@app.route("/admin/doctor-requests/reject", methods=["POST"])
@role_required("admin")
def admin_reject_doctor():
    try:
        manager.reject_doctor(request.form.get("username", ""))
        flash("Registration rejected.", "success")
    except KeyError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_requests"))


@app.route("/admin/doctors")
@role_required("admin")
def admin_doctors():
    return render_template("admin_doctors.html", doctors=manager.get_doctor_accounts())


def backup_label(item):
    """Plain-language summary of what a backup holds."""
    counts = item["counts"]
    accounts = sum(counts.get(t, 0) for t in ("admins", "doctors", "patients"))
    return f"{accounts} accounts, {counts.get('appointments', 0)} appointments"


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


app.jinja_env.filters["backup_label"] = backup_label
app.jinja_env.filters["human_size"] = human_size
app.jinja_env.filters["backup_when"] = lambda value: value.strftime("%b %d, %Y \u00b7 %I:%M %p").replace(" 0", " ")


def backup_file_arg():
    """The backup file named in the request, or None (and an error message) if it does not exist."""
    name = (request.values.get("file") or "").strip()
    try:
        backup.load_backup(name)
        return name
    except backup.BackupError as e:
        flash(str(e), "error")
        return None


@app.route("/admin/backups")
@role_required("admin")
def admin_backups():
    try:
        backups_found, error = backup.list_backups(), None
    except backup.BackupError as e:
        backups_found, error = [], str(e)
    return render_template(
        "admin_backups.html", backups=backups_found, interval=backup.INTERVAL_DAYS, list_error=error,
        last_auto=backup.latest("auto", backups_found), next_auto=backup.next_automatic(backups_found),
        last_error=backup.status["last_error"], keep_auto=backup.KEEP["auto"],
        due_now=backup.next_automatic(backups_found) <= datetime.now(), auto_on=backup.auto_enabled(),
        store_label=backup.get_store().label, store_warning=backup.storage_warning(),
    )


@app.before_request
def backup_weekly_check():
    """About once an hour, in the background, make the weekly automatic backup if it is due."""
    if request.endpoint != "static":
        backup.maybe_run_in_background()


@app.route("/admin/backups/create", methods=["POST"])
@role_required("admin")
def admin_backup_create():
    try:
        name = backup.create_backup("manual", session["username"])
        flash(f"Backup saved ({name}).", "success")
    except Exception as e:
        flash(f"The backup failed: {e}", "error")
    return redirect(url_for("admin_backups"))


@app.route("/admin/backups/view")
@role_required("admin")
def admin_backup_view():
    name = backup_file_arg()
    if name is None:
        return redirect(url_for("admin_backups"))
    header, tables = backup.load_backup(name)
    return render_template("admin_backup_view.html", name=name, header=header, tables=backup.preview(tables), rows_shown=backup.PREVIEW_ROWS)


@app.route("/admin/backups/download")
@role_required("admin")
def admin_backup_download():
    name = backup_file_arg()
    if name is None:
        return redirect(url_for("admin_backups"))
    return Response(backup.read_backup_file(name), mimetype="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/admin/backups/delete", methods=["POST"])
@role_required("admin")
def admin_backup_delete():
    try:
        backup.delete_backup(request.form.get("file", ""))
        flash("Backup deleted.", "success")
    except backup.BackupError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_backups"))


@app.route("/admin/backups/restore", methods=["GET", "POST"])
@role_required("admin")
def admin_backup_restore():
    name = backup_file_arg()
    if name is None:
        return redirect(url_for("admin_backups"))
    if request.method == "POST":
        login = manager.authenticate(session["username"], request.form.get("password", ""))
        if not login or login["role"] != "admin" or login["username"] != session["username"]:
            flash("Wrong password. Nothing was restored.", "error")
        elif request.form.get("confirm", "").strip() != "RESTORE":
            flash("Type RESTORE in capital letters to confirm. Nothing was restored.", "error")
        else:
            try:
                result = backup.restore_backup(name, session["username"])
            except backup.BackupError as e:
                flash(str(e), "error")
                return redirect(url_for("admin_backup_restore", file=name))
            manager.load_from_file()
            session.clear()
            flash("Backup restored. Please sign in again." + (
                f" A copy of the data from just before the restore was saved ({result['safety_copy']})." if result["safety_copy"] else ""
            ), "success")
            return redirect(url_for("login"))
        return redirect(url_for("admin_backup_restore", file=name))
    header, _ = backup.load_backup(name)
    now, then = backup.current_counts(), header.get("counts", {})
    groups = [("Accounts", ("admins", "doctors", "patients")), ("Doctor requests", ("doctor_requests",)),
              ("Appointments", ("appointments",)), ("Notifications", ("notifications",)),
              ("Profile pictures", ("profile_pictures",))]
    rows = [(label, sum(now.get(t, 0) for t in tables), sum(then.get(t, 0) for t in tables)) for label, tables in groups]
    return render_template("admin_backup_restore.html", name=name, header=header, rows=rows)


@app.route("/patient/book", methods=["GET", "POST"])
@role_required("patient")
def patient_book():
    if request.method == "POST":
        service = request.form.get("service", SERVICES[0])
        try:
            if not request.form.get("date") or not request.form.get("slot"):
                raise InvalidInputError("Please pick a date and a time.")
            appointment = manager.create_appointment(
                session["name"], service, f"{request.form['date']} {request.form['slot']}",
                None, clean_reason(request.form.get("reason")), session["username"],
            )
            notify_patient(appointment, "Your appointment is booked.", "Appointment confirmed")
            push_notifications(
                appointment,
                doctor_msg=f"New {appt_text(appointment)} with {appointment.patient_name}.",
                admin_msg=f"{appointment.patient_name} booked a {appt_text(appointment)} with {doctor_label(appointment.doctor)}.",
            )
            flash(f"Appointment {appointment.record_id} booked with {appointment.doctor}.", "success")
            return redirect(url_for("patient_appointments"))
        except (InvalidInputError, DuplicateRecordError) as e:
            flash(str(e), "error")
        return redirect(url_for("patient_book", service=service))

    service = request.args.get("service", SERVICES[0])
    if service not in SERVICES:
        service = SERVICES[0]
    cal, slots, selected = booking_calendar("patient_book", {"service": service}, service, None)
    return render_template(
        "patient_book.html", service=service, services=SERVICES, cal=cal, slots=slots, selected=selected,
        has_doctors=bool(manager.get_doctors()),
    )


def own_record(rid):
    """The appointment with this id, but only if it belongs to the signed-in patient."""
    record = manager.get_record(rid)
    return record if record is not None and record.patient_username == session["username"] else None


@app.route("/patient/reschedule", methods=["GET", "POST"])
@role_required("patient")
def patient_reschedule():
    rid = (request.args.get("rid") or request.form.get("rid") or "").strip()
    record = own_record(rid)
    if record is None or not can_cancel(record):
        flash("That appointment can no longer be changed.", "error")
        return redirect(url_for("patient_appointments"))

    if request.method == "POST":
        try:
            if not request.form.get("date") or not request.form.get("slot"):
                raise InvalidInputError("Please pick a date and a time.")
            old_text = appt_text(record)
            manager.reschedule_appointment(
                rid, record.patient_name, record.service, f"{request.form['date']} {request.form['slot']}",
                record.doctor, clean_reason(request.form.get("reason")),
            )
            updated = manager.get_record(rid)
            notify_patient(updated, "Your appointment was rescheduled.", "Appointment rescheduled")
            push_notifications(
                updated,
                doctor_msg=f"{updated.patient_name} rescheduled the {old_text}. New time: {pretty_datetime(updated.datetime_str)}.",
                admin_msg=f"{updated.patient_name} rescheduled the {old_text} with {doctor_label(updated.doctor)}. New time: {pretty_datetime(updated.datetime_str)}.",
            )
            flash(f"Appointment {rid} rescheduled.", "success")
            return redirect(url_for("patient_appointments"))
        except (InvalidInputError, DuplicateRecordError, KeyError) as e:
            flash(str(e), "error")
        return redirect(url_for("patient_reschedule", rid=rid))

    cal, slots, selected = booking_calendar("patient_reschedule", {"rid": rid}, record.service, record.doctor, rid)
    return render_template(
        "patient_reschedule.html", record=record, cal=cal, slots=slots, selected=selected,
        action_url=url_for("patient_reschedule"), back_url=url_for("patient_appointments"),
        back_label="Back to my appointments", for_doctor=False,
    )


@app.route("/patient/appointments/cancel", methods=["POST"])
@role_required("patient")
def patient_cancel():
    rid = request.form.get("rid", "")
    record = own_record(rid)
    try:
        if record is None or not can_cancel(record):
            raise InvalidInputError("That appointment can no longer be cancelled.")
        manager.set_status(rid, "cancelled")
        notify_patient(record, "Your appointment was cancelled.", "Appointment cancelled")
        push_notifications(
            record,
            doctor_msg=f"{record.patient_name} cancelled the {appt_text(record)}.",
            admin_msg=f"{record.patient_name} cancelled the {appt_text(record)} with {doctor_label(record.doctor)}.",
        )
        flash(f"Appointment {rid} cancelled.", "success")
    except (InvalidInputError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("patient_appointments"))


@app.route("/patient/appointments")
@role_required("patient")
def patient_appointments():
    records = manager.get_sorted_records(sort_by="date/time", patient_username=session["username"])
    records, page_info = paginate(records)
    return render_template("patient_appointments.html", records=records, page_info=page_info)


@app.route("/doctor/appointments")
@role_required("doctor")
def doctor_appointments():
    records = manager.get_sorted_records(sort_by="date/time", doctor_filter=session["name"])
    specializations = manager.get_doctor_specializations(session["username"])
    records, page_info = paginate(records)
    return render_template("doctor_appointments.html", records=records, specializations=specializations, page_info=page_info)


@app.route("/doctor/appointments/status", methods=["POST"])
@role_required("doctor")
def doctor_set_status():
    rid = request.form.get("rid", "")
    status = request.form.get("status", "")
    record = manager.get_record(rid)
    try:
        if record is None or record.doctor != session["name"]:
            raise InvalidInputError("Appointment not found.")
        if status in ("completed", "no_show") and can_mark_done(record):
            manager.set_status(rid, status)
            push_done_notifications(record, status, by_admin=False)
            flash(f"Appointment {rid} marked as {'completed' if status == 'completed' else 'no-show'}.", "success")
        elif status == "cancelled" and can_cancel(record):
            manager.set_status(rid, "cancelled")
            notify_patient(record, "Your appointment was cancelled by the doctor.", "Appointment cancelled")
            push_notifications(
                record,
                patient_msg=f"{doctor_label(record.doctor)} cancelled your {appt_text(record)}.",
                admin_msg=f"{doctor_label(record.doctor)} cancelled {record.patient_name}'s {appt_text(record)}.",
            )
            flash(f"Appointment {rid} cancelled. The patient has been notified by email if they have an address on file.", "success")
        else:
            raise InvalidInputError("That update is not allowed for this appointment right now.")
    except (InvalidInputError, KeyError) as e:
        flash(str(e), "error")
    target = "doctor_dashboard" if request.form.get("next") == "dashboard" else "doctor_appointments"
    return redirect(url_for(target))


@app.route("/doctor/book", methods=["GET", "POST"])
@role_required("doctor")
def doctor_book():
    """A doctor books an appointment (for example a follow-up) for an existing patient, on their own calendar."""
    specializations = manager.get_doctor_specializations(session["username"]) or list(SERVICES[:1])
    source = request.form if request.method == "POST" else request.args
    patient = manager.get_patient(source.get("patient", "").strip()) if source.get("patient") else None
    service = source.get("service", specializations[0])
    if service not in specializations:
        service = specializations[0]

    if request.method == "POST":
        try:
            if patient is None:
                raise InvalidInputError("Please choose a patient first.")
            if not request.form.get("date") or not request.form.get("slot"):
                raise InvalidInputError("Please pick a date and a time.")
            appointment = manager.create_appointment(
                patient[1], service, f"{request.form['date']} {request.form['slot']}",
                session["name"], clean_reason(request.form.get("reason")), patient[0],
            )
            notify_patient(appointment, f"{doctor_label(session['name'])} booked an appointment for you.", "Appointment booked for you")
            push_notifications(
                appointment,
                patient_msg=f"{doctor_label(appointment.doctor)} booked a {appt_text(appointment)} for you.",
                admin_msg=f"{doctor_label(appointment.doctor)} booked a {appt_text(appointment)} for {appointment.patient_name}.",
            )
            flash(f"Appointment {appointment.record_id} booked for {appointment.patient_name}. The patient has been notified.", "success")
            return redirect(url_for("doctor_appointments"))
        except (InvalidInputError, DuplicateRecordError) as e:
            flash(str(e), "error")
        return redirect(url_for("doctor_book", patient=patient[0] if patient else "", service=service))

    if patient is None:
        return render_template("doctor_book.html", patient=None, patients=patient_options(),
                               specializations=specializations, service=service, cal=None, slots=[], selected="")
    cal, slots, selected = booking_calendar("doctor_book", {"patient": patient[0], "service": service}, service, session["name"])
    return render_template("doctor_book.html", patient=patient, patients=[], specializations=specializations,
                           service=service, cal=cal, slots=slots, selected=selected)


@app.route("/doctor/reschedule", methods=["GET", "POST"])
@role_required("doctor")
def doctor_reschedule():
    rid = (request.args.get("rid") or request.form.get("rid") or "").strip()
    record = manager.get_record(rid)
    if record is None or record.doctor != session["name"] or not can_cancel(record):
        flash("That appointment can no longer be changed.", "error")
        return redirect(url_for("doctor_appointments"))

    if request.method == "POST":
        try:
            if not request.form.get("date") or not request.form.get("slot"):
                raise InvalidInputError("Please pick a date and a time.")
            old_text = appt_text(record)
            manager.reschedule_appointment(
                rid, record.patient_name, record.service, f"{request.form['date']} {request.form['slot']}",
                record.doctor, clean_reason(request.form.get("reason")),
            )
            updated = manager.get_record(rid)
            notify_patient(updated, "Your appointment was rescheduled by the doctor.", "Appointment rescheduled")
            push_notifications(
                updated,
                patient_msg=f"{doctor_label(updated.doctor)} rescheduled your {old_text}. New time: {pretty_datetime(updated.datetime_str)}.",
                admin_msg=f"{doctor_label(updated.doctor)} rescheduled {updated.patient_name}'s {old_text}. New time: {pretty_datetime(updated.datetime_str)}.",
            )
            flash(f"Appointment {rid} rescheduled. The patient has been notified.", "success")
            return redirect(url_for("doctor_appointments"))
        except (InvalidInputError, DuplicateRecordError, KeyError) as e:
            flash(str(e), "error")
        return redirect(url_for("doctor_reschedule", rid=rid))

    cal, slots, selected = booking_calendar("doctor_reschedule", {"rid": rid}, record.service, record.doctor, rid)
    return render_template(
        "patient_reschedule.html", record=record, cal=cal, slots=slots, selected=selected,
        action_url=url_for("doctor_reschedule"), back_url=url_for("doctor_appointments"),
        back_label="Back to my appointments", for_doctor=True,
    )


@app.route("/notifications")
@login_required
def notifications():
    items = manager.get_notifications(session["username"])
    manager.mark_notifications_read(session["username"])  # shown as "new" on this visit, read afterwards
    return render_template("notifications.html", items=items)


@app.route("/notifications/clear", methods=["POST"])
@login_required
def clear_notifications():
    manager.clear_notifications(session["username"])
    return redirect(url_for("notifications"))


@app.route("/doctor/schedule", methods=["GET", "POST"])
@role_required("doctor")
def doctor_schedule():
    username = session["username"]
    if request.method == "POST":
        schedule = {}
        for weekday in range(7):
            if request.form.get(f"works_{weekday}"):
                schedule[weekday] = (request.form.get(f"start_{weekday}", ""), request.form.get(f"end_{weekday}", ""))
        try:
            manager.set_schedule(username, schedule)
            flash("Your schedule has been updated.", "success")
        except InvalidInputError as e:
            flash(str(e), "error")
        return redirect(url_for("doctor_schedule"))
    saved = manager.get_schedule(username)
    rows = []
    for weekday, day_name in enumerate(DAY_NAMES):
        start, end = saved.get(weekday, (None, None))
        rows.append({
            "index": weekday,
            "name": day_name,
            "works": weekday in saved,
            "start": start.strftime("%H:%M") if start else "09:00",
            "end": end.strftime("%H:%M") if end else "17:00",
        })
    return render_template("doctor_schedule.html", rows=rows, times=TIME_CHOICES)


@app.route("/doctor/block-dates")
@role_required("doctor")
def doctor_block_dates():
    year, month = month_from_request()
    blocked = manager.get_blocked_dates(session["username"])

    def state_for_day(day):
        if day < date.today():
            return "past"
        return "blocked" if day in blocked else "free"

    cal = build_calendar(year, month, state_for_day, None, lambda y, m: url_for("doctor_block_dates", year=y, month=m))
    return render_template("doctor_block_dates.html", cal=cal, blocked=sorted(blocked))


@app.route("/doctor/block-dates/toggle", methods=["POST"])
@role_required("doctor")
def doctor_toggle_block():
    year = request.form.get("year", type=int)
    month = request.form.get("month", type=int)
    try:
        day = date.fromisoformat(request.form.get("date", ""))
        if request.form.get("action") == "unblock":
            manager.unblock_date(session["username"], day)
        else:
            manager.block_date(session["username"], day)
    except (ValueError, InvalidInputError) as e:
        flash(str(e) if isinstance(e, InvalidInputError) else "Invalid date.", "error")
    return redirect(url_for("doctor_block_dates", year=year, month=month))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=False)