import argparse
import os
import sys
import socket
import json
import zipfile
import webbrowser
import threading
import subprocess
import tempfile
import time
import re
import shutil
import queue
import atexit
import signal
import uuid
from datetime import datetime, timezone
from typing import Optional, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import select, desc

# --- PYINSTALLER PATH COMPLIANCE ---
def get_resource_path(relative_path: str) -> str:
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def parse_app_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse application arguments, including the one-time update restart flag."""
    parser = argparse.ArgumentParser(description="Passport Safe Terminal")
    parser.add_argument(
        "--updated",
        action="store_true",
        help="Run after an OTA replacement without opening a new browser tab.",
    )
    return parser.parse_args(argv)


def _ps_single_quote(value: str) -> str:
    """Return a PowerShell-safe single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def build_windows_update_command(source_path: str, target_path: str) -> str:
    """Build the PowerShell chain used to replace the running Windows executable."""
    source = _ps_single_quote(source_path)
    target = _ps_single_quote(target_path)
    return (
        f"$source = {source}; $target = {target}; "
        "Start-Sleep -Seconds 2; "
        "Remove-Item -LiteralPath $target -Force; "
        "Move-Item -LiteralPath $source -Destination $target -Force; "
        "Start-Process -FilePath $target -ArgumentList '--updated' -WindowStyle Hidden"
    )


def _github_api_request(url: str, token: Optional[str] = None) -> dict:
    """Issue a GitHub API request with a timeout and optional authentication."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "passport-safe-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"GitHub API request failed: {exc}") from exc


def _version_tuple(version: str) -> tuple[int, ...]:
    """Convert a release tag such as v1.0.4 into comparable numeric parts."""
    normalized = version.strip().lstrip('vV')
    parts = normalized.split('.')
    if not parts or not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid version: {version}")
    return tuple(int(part) for part in parts)


def is_newer_release(current_version: str, latest_version: str) -> bool:
    """Return whether latest_version is newer than current_version."""
    current = _version_tuple(current_version)
    latest = _version_tuple(latest_version)
    return latest > current


def get_latest_release() -> dict:
    """Retrieve the latest GitHub release metadata."""
    repository = os.environ.get("GITHUB_REPOSITORY", "zalman11/school-test")
    return _github_api_request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        os.environ.get("GITHUB_TOKEN"),
    )


def download_latest_release_asset(
    repository: str,
    asset_name: str,
    destination_dir: str,
) -> tuple[str, str, str]:
    """Download the newest release asset matching the current executable name."""
    release_url = f"https://api.github.com/repos/{repository}/releases/latest"
    release = _github_api_request(release_url, os.environ.get("GITHUB_TOKEN"))
    if not isinstance(release, dict) or not release.get("assets"):
        raise RuntimeError("The latest GitHub release has no downloadable assets.")

    normalized_asset_name = asset_name.casefold()
    matching_asset = next(
        (
            asset
            for asset in release["assets"]
            if asset.get("name", "").casefold() == normalized_asset_name
        ),
        None,
    )
    if not matching_asset or not matching_asset.get("browser_download_url"):
        raise RuntimeError(f"GitHub release does not contain the required asset: {asset_name}")

    temporary_path = os.path.join(
        destination_dir,
        f"{asset_name}.download",
    )
    request = Request(
        matching_asset["browser_download_url"],
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "passport-safe-updater",
            **(
                {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
                if os.environ.get("GITHUB_TOKEN")
                else {}
            ),
        },
    )
    with urlopen(request, timeout=120) as response, open(temporary_path, "wb") as output:
        shutil.copyfileobj(response, output)

    return temporary_path, release["tag_name"], matching_asset["browser_download_url"]


def background_update() -> dict:
    """Download the latest matching release asset and launch its Windows replacement chain."""
    if os.name != "nt":
        raise RuntimeError("OTA updates are supported on Windows only.")

    current_executable = os.path.abspath(sys.argv[0])
    executable_name = os.path.basename(current_executable).lower()
    if not executable_name.endswith(".exe"):
        raise RuntimeError("The current executable must have a .exe extension.")

    repository = os.environ.get("GITHUB_REPOSITORY", "zalman11/school-test")
    temporary_dir = tempfile.mkdtemp(prefix="passport-safe-update-")
    downloaded_path, tag_name, download_url = download_latest_release_asset(
        repository,
        os.path.basename(current_executable),
        temporary_dir,
    )
    command = build_windows_update_command(downloaded_path, current_executable)
    powershell = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            command,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return {
        "status": "queued",
        "release": tag_name,
        "download_url": download_url,
        "process_id": powershell.pid,
    }


# Ensure the database is saved in the actual user execution directory, NOT the temporary _MEIPASS folder
db_dir = os.path.abspath(os.path.dirname(sys.argv[0]))
db_path = os.path.join(db_dir, "passport_tracker.db")
backup_zip_path = os.path.join(db_dir, "passport_tracker_backup.zip")
backups_dir = os.path.join(db_dir, "backups")
os.makedirs(backups_dir, exist_ok=True)

app = Flask(
    __name__,
    template_folder=get_resource_path('templates'),
    static_folder=get_resource_path('static')
)

APP_VERSION = os.environ.get('APP_VERSION', '1.0.4')

app.config['SECRET_KEY'] = 'super-secret-school-key-change-this'
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
setattr(login_manager, 'login_view', 'login')
login_manager.login_message_category = 'warning'


def resolve_passport_selection(selection: Optional[str], registered_count: int) -> Optional[int]:
    """Resolve a terminal selection to a safe passport count for a student."""
    try:
        registered_count = max(0, int(registered_count))
    except (TypeError, ValueError):
        return None

    normalized = (selection or '').strip().lower()
    if normalized == 'all':
        return registered_count or None
    if normalized.isdigit():
        selected_count = int(normalized)
        if 1 <= selected_count <= 3 and selected_count <= registered_count:
            return selected_count
    return None


# --- DATABASE MODELS ---
class User(db.Model, UserMixin): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    username: Any = db.Column(db.String(50), unique=True, nullable=False)
    password_hash: Any = db.Column(db.String(255), nullable=True)
    role: Any = db.Column(db.String(20), nullable=False) # 'Teacher' or 'Admin'
    setup_token: Any = db.Column(db.String(100), nullable=True)

    @property
    def is_setup_complete(self) -> bool:
        return bool(self.password_hash and self.password_hash.strip() and not self.setup_token)

    def __init__(self, username: str, password_hash: str = "", role: str = "Teacher", setup_token: Optional[str] = None):
        self.username = username.strip()
        self.password_hash = password_hash
        self.role = role
        self.setup_token = setup_token

class SystemConfig(db.Model): # type: ignore
    __allow_unmapped__ = True
    key: Any = db.Column(db.String(50), primary_key=True)
    value: Any = db.Column(db.String(100), nullable=False)

    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value


def set_system_config(
    key: str,
    value: str,
    overwrite: bool = True,
) -> SystemConfig:
    """Create a configuration value, optionally updating an existing key."""
    config = db.session.get(SystemConfig, key)
    if config is None:
        config = SystemConfig(key=key, value=value)
        db.session.add(config)
    elif overwrite:
        config.value = value
    return config


class ScheduledRollover(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    label: Any = db.Column(db.String(100), nullable=False)
    target_date: Any = db.Column(db.Date, nullable=False) # Store scheduled date
    recurrence: Any = db.Column(db.String(20), nullable=False) # 'once' or 'yearly'
    executed: Any = db.Column(db.Boolean, default=False, nullable=False)

    def __init__(self, label: str, target_date: Any, recurrence: str):
        self.label = label.strip()
        self.target_date = target_date
        self.recurrence = recurrence

class GradeConfig(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    level: Any = db.Column(db.Integer, nullable=False) # e.g., 9
    track: Any = db.Column(db.String(50), nullable=False, default="") # e.g., "A", "Special Support"
    custom_display_name: Any = db.Column(db.String(100), nullable=True) # Custom renamed identifier

    @property
    def display_name(self) -> str:
        if self.custom_display_name:
            return self.custom_display_name
        if self.track:
            return f"Grade {self.level}{self.track}"
        return f"Grade {self.level}"

    def __init__(self, level: int, track: str = "", custom_display_name: Optional[str] = None):
        self.level = level
        self.track = track.strip()
        self.custom_display_name = custom_display_name

class Student(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    name: Any = db.Column(db.String(100), nullable=False)
    grade: Any = db.Column(db.String(50), nullable=False) # Maps to display_name or 'Graduated'
    current_status: Any = db.Column(db.String(20), default='Out of Safe', nullable=False)
    last_moved: Any = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_handled_by: Any = db.Column(db.String(50), nullable=True)
    graduation_year: Any = db.Column(db.Integer, nullable=True) # Dedicated tracking column
    passport_count: Any = db.Column(db.Integer, nullable=False, default=1)

    def __init__(self, name: str, grade: str, current_status: str = 'Out of Safe', last_handled_by: Optional[str] = None, graduation_year: Optional[int] = None, passport_count: int = 1):
        self.name = name.strip()
        self.grade = grade.strip()
        self.current_status = current_status
        self.last_handled_by = last_handled_by
        self.graduation_year = graduation_year
        self.passport_count = passport_count if passport_count in (1, 2, 3) else 1

class ArchivePeriod(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    label: Any = db.Column(db.String(100), unique=True, nullable=False)
    archived_at: Any = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __init__(self, label: str):
        self.label = label.strip()

class ArchivedStudent(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    period_id: Any = db.Column(db.Integer, db.ForeignKey('archive_period.id'), nullable=False)
    name: Any = db.Column(db.String(100), nullable=False)
    grade_at_archive: Any = db.Column(db.String(50), nullable=False)
    status_at_archive: Any = db.Column(db.String(20), nullable=False)
    last_handled_by: Any = db.Column(db.String(50), nullable=True)
    
    period: Any = db.relationship('ArchivePeriod', backref=db.backref('students', cascade="all, delete-orphan"))

    def __init__(self, period_id: int, name: str, grade_at_archive: str, status_at_archive: str, last_handled_by: Optional[str]):
        self.period_id = period_id
        self.name = name
        self.grade_at_archive = grade_at_archive
        self.status_at_archive = status_at_archive
        self.last_handled_by = last_handled_by

class TransactionLog(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    timestamp: Any = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    student_id: Any = db.Column(db.Integer, nullable=False)
    student_name: Any = db.Column(db.String(100), nullable=False)
    action: Any = db.Column(db.String(10), nullable=False)
    teacher_username: Any = db.Column(db.String(50), nullable=False)
    staff_member: Any = db.Column(db.String(100), nullable=True)
    reason: Any = db.Column(db.String(100), nullable=True)
    passport_selection: Any = db.Column(db.String(10), nullable=True)

    def __init__(self, student_id: int, student_name: str, action: str, teacher_username: str, staff_member: Optional[str] = None, reason: Optional[str] = None, passport_selection: Optional[str] = None):
        self.student_id = student_id
        self.student_name = student_name
        self.action = action
        self.teacher_username = teacher_username
        self.staff_member = staff_member
        self.reason = reason
        self.passport_selection = passport_selection

class Notification(db.Model): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    timestamp: Any = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    title: Any = db.Column(db.String(200), nullable=False)
    message: Any = db.Column(db.Text, nullable=False)
    recipients: Any = db.Column(db.String(200), nullable=False)
    email_status: Any = db.Column(db.String(50), nullable=False) # 'sent via email' or 'not sent via email'

    def __init__(self, title: str, message: str, recipients: str, email_status: str):
        self.title = title
        self.message = message
        self.recipients = recipients
        self.email_status = email_status

@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))

def verify_admin_role() -> bool:
    return current_user.is_authenticated and getattr(current_user, 'role', '') == 'Admin'

# --- AUTOMATED EMAIL NOTIFICATION HELPER ---
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_checkout_email_alert(student_name: str, grade: str, passport_count: int, staff_member: str, reason: str, teacher_username: str):
    """Asynchronously dispatches an automated email alert when special checkout reasons are triggered and logs to System Inbox."""
    def email_worker():
        recipients = ["kklein@levhatorah.org", "amendlowitz@levhatorah.org"]
        
        with app.app_context():
            server_cfg = db.session.get(SystemConfig, 'smtp_server')
            port_cfg = db.session.get(SystemConfig, 'smtp_port')
            user_cfg = db.session.get(SystemConfig, 'smtp_sender_email')
            pass_cfg = db.session.get(SystemConfig, 'smtp_sender_password')
            
            smtp_server = server_cfg.value if server_cfg else "smtp.gmail.com"
            try:
                smtp_port = int(port_cfg.value) if port_cfg else 587
            except ValueError:
                smtp_port = 587
            sender_email = user_cfg.value if user_cfg else ""
            sender_password = pass_cfg.value if pass_cfg else ""

        subject = f"[PASSPORT ALERT] {student_name} - Passport Checked Out ({reason})"
        body = f"""PASSPORT SAFE TERMINAL - AUTOMATED NOTIFICATION

A passport has been checked out with a flagged security/travel reason.

• Student Name: {student_name}
• Class/Group: {grade}
• Passport Count: {passport_count}
• Action: Withdrawn from Safe (Out)
• Reason for Checkout: {reason}
• Authorized Staff Member: {staff_member}
• Logged System User: @{teacher_username}
• Timestamp (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}

This is an automated system alert sent to designated security contacts ({', '.join(recipients)}).
"""

        msg = MIMEMultipart()
        msg['From'] = sender_email if sender_email else "passport-safe@levhatorah.org"
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        email_sent_successfully = False

        if sender_email and sender_password:
            try:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(sender_email, sender_password)
                    server.sendmail(sender_email, recipients, msg.as_string())
                email_sent_successfully = True
                app.logger.info(f"Email alert sent successfully to {recipients} for {student_name}.")
                print(f"📧 Email alert sent to {recipients} for {student_name} ({reason})")
            except Exception as e:
                email_sent_successfully = False
                app.logger.error(f"Failed to send email alert via SMTP: {e}")
                print(f"⚠️ SMTP alert dispatch error: {e}")
        else:
            email_sent_successfully = False
            app.logger.info(f"SMTP credentials not configured. Email alert triggered for {student_name} ({reason}) to {recipients}.")
            print(f"📧 [Automated Alert Triggered] To: {recipients} | Subject: {subject} | Student: {student_name}")

        # Always record in Internal Inbox Notification Log (strictly internal logging, never alters or blocks emails!)
        try:
            with app.app_context():
                status_tag = "sent via email" if email_sent_successfully else "not sent via email"
                notif = Notification(
                    title=f"Passport Alert: {student_name} ({reason})",
                    message=f"Passport checked out for {student_name} ({grade}). Reason: {reason}. Authorized Staff Member: {staff_member}. Processed by @{teacher_username}.",
                    recipients=", ".join(recipients),
                    email_status=status_tag
                )
                db.session.add(notif)
                db.session.commit()
        except Exception as err:
            app.logger.error(f"Failed to record internal inbox notification: {err}")

    threading.Thread(target=email_worker, daemon=True).start()

# --- DATABASE BACKUP & RESTORE UTILITIES ---
def trigger_auto_backup() -> bool:
    """Serializes all User and Student accounts to a compact ZIP archive if auto_backup is enabled."""
    try:
        enabled_cfg = db.session.get(SystemConfig, 'auto_backup_enabled')
        if enabled_cfg and enabled_cfg.value == 'false':
            return False
        
        users = db.session.scalars(select(User)).all()
        students = db.session.scalars(select(Student)).all()
        
        backup_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'users': [{
                'username': u.username,
                'password_hash': u.password_hash,
                'role': u.role
            } for u in users],
            'students': [{
                'name': s.name,
                'grade': s.grade,
                'current_status': s.current_status,
                'last_moved': s.last_moved.isoformat() if s.last_moved else None,
                'last_handled_by': s.last_handled_by,
                'graduation_year': s.graduation_year,
                'passport_count': s.passport_count
            } for s in students]
        }
        
        json_data = json.dumps(backup_data, indent=4)
        
        # Save to main zip file
        with zipfile.ZipFile(backup_zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("passport_tracker_backup.json", json_data)
            
        # Also save a timestamped backup in the backups/ directory
        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        history_zip_path = os.path.join(backups_dir, f"backup_{timestamp_str}.zip")
        with zipfile.ZipFile(history_zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("passport_tracker_backup.json", json_data)
            
        # Limit history backups to last 5 copies to save space
        try:
            backup_files = sorted(
                [os.path.join(backups_dir, f) for f in os.listdir(backups_dir) if f.startswith("backup_") and f.endswith(".zip")],
                key=os.path.getmtime
            )
            while len(backup_files) > 5:
                os.remove(backup_files.pop(0))
        except Exception:
            pass
            
        return True
    except Exception as e:
        app.logger.error(f"ZIP Backup failed: {e}")
        return False

def restore_from_backup_file() -> bool:
    """Replaces current database users and students with data from the backup ZIP file."""
    if not os.path.exists(backup_zip_path):
        return False
    try:
        with zipfile.ZipFile(backup_zip_path, 'r') as zipf:
            json_data = zipf.read("passport_tracker_backup.json").decode('utf-8')
            backup_data = json.loads(json_data)
        
        # Wipe existing records
        db.session.query(User).delete()
        db.session.query(Student).delete()
        
        for u_data in backup_data.get('users', []):
            user = User(
                username=u_data['username'],
                password_hash=u_data['password_hash'],
                role=u_data['role']
            )
            db.session.add(user)
            
        for s_data in backup_data.get('students', []):
            last_moved = None
            if s_data.get('last_moved'):
                try:
                    last_moved = datetime.fromisoformat(s_data['last_moved'])
                except Exception:
                    pass
            
            student = Student(
                name=s_data['name'],
                grade=s_data['grade'],
                current_status=s_data.get('current_status', 'Out of Safe'),
                last_handled_by=s_data.get('last_handled_by'),
                graduation_year=s_data.get('graduation_year'),
                passport_count=s_data.get('passport_count', 1)
            )
            if last_moved:
                student.last_moved = last_moved
            db.session.add(student)
            
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Restore failed from ZIP: {e}")
        return False

# --- NETWORK HELPER FOR AUTOMATIC HOSTNAME DISCOVERY ---
def get_local_network_info():
    """Retrieve system hostname and active LAN IPv4 coordinates for dynamic QR configuration."""
    hostname = socket.gethostname()
    if "." in hostname:
        hostname = hostname.split(".")[0]
    
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return hostname.lower(), local_ip

# --- AUTOMATED BACKGROUND SCHEDULER CHECKER ---
def process_automated_rollover(task: ScheduledRollover):
    """Executes the automatic rollover sequence on scheduled dates"""
    active_students = db.session.scalars(select(Student).filter(Student.grade != 'Graduated')).all()
    if not active_students:
        if task.recurrence == 'yearly':
            try:
                task.target_date = task.target_date.replace(year=task.target_date.year + 1)
            except ValueError:
                task.target_date = task.target_date.replace(year=task.target_date.year + 1, day=28)
        else:
            task.executed = True
        db.session.commit()
        return

    max_level_str = db.session.get(SystemConfig, 'end_grade_level')
    max_level = int(max_level_str.value) if max_level_str else 12

    period_label = f"Auto-Archive: {task.label} ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    period = ArchivePeriod(label=period_label)
    db.session.add(period)
    db.session.flush()

    current_year = datetime.now(timezone.utc).year

    for student in active_students:
        archived = ArchivedStudent(
            period_id=period.id, name=student.name,
            grade_at_archive=student.grade, status_at_archive=student.current_status,
            last_handled_by=student.last_handled_by
        )
        db.session.add(archived)
        
        try:
            cleaned = student.grade.replace("Grade", "").strip()
            num_str = "".join([c for c in cleaned if c.isdigit()])
            track_str = "".join([c for c in cleaned if not c.isdigit()]).strip()
            if num_str:
                next_level = int(num_str) + 1
                if next_level > max_level:
                    student.grade = "Graduated"
                    student.graduation_year = current_year
                else:
                    student.grade = f"Grade {next_level}{track_str}"
            else:
                student.grade = "Graduated"
                student.graduation_year = current_year
        except Exception:
            student.grade = "Graduated"
            student.graduation_year = current_year

    if task.recurrence == 'yearly':
        try:
            task.target_date = task.target_date.replace(year=task.target_date.year + 1)
        except ValueError:
            task.target_date = task.target_date.replace(year=task.target_date.year + 1, day=28)
    else:
        task.executed = True

    db.session.commit()
    trigger_auto_backup()

@app.before_request
def check_scheduled_rollovers():
    if request.endpoint in ['static', 'get_resource_path']:
        return
    try:
        today = datetime.now(timezone.utc).date()
        stmt = select(ScheduledRollover).filter(
            ScheduledRollover.target_date <= today,
            ScheduledRollover.executed == False
        )
        pending_rollovers = db.session.scalars(stmt).all()
        for rollover in pending_rollovers:
            process_automated_rollover(rollover)
    except Exception:
        pass

@app.before_request
def check_setup_redirect():
    if request.endpoint in ['static', 'setup'] or request.path.startswith('/static'):
        return
    try:
        admin_exists = db.session.scalar(select(User).filter_by(role='Admin'))
        if not admin_exists:
            return redirect(url_for('setup'))
    except Exception:
        pass

# --- DATABASE SEED ENGINE & MIGRATION HELPER ---
with app.app_context():
    db.create_all()
    
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    
    if 'grade_config' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('grade_config')]
        if 'custom_display_name' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE grade_config ADD COLUMN custom_display_name VARCHAR(100)"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    if 'student' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('student')]
        if 'graduation_year' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE student ADD COLUMN graduation_year INTEGER"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        if 'passport_count' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE student ADD COLUMN passport_count INTEGER NOT NULL DEFAULT 1"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    if 'transaction_log' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('transaction_log')]
        if 'staff_member' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE transaction_log ADD COLUMN staff_member VARCHAR(100)"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        if 'reason' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE transaction_log ADD COLUMN reason VARCHAR(100)"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        if 'passport_selection' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE transaction_log ADD COLUMN passport_selection VARCHAR(10)"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    if 'user' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('user')]
        if 'setup_token' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE user ADD COLUMN setup_token VARCHAR(100)"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    set_system_config('start_grade_level', '8', overwrite=False)
    set_system_config('end_grade_level', '12', overwrite=False)
    set_system_config('auto_backup_enabled', 'true', overwrite=False)
    set_system_config('cloudflare_enabled', 'false', overwrite=False)
    
    # Seed default teacher accounts automatically
    default_teachers = ["Keren Klein", "Gavri Leichter", "Tova Stross", "Ariella Mendlowitz", "Rav Cytrin", "Madrich"]
    for t_name in default_teachers:
        if not db.session.scalar(select(User).filter_by(username=t_name)):
            db.session.add(User(username=t_name, password_hash="", role="Teacher", setup_token=uuid.uuid4().hex))
    db.session.commit()
    
    if not db.session.scalar(select(User)):
        restored = False
        if os.path.exists(backup_zip_path):
            try:
                with zipfile.ZipFile(backup_zip_path, 'r') as zipf:
                    json_data = zipf.read("passport_tracker_backup.json").decode('utf-8')
                    backup_data = json.loads(json_data)
                for u_data in backup_data.get('users', []):
                    user = User(
                        username=u_data['username'],
                        password_hash=u_data['password_hash'],
                        role=u_data['role']
                    )
                    db.session.add(user)
                for s_data in backup_data.get('students', []):
                    last_moved = None
                    if s_data.get('last_moved'):
                        try:
                            last_moved = datetime.fromisoformat(s_data['last_moved'])
                        except Exception:
                            pass
                    student = Student(
                        name=s_data['name'],
                        grade=s_data['grade'],
                        current_status=s_data.get('current_status', 'Out of Safe'),
                        last_handled_by=s_data.get('last_handled_by'),
                        graduation_year=s_data.get('graduation_year'),
                        passport_count=s_data.get('passport_count', 1)
                    )
                    if last_moved:
                        student.last_moved = last_moved
                    db.session.add(student)
                
                db.session.add(SystemConfig(key='auto_backup_enabled', value='true'))
                db.session.commit()
                restored = True
                print("Accidental wipe recovery: Restored database from passport_tracker_backup.zip!")
            except Exception as e:
                db.session.rollback()
                print(f"Error restoring database from ZIP backup during startup: {e}")

# --- SYSTEM CONTROLLER ROUTING ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = db.session.scalar(select(User).filter_by(username=username))
        if user and user.password_hash and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('index'))
        flash('Invalid verification credentials.', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
def index():
    configs = db.session.scalars(select(GradeConfig).order_by(GradeConfig.level, GradeConfig.track)).all()
    grades = [c.display_name for c in configs]
    selected_student_id = request.args.get('student_id', '')

    detected_teacher = getattr(current_user, 'username', '') if current_user.is_authenticated else ''
    session_teacher = session.get('teacher_alias') or request.cookies.get('teacher_alias') or ''
    if not detected_teacher or detected_teacher == 'System':
        detected_teacher = session_teacher

    teachers = db.session.scalars(select(User).filter_by(role='Teacher').order_by(User.username)).all()
    teacher_options = [u.username for u in teachers]

    return render_template('index.html', 
                           grades=grades, 
                           selected_student_id=selected_student_id, 
                           detected_teacher=detected_teacher,
                           teacher_options=teacher_options)

@app.route('/set-teacher-session', methods=['POST'])
def set_teacher_session():
    teacher_alias = (request.form.get('teacher_alias') or '').strip()
    if teacher_alias:
        session['teacher_alias'] = teacher_alias
        response = redirect(request.referrer or url_for('index'))
        response.set_cookie('teacher_alias', teacher_alias, max_age=30*86400)
        flash(f"Active staff identity set to @{teacher_alias}.", 'success')
        return response
    return redirect(url_for('index'))

@app.route('/teacher-setup/<token>', methods=['GET', 'POST'])
def teacher_setup(token: str):
    user = db.session.scalar(select(User).filter_by(setup_token=token))
    if not user:
        flash("Invalid, expired, or previously completed teacher setup link.", "danger")
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''
        if not password or len(password) < 4:
            flash("Password must be at least 4 characters long.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        else:
            user.password_hash = generate_password_hash(password)
            user.setup_token = None
            db.session.commit()
            trigger_auto_backup()
            flash(f"Account setup complete for @{user.username}! You may now log in.", "success")
            return redirect(url_for('login'))
            
    return render_template('teacher_setup.html', user=user, token=token)

@app.route('/api/students')
@login_required
def get_students_by_grade():
    grade = request.args.get('grade')
    if not grade or grade.upper() == 'ALL':
        stmt = select(Student).filter(Student.grade != 'Graduated').order_by(Student.name)
    elif grade == 'Graduated':
        stmt = select(Student).filter_by(grade='Graduated', current_status='In Safe').order_by(Student.name)
    else:
        stmt = select(Student).filter_by(grade=grade).order_by(Student.name)
    students = db.session.scalars(stmt).all()
    return jsonify([{
        'id': s.id, 
        'name': s.name, 
        'status': s.current_status,
        'graduation_year': s.graduation_year,
        'passport_count': s.passport_count
    } for s in students])

@app.route('/transaction', methods=['POST'])
def handle_transaction():
    student_id = request.form.get('student_id')
    action_type = request.form.get('action')
    staff_member = (request.form.get('staff_member') or '').strip()
    reason = (request.form.get('reason') or '').strip()
    passport_selection = request.form.get('passport_selection', 'all').strip().lower()

    if not student_id or not action_type:
        return redirect(url_for('index'))
    
    student = db.session.get(Student, int(student_id))
    if student:
        selected_passport_count = resolve_passport_selection(passport_selection, student.passport_count)
        if selected_passport_count is None:
            flash("The selected passport quantity is not available for this student.", 'danger')
            return redirect(url_for('index'))

        status_map = {'In': 'In Safe', 'Out': 'Out of Safe'}
        student.current_status = status_map[action_type]
        student.last_moved = datetime.now(timezone.utc)
        
        fallback_teacher = session.get('teacher_alias') or request.cookies.get('teacher_alias') or 'Guest'
        handler_name = staff_member if staff_member else (str(getattr(current_user, 'username', '')) if current_user.is_authenticated else fallback_teacher)
        student.last_handled_by = handler_name
        
        logged_user = str(getattr(current_user, 'username', '')) if current_user.is_authenticated else fallback_teacher
        log = TransactionLog(
            student_id=student.id,
            student_name=student.name,
            action=action_type,
            teacher_username=logged_user,
            staff_member=staff_member if staff_member else None,
            reason=reason if reason else None,
            passport_selection=str(selected_passport_count) if passport_selection != 'all' else 'all'
        )
        db.session.add(log)
        db.session.commit()
        trigger_auto_backup()

        alert_reasons = ["Travel In Israel", "Travel Outside of Israel", "Leaving the Yeshiva"]
        if action_type == 'Out' and reason in alert_reasons:
            send_checkout_email_alert(
                student_name=student.name,
                grade=student.grade,
                passport_count=selected_passport_count,
                staff_member=handler_name,
                reason=reason,
                teacher_username=logged_user
            )
            flash(f"Updated status for {student.name}. Automated email alert dispatched to kklein@levhatorah.org & amendlowitz@levhatorah.org.", 'warning')
        else:
            flash(f"Updated status for {student.name}.", 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    students = db.session.scalars(select(Student).order_by(Student.name)).all()
    logs = db.session.scalars(select(TransactionLog).order_by(desc(TransactionLog.timestamp))).all()
    archive_periods = db.session.scalars(select(ArchivePeriod).order_by(desc(ArchivePeriod.archived_at))).all()
    configs = db.session.scalars(select(GradeConfig).order_by(GradeConfig.level, GradeConfig.track)).all()
    grades = [c.display_name for c in configs]
    notifications = db.session.scalars(select(Notification).order_by(desc(Notification.timestamp))).all()
    return render_template('dashboard.html', 
                           students=students, 
                           logs=logs, 
                           archive_periods=archive_periods, 
                           grades=grades,
                           notifications=notifications)

@app.route('/api/update/check', methods=['GET'])
@login_required
def check_for_updates():
    if not verify_admin_role():
        return jsonify({"error": "Admin privileges required."}), 403

    try:
        release = get_latest_release()
        latest_version = release.get('tag_name', '')
        update_available = is_newer_release(APP_VERSION, latest_version)
        return jsonify({
            "current_version": APP_VERSION,
            "latest_version": latest_version,
            "update_available": update_available,
            "message": (
                f"Updates found: {latest_version} is available."
                if update_available
                else f"You are already up to date at {APP_VERSION}."
            ),
        })
    except (RuntimeError, ValueError) as exc:
        return jsonify({
            "error": f"Unable to check for updates: {exc}",
            "update_available": False,
        }), 503


@app.route('/api/update', methods=['POST'])
@login_required
def trigger_update():
    if not verify_admin_role():
        return jsonify({"error": "Admin privileges required."}), 403

    update_thread = threading.Thread(target=background_update, daemon=True)
    update_thread.start()
    return jsonify({
        "status": "queued",
        "message": "The latest release is being downloaded and the application will restart after replacement.",
    }), 202


@app.route('/settings')
@login_required
def settings():
    if not verify_admin_role():
        flash("Access denied. Admin privileges required.", "danger")
        return redirect(url_for('dashboard'))
    teachers = db.session.scalars(select(User).filter(User.username != current_user.username)).all()
    students = db.session.scalars(select(Student).filter(Student.grade != 'Graduated').order_by(Student.grade, Student.name)).all()
    graduates = db.session.scalars(select(Student).filter(Student.grade == 'Graduated').order_by(Student.name)).all()
    grade_configs = db.session.scalars(select(GradeConfig).order_by(GradeConfig.level, GradeConfig.track)).all()
    
    start_grade = db.session.get(SystemConfig, 'start_grade_level')
    end_grade = db.session.get(SystemConfig, 'end_grade_level')
    start_val = start_grade.value if start_grade else "8"
    end_val = end_grade.value if end_grade else "12"

    scheduled_rollovers = db.session.scalars(select(ScheduledRollover).order_by(ScheduledRollover.target_date)).all()
    hostname, local_ip = get_local_network_info()

    auto_backup_cfg = db.session.get(SystemConfig, 'auto_backup_enabled')
    auto_backup_enabled = auto_backup_cfg.value if auto_backup_cfg else 'true'
    backup_exists = os.path.exists(backup_zip_path)

    smtp_server_cfg = db.session.get(SystemConfig, 'smtp_server')
    smtp_port_cfg = db.session.get(SystemConfig, 'smtp_port')
    smtp_email_cfg = db.session.get(SystemConfig, 'smtp_sender_email')
    smtp_pass_cfg = db.session.get(SystemConfig, 'smtp_sender_password')
    
    smtp_server = smtp_server_cfg.value if smtp_server_cfg else ''
    smtp_port = smtp_port_cfg.value if smtp_port_cfg else '587'
    smtp_email = smtp_email_cfg.value if smtp_email_cfg else ''
    smtp_pass = smtp_pass_cfg.value if smtp_pass_cfg else ''

    cf_cfg = db.session.get(SystemConfig, 'cloudflare_enabled')
    cloudflare_enabled = cf_cfg.value if cf_cfg else 'false'

    return render_template('settings.html', 
                           teachers=teachers, 
                           students=students, 
                           graduates=graduates, 
                           grade_configs=grade_configs,
                           start_grade=start_val,
                           end_grade=end_val,
                           scheduled_rollovers=scheduled_rollovers,
                           hostname=hostname,
                           local_ip=local_ip,
                           auto_backup_enabled=auto_backup_enabled,
                           backup_exists=backup_exists,
                           smtp_server=smtp_server,
                           smtp_port=smtp_port,
                           smtp_email=smtp_email,
                           smtp_pass=smtp_pass,
                           cloudflare_enabled=cloudflare_enabled,
                           tunnel_url=tunnel_url)

@app.route('/settings/set-user-password/<int:user_id>', methods=['POST'])
@login_required
def set_user_password(user_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    user = db.session.get(User, user_id)
    if user:
        password = request.form.get('password') or ''
        if len(password) < 4:
            flash("Password must be at least 4 characters long.", "danger")
        else:
            user.password_hash = generate_password_hash(password)
            user.setup_token = None
            db.session.commit()
            trigger_auto_backup()
            flash(f"Password updated for @{user.username}.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/generate-setup-token/<int:user_id>', methods=['POST'])
@login_required
def generate_setup_token(user_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    user = db.session.get(User, user_id)
    if user:
        user.password_hash = ""
        user.setup_token = uuid.uuid4().hex
        db.session.commit()
        trigger_auto_backup()
        flash(f"Generated new setup URL for @{user.username}.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/toggle-cloudflare', methods=['POST'])
@login_required
def toggle_cloudflare():
    if not verify_admin_role(): return "Unauthorized", 403
    global tunnel_process, tunnel_url
    
    cfg = db.session.get(SystemConfig, 'cloudflare_enabled')
    current_val = cfg.value if cfg else 'false'
    new_val = 'false' if current_val == 'true' else 'true'
    
    if not cfg:
        db.session.add(SystemConfig(key='cloudflare_enabled', value=new_val))
    else:
        cfg.value = new_val
    db.session.commit()
    
    if new_val == 'true':
        if not tunnel_process or tunnel_process.poll() is not None:
            threading.Thread(target=start_automatic_tunnel, args=(5000,), daemon=True).start()
        flash("Cloudflare Tunnel ENABLED. Public URL generated.", "success")
    else:
        if tunnel_process and tunnel_process.poll() is None:
            try:
                tunnel_process.terminate()
            except Exception:
                pass
            tunnel_process = None
        tunnel_url = "http://127.0.0.1:5000/"
        flash("Cloudflare Tunnel DISABLED.", "warning")
        
    return redirect(url_for('settings'))

@app.route('/settings/update-email-config', methods=['POST'])
@login_required
def update_email_config():
    if not verify_admin_role(): return "Unauthorized", 403
    server = (request.form.get('smtp_server') or '').strip()
    port = (request.form.get('smtp_port') or '').strip()
    sender_email = (request.form.get('smtp_sender_email') or '').strip()
    sender_password = request.form.get('smtp_sender_password') or ''

    for key, val in [
        ('smtp_server', server),
        ('smtp_port', port),
        ('smtp_sender_email', sender_email),
        ('smtp_sender_password', sender_password)
    ]:
        cfg = db.session.get(SystemConfig, key)
        if not cfg:
            db.session.add(SystemConfig(key=key, value=val))
        else:
            cfg.value = val

    db.session.commit()
    flash("SMTP email notification configuration saved.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/update-bounds', methods=['POST'])
@login_required
def update_bounds():
    if not verify_admin_role(): return "Unauthorized", 403
    start_val_str = request.form.get('start_grade')
    end_val_str = request.form.get('end_grade')

    if start_val_str and end_val_str:
        try:
            start_val = int(start_val_str)
            end_val = int(end_val_str)
            if start_val > end_val:
                flash("Error: First grade cannot be greater than the final grade.", "danger")
            else:
                cfg_start = db.session.get(SystemConfig, 'start_grade_level') or SystemConfig(key='start_grade_level', value=start_val_str)
                cfg_end = db.session.get(SystemConfig, 'end_grade_level') or SystemConfig(key='end_grade_level', value=end_val_str)
                cfg_start.value = str(start_val)
                cfg_end.value = str(end_val)
                db.session.add_all([cfg_start, cfg_end])
                
                created_grades = []
                for lvl in range(start_val, end_val + 1):
                    exists = db.session.scalar(select(GradeConfig).filter_by(level=lvl, track=""))
                    if not exists:
                        new_cfg = GradeConfig(level=lvl, track="")
                        db.session.add(new_cfg)
                        created_grades.append(f"Grade {lvl}")
                
                db.session.commit()
                if created_grades:
                    flash(f"Boundaries updated. Standard grades sequentially created: {', '.join(created_grades)}.", "success")
                else:
                    flash("Academic grade boundaries updated.", "success")
        except ValueError:
            flash("Invalid input types for grade ranges.", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/edit-grade/<int:grade_id>', methods=['POST'])
@login_required
def edit_grade(grade_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    cfg = db.session.get(GradeConfig, grade_id)
    if cfg:
        old_display_name = cfg.display_name
        new_name = request.form.get('new_name', '').strip()
        if new_name:
            cfg.custom_display_name = new_name
            students_to_update = db.session.scalars(select(Student).filter_by(grade=old_display_name)).all()
            for s in students_to_update:
                s.grade = new_name
            db.session.commit()
            trigger_auto_backup()
            flash(f"Renamed '{old_display_name}' to '{new_name}'. Unified student directories.", "success")
        else:
            flash("New name cannot be blank.", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/split-class', methods=['POST'])
@login_required
def split_class():
    if not verify_admin_role(): return "Unauthorized", 403
    source_grade = request.form.get('source_grade')
    dest_track = request.form.get('dest_track', '').strip()
    student_ids = request.form.getlist('split_students')

    if not source_grade or not dest_track or not student_ids:
        flash("Split failed. Specify target tracks and check students.", "danger")
        return redirect(url_for('settings'))

    src_cfg = db.session.scalar(select(GradeConfig).filter(
        (GradeConfig.custom_display_name == source_grade) | 
        (GradeConfig.track == source_grade)
    ))
    
    level = 9
    if src_cfg:
        level = src_cfg.level
    else:
        cleaned = source_grade.replace("Grade", "").strip()
        num_str = "".join([c for c in cleaned if c.isdigit()])
        if num_str:
            level = int(num_str)

    dest_cfg = db.session.scalar(select(GradeConfig).filter_by(level=level, track=dest_track))
    if not dest_cfg:
        dest_cfg = GradeConfig(level=level, track=dest_track)
        db.session.add(dest_cfg)
        db.session.flush()

    dest_display_name = dest_cfg.display_name

    moved_count = 0
    for sid_str in student_ids:
        sid = int(sid_str)
        student = db.session.get(Student, sid)
        if student and student.grade == source_grade:
            student.grade = dest_display_name
            moved_count += 1

    db.session.commit()
    trigger_auto_backup()
    flash(f"Successfully split class: Moved {moved_count} student(s) to '{dest_display_name}'.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/add-schedule', methods=['POST'])
@login_required
def add_schedule():
    if not verify_admin_role(): return "Unauthorized", 403
    label = request.form.get('label')
    target_date_str = request.form.get('target_date')
    recurrence = request.form.get('recurrence') or 'once'

    if label and target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
            new_rollover = ScheduledRollover(label=label, target_date=target_date, recurrence=recurrence)
            db.session.add(new_rollover)
            db.session.commit()
            flash("Scheduled automatic rollover added to system calendar.", "success")
        except ValueError:
            flash("Invalid date selection.", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/delete-schedule/<int:schedule_id>', methods=['POST'])
@login_required
def delete_schedule(schedule_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    task = db.session.get(ScheduledRollover, schedule_id)
    if task:
        db.session.delete(task)
        db.session.commit()
        flash("Scheduled automatic rollover deleted.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/add-grade', methods=['POST'])
@login_required
def add_grade():
    if not verify_admin_role(): return "Unauthorized", 403
    level_raw = request.form.get('level')
    track = (request.form.get('track') or '').strip()
    if level_raw:
        try:
            level = int(level_raw)
            start_grade_cfg = db.session.get(SystemConfig, 'start_grade_level')
            end_grade_cfg = db.session.get(SystemConfig, 'end_grade_level')
            start_grade = int(start_grade_cfg.value) if start_grade_cfg else 8
            end_grade = int(end_grade_cfg.value) if end_grade_cfg else 12

            if level < start_grade or level > end_grade:
                flash(f"Error: Grade must be within range bounds (Grade {start_grade} - {end_grade}).", "danger")
            else:
                exists = db.session.scalar(select(GradeConfig).filter_by(level=level, track=track))
                if exists:
                    flash("This grade configuration already exists.", "danger")
                else:
                    db.session.add(GradeConfig(level=level, track=track))
                    db.session.commit()
                    flash("New class tier configuration initialized.", "success")
        except ValueError:
            flash("Numeric level configuration invalid.", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/delete-grade/<int:grade_id>', methods=['POST'])
@login_required
def delete_grade(grade_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    cfg = db.session.get(GradeConfig, grade_id)
    if cfg:
        db.session.delete(cfg)
        db.session.commit()
        flash("Class tier removed from registry.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/add-teacher', methods=['POST'])
@login_required
def add_teacher():
    if not verify_admin_role(): return "Unauthorized", 403
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    role = request.form.get('role') or 'Teacher'
    if username:
        if db.session.scalar(select(User).filter_by(username=username)):
            flash('Error: Identity alias already exists.', 'danger')
        else:
            if password:
                pwd_hash = generate_password_hash(password)
                token = None
                flash_msg = f"Account for '{username}' created with configured password."
            else:
                pwd_hash = ""
                token = uuid.uuid4().hex
                flash_msg = f"Account for '{username}' initialized. Setup link generated!"
            
            db.session.add(User(username=username, password_hash=pwd_hash, role=role, setup_token=token))
            db.session.commit()
            trigger_auto_backup()
            flash(flash_msg, 'success')
    return redirect(url_for('settings'))

@app.route('/settings/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    user = db.session.get(User, user_id)
    if user:
        db.session.delete(user)
        db.session.commit()
        trigger_auto_backup()
        flash("Staff credentials deleted successfully.", 'success')
    return redirect(url_for('settings'))

@app.route('/settings/bulk-enroll', methods=['POST'])
@login_required
def bulk_enroll():
    if not verify_admin_role(): return "Unauthorized", 403
    grade = (request.form.get('grade') or '').strip()
    raw_names = request.form.get('names_list') or ''
    try:
        passport_count = int(request.form.get('passport_count') or 1)
    except ValueError:
        passport_count = 1, 3
    if passport_count not in (1, 2):
        passport_count = 1
    if not grade or not raw_names: return redirect(url_for('settings'))
    
    names = [n.strip() for n in raw_names.split('\n') if n.strip()]
    for name in names:
        db.session.add(Student(name=name, grade=grade, passport_count=passport_count))
    db.session.commit()
    trigger_auto_backup()
    flash(f"Enrolled {len(names)} profiles under category context '{grade}'.", "success")
    return redirect(url_for('settings'))

@app.route('/settings/update-passport-count/<int:student_id>', methods=['POST'])
@login_required
def update_passport_count(student_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    student = db.session.get(Student, student_id)
    try:
        passport_count = int(request.form.get('passport_count') or 1)
    except ValueError:
        passport_count = 1
    if student and passport_count in (1, 2, 3):
        student.passport_count = passport_count
        db.session.commit()
        trigger_auto_backup()
        flash(f"Updated passport count for {student.name}.", 'success')
    return redirect(url_for('settings'))

@app.route('/settings/remove-student/<int:student_id>', methods=['POST'])
@login_required
def remove_student(student_id: int):
    if not verify_admin_role(): return "Unauthorized", 403
    student = db.session.get(Student, student_id)
    if student:
        db.session.delete(student)
        db.session.commit()
        trigger_auto_backup()
        flash("Student deleted.", 'success')
    return redirect(url_for('settings'))

@app.route('/settings/advance-year', methods=['POST'])
@login_required
def advance_year():
    if not verify_admin_role(): return "Unauthorized", 403
    archive_label = (request.form.get('archive_label') or '').strip()
    if not archive_label: return redirect(url_for('settings'))
    
    if db.session.scalar(select(ArchivePeriod).filter_by(label=archive_label)):
        flash("Error: History term label already used.", "danger")
        return redirect(url_for('settings'))

    active_students = db.session.scalars(select(Student).filter(Student.grade != 'Graduated')).all()
    if not active_students:
        flash("No active students found to advance.", "warning")
        return redirect(url_for('settings'))

    max_level_cfg = db.session.get(SystemConfig, 'end_grade_level')
    max_level = int(max_level_cfg.value) if max_level_cfg else 12

    period = ArchivePeriod(label=archive_label)
    db.session.add(period)
    db.session.flush()

    current_year = datetime.now(timezone.utc).year

    for student in active_students:
        archived = ArchivedStudent(
            period_id=period.id, name=student.name,
            grade_at_archive=student.grade, status_at_archive=student.current_status,
            last_handled_by=student.last_handled_by
        )
        db.session.add(archived)
        
        try:
            cleaned = student.grade.replace("Grade", "").strip()
            num_str = "".join([c for c in cleaned if c.isdigit()])
            track_str = "".join([c for c in cleaned if not c.isdigit()]).strip()
            if num_str:
                next_level = int(num_str) + 1
                if next_level > max_level:
                    student.grade = "Graduated"
                    student.graduation_year = current_year
                else:
                    student.grade = f"Grade {next_level}{track_str}"
            else:
                student.grade = "Graduated"
                student.graduation_year = current_year
        except Exception:
            student.grade = "Graduated"
            student.graduation_year = current_year

    db.session.commit()
    trigger_auto_backup()
    flash(f"Snapshot committed under historical category context '{archive_label}'. Active grades pushed. Senior students graduated.", "success")
    return redirect(url_for('settings'))

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    try:
        admin_exists = db.session.scalar(select(User).filter_by(role='Admin'))
        if admin_exists:
            flash("System is already initialized.", "info")
            return redirect(url_for('login'))
    except Exception:
        pass
        
    backup_exists = os.path.exists(backup_zip_path)
    
    if request.method == 'POST':
        if request.form.get('restore') == 'true':
            if restore_from_backup_file():
                flash("Database successfully restored from passport_tracker_backup.zip! Log in with your previous credentials.", "success")
                return redirect(url_for('login'))
            else:
                flash("Restore failed. Please verify the backup file exists and is not corrupted.", "danger")
                return redirect(url_for('setup'))
        
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        start_grade_str = request.form.get('start_grade') or '8'
        end_grade_str = request.form.get('end_grade') or '12'
        auto_backup = request.form.get('auto_backup') == 'true'
        
        if not username or not password:
            flash("Username and password are required.", "danger")
            return redirect(url_for('setup'))
            
        try:
            start_val = int(start_grade_str)
            end_val = int(end_grade_str)
            if start_val > end_val:
                flash("Starting grade cannot be greater than ending grade.", "danger")
                return redirect(url_for('setup'))
        except ValueError:
            flash("Invalid grade inputs.", "danger")
            return redirect(url_for('setup'))
            
        try:
            admin = User(username=username, password_hash=generate_password_hash(password), role="Admin")
            db.session.add(admin)
            
            set_system_config('start_grade_level', str(start_val))
            set_system_config('end_grade_level', str(end_val))
            set_system_config('auto_backup_enabled', 'true' if auto_backup else 'false')
            
            for lvl in range(start_val, end_val + 1):
                db.session.add(GradeConfig(level=lvl, track=""))
                
            db.session.commit()
            
            login_user(admin)
            
            if auto_backup:
                trigger_auto_backup()
                
            flash("Initialization complete! Welcome to the Passport Safe Terminal.", "success")
            return redirect(url_for('index'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error during system setup initialization: {str(e)}", "danger")
            return redirect(url_for('setup'))
            
    return render_template('setup.html', backup_exists=backup_exists)

@app.route('/settings/update-backup-config', methods=['POST'])
@login_required
def update_backup_config():
    if not verify_admin_role(): return "Unauthorized", 403
    enabled = request.form.get('auto_backup_enabled') == 'true'
    
    cfg = db.session.get(SystemConfig, 'auto_backup_enabled')
    if not cfg:
        cfg = SystemConfig(key='auto_backup_enabled', value='true' if enabled else 'false')
        db.session.add(cfg)
    else:
        cfg.value = 'true' if enabled else 'false'
        
    db.session.commit()
    
    if enabled:
        trigger_auto_backup()
        flash("Auto-backup is now ENABLED. An initial database backup has been saved.", "success")
    else:
        flash("Auto-backup is now DISABLED.", "warning")
        
    return redirect(url_for('settings'))

@app.route('/settings/backup/now', methods=['POST'])
@login_required
def manual_backup():
    if not verify_admin_role(): return "Unauthorized", 403
    success = trigger_auto_backup()
    if success:
        flash("Manual backup completed successfully!", "success")
    else:
        flash("Manual backup failed. Please check file permissions or logs.", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/backup/restore', methods=['POST'])
@login_required
def manual_restore():
    if not verify_admin_role(): return "Unauthorized", 403
    success = restore_from_backup_file()
    if success:
        flash("Database restored successfully from the backup file! Active users and students updated.", "success")
    else:
        flash("Restore failed. Verify if a valid backup file exists.", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/reset/students', methods=['POST'])
@login_required
def reset_students():
    if not verify_admin_role(): return "Unauthorized", 403
    try:
        db.session.query(Student).filter(Student.grade != 'Graduated').delete()
        db.session.commit()
        trigger_auto_backup()
        flash("All active student records have been successfully reset.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error resetting active students: {str(e)}", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/reset/graduates', methods=['POST'])
@login_required
def reset_graduates():
    if not verify_admin_role(): return "Unauthorized", 403
    try:
        db.session.query(Student).filter(Student.grade == 'Graduated').delete()
        db.session.commit()
        trigger_auto_backup()
        flash("All graduated student records have been successfully reset.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error resetting graduated students: {str(e)}", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/reset/logs', methods=['POST'])
@login_required
def reset_logs():
    if not verify_admin_role(): return "Unauthorized", 403
    try:
        db.session.query(TransactionLog).delete()
        db.session.commit()
        flash("All transaction logs have been successfully cleared.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error resetting logs: {str(e)}", "danger")
    return redirect(url_for('settings'))

@app.route('/settings/reset/factory', methods=['POST'])
@login_required
def factory_reset():
    if not verify_admin_role(): return "Unauthorized", 403
    try:
        db.session.remove()
        db.engine.dispose()
        
        if os.path.exists(db_path):
            os.remove(db_path)
        
        db.create_all()
        
        logout_user()
        flash("System factory reset complete! All data has been wiped. Please complete the initial onboarding to set up your safe terminal again.", "success")
        return redirect(url_for('setup'))
    except Exception as e:
        flash(f"Error during factory reset: {str(e)}", "danger")
        return redirect(url_for('settings'))

# Global trackers for the thread-based background processes
tunnel_url = "http://127.0.0.1:5000/"
tunnel_process = None

# --- AUTOMATIC OPTIMIZED CLOUDFLARE TUNNEL (UBUNTU & PYINSTALLER SAFED) ---
def start_automatic_tunnel(port=5000):
    import sys
    import os
    import re
    import time
    import subprocess
    import webbrowser
    import threading
    import queue
    import atexit
    import signal
    import shutil

    global tunnel_url, tunnel_process
    print("⚡ Preparing Cloudflare Tunnel on Ubuntu...")
    
    # 1. Determine execution Command
    system_binary = shutil.which("cloudflared")
    if system_binary:
        cloudflared_cmd = [system_binary, "tunnel", "--url", f"http://127.0.0.1:{port}"]
        print(f"✅ Found system-native binary: {system_binary}")
    else:
        is_frozen = getattr(sys, 'frozen', False)
        if is_frozen:
            python_bin = shutil.which("python3") or shutil.which("python") or "python3"
            cloudflared_cmd = [python_bin, "-m", "pycloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"]
            print(f"⚡ App is Frozen. Invoking module fallback via: {python_bin}")
        else:
            cloudflared_cmd = [sys.executable, "-m", "pycloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"]
            print("⚡ Running cloudflared via Python environment...")

    # 2. Force immediate stdout logs on Ubuntu
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    try:
        process = subprocess.Popen(
            cloudflared_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        tunnel_process = process
    except Exception as e:
        print(f"❌ Failed to boot up Cloudflare subprocess: {e}")
        print("💡 Recommendation: Install Cloudflare natively on Ubuntu: 'sudo apt install cloudflared'")
        return None

    # 3. Secure atexit hook to terminate subprocess cleanly on exit (Avoid Zombie tasks on Ubuntu)
    def cleanup():
        global tunnel_process
        if tunnel_process and tunnel_process.poll() is None:
            print("\n🛑 Shutting down Cloudflare Tunnel service...")
            try:
                tunnel_process.send_signal(signal.SIGINT)
                try:
                    tunnel_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    tunnel_process.kill()
                    tunnel_process.wait()
                print("✅ Service detached.")
            except Exception:
                pass
    atexit.register(cleanup)

    # 4. Read stdout using a Thread-safe Queue to prevent Python's readline from freezing on Linux
    io_queue = queue.Queue()
    
    def enqueue_output(out, q):
        try:
            for line in iter(out.readline, ''):
                if not line:
                    break
                q.put(line)
        except Exception:
            pass
        finally:
            out.close()

    io_thread = threading.Thread(target=enqueue_output, args=(process.stdout, io_queue))
    io_thread.daemon = True
    io_thread.start()

    # 5. Extract Dynamic URL
    for i in range(300): # Allow ~60 seconds max loop margin
        if process.poll() is not None:
            print("❌ Cloudflare subprocess exited prematurely.")
            break
            
        # Extract lines from asynchronous queue
        while not io_queue.empty():
            try:
                line = io_queue.get_nowait()
                clean_line = line.strip()
                
                # Stream logs so you can see downloader metrics or diagnostic errors!
                print(f"   [cloudflared] {clean_line}")

                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", clean_line)
                if match:
                    tunnel_url = match.group(0)
                    print("\n" + "="*60)
                    print(f"🔗 PUBLIC HTTPS LINK GENERATED: {tunnel_url}")
                    print("="*60 + "\n")
                    return process
            except queue.Empty:
                break
                
        time.sleep(0.2)
        
    print("⚠️ Tunnel initialization timed out. No domain resolved.")
    return process


def main() -> None:
    args = parse_app_args()

    # Start Cloudflare tunnel ONLY if enabled in settings
    with app.app_context():
        cfg = db.session.get(SystemConfig, 'cloudflare_enabled')
        if cfg and cfg.value == 'true':
            tunnel_thread = threading.Thread(target=start_automatic_tunnel, args=(5000,), daemon=True)
            tunnel_thread.start()

    if not args.updated:
        browser_url = tunnel_url if tunnel_url.startswith("https://") else "http://127.0.0.1:5000/"
        webbrowser.open(browser_url)

    try:
        # Fire up local Flask web service
        app.run(host='0.0.0.0', port=5000, debug=False)
    finally:
        print("\n🛑 Closing background tunnel...")
        if 'tunnel_process' in locals() and tunnel_process is not None:
            try:
                tunnel_process.terminate()
                tunnel_process.wait(timeout=2)
            except Exception:
                pass


if __name__ == '__main__':
    main()
