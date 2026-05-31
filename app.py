import os
import sys
import socket
import json
import zipfile
import webbrowser
import threading
from datetime import datetime, timezone
from typing import Optional, Any
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import select, desc

# --- PYINSTALLER PATH COMPLIANCE ---
def get_resource_path(relative_path: str) -> str:
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

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

app.config['SECRET_KEY'] = 'super-secret-school-key-change-this'
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
setattr(login_manager, 'login_view', 'login')
login_manager.login_message_category = 'warning'

# --- DATABASE MODELS ---
class User(db.Model, UserMixin): # type: ignore
    __allow_unmapped__ = True
    id: Any = db.Column(db.Integer, primary_key=True)
    username: Any = db.Column(db.String(50), unique=True, nullable=False)
    password_hash: Any = db.Column(db.String(255), nullable=False)
    role: Any = db.Column(db.String(20), nullable=False) # 'Teacher' or 'Admin'

    def __init__(self, username: str, password_hash: str, role: str):
        self.username = username.strip()
        self.password_hash = password_hash
        self.role = role

class SystemConfig(db.Model): # type: ignore
    __allow_unmapped__ = True
    key: Any = db.Column(db.String(50), primary_key=True)
    value: Any = db.Column(db.String(100), nullable=False)

    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value

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

    def __init__(self, name: str, grade: str, current_status: str = 'Out of Safe', last_handled_by: Optional[str] = None, graduation_year: Optional[int] = None):
        self.name = name.strip()
        self.grade = grade.strip()
        self.current_status = current_status
        self.last_handled_by = last_handled_by
        self.graduation_year = graduation_year

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

    def __init__(self, student_id: int, student_name: str, action: str, teacher_username: str):
        self.student_id = student_id
        self.student_name = student_name
        self.action = action
        self.teacher_username = teacher_username

@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))

def verify_admin_role() -> bool:
    return current_user.is_authenticated and getattr(current_user, 'role', '') == 'Admin'

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
                'graduation_year': s.graduation_year
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
                graduation_year=s_data.get('graduation_year')
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
    # Safely strip local domain suffixes to isolate clean device hostnames
    if "." in hostname:
        hostname = hostname.split(".")[0]
    
    local_ip = "127.0.0.1"
    try:
        # Create a dummy socket to find active routing interfaces
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

    # Fetch dynamic bounds
    max_level_str = db.session.get(SystemConfig, 'end_grade_level')
    max_level = int(max_level_str.value) if max_level_str else 12

    # Initialize dynamic snapshot log container
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
        
        # Advance student
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

    # Handle recurrence metrics
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
    """Checked automatically on incoming user activity without background thread bottlenecks"""
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
        pass # Protect route loading in case database tables are pending creation

@app.before_request
def check_setup_redirect():
    """Redirects user to the setup wizard if the system has no admin account configured."""
    if request.endpoint in ['static', 'setup'] or request.path.startswith('/static'):
        return
    try:
        # Check if database has any Admin user
        admin_exists = db.session.scalar(select(User).filter_by(role='Admin'))
        if not admin_exists:
            return redirect(url_for('setup'))
    except Exception:
        pass # Protect in case tables do not exist yet

# --- DATABASE SEED ENGINE & MIGRATION HELPER ---
with app.app_context():
    db.create_all()
    
    # Simple migration helper to dynamically handle model upgrades for local desktop deployments
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    
    # 1. Migrate grade_config Table
    if 'grade_config' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('grade_config')]
        if 'custom_display_name' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE grade_config ADD COLUMN custom_display_name VARCHAR(100)"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    # 2. Migrate student Table (Add graduation_year field if missing)
    if 'student' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('student')]
        if 'graduation_year' not in columns:
            try:
                db.session.execute(db.text("ALTER TABLE student ADD COLUMN graduation_year INTEGER"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    # Ensure system configs are present
    if not db.session.get(SystemConfig, 'start_grade_level'):
        db.session.add(SystemConfig(key='start_grade_level', value='8'))
    if not db.session.get(SystemConfig, 'end_grade_level'):
        db.session.add(SystemConfig(key='end_grade_level', value='12'))
    if not db.session.get(SystemConfig, 'auto_backup_enabled'):
        db.session.add(SystemConfig(key='auto_backup_enabled', value='true'))
    
    if not db.session.scalar(select(User)):
        # Check if backup file exists and load it to restore accidentally wiped DB
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
                        graduation_year=s_data.get('graduation_year')
                    )
                    if last_moved:
                        student.last_moved = last_moved
                    db.session.add(student)
                
                # Re-seed configs
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
        if user and check_password_hash(user.password_hash, password):
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
@login_required
def index():
    configs = db.session.scalars(select(GradeConfig).order_by(GradeConfig.level, GradeConfig.track)).all()
    grades = [c.display_name for c in configs]
    return render_template('index.html', grades=grades)

@app.route('/api/students')
@login_required
def get_students_by_grade():
    grade = request.args.get('grade')
    if not grade or grade.upper() == 'ALL':
        # Enrolled Students (excluding Graduated completely)
        stmt = select(Student).filter(Student.grade != 'Graduated').order_by(Student.name)
    elif grade == 'Graduated':
        # Retrieve exclusively graduated students who STILL have passports inside the safe!
        stmt = select(Student).filter_by(grade='Graduated', current_status='In Safe').order_by(Student.name)
    else:
        # Enrolled Students belonging to a specific selected active grade
        stmt = select(Student).filter_by(grade=grade).order_by(Student.name)
    students = db.session.scalars(stmt).all()
    return jsonify([{
        'id': s.id, 
        'name': s.name, 
        'status': s.current_status,
        'graduation_year': s.graduation_year
    } for s in students])

@app.route('/transaction', methods=['POST'])
@login_required
def handle_transaction():
    student_id = request.form.get('student_id')
    action_type = request.form.get('action')
    if not student_id or not action_type:
        return redirect(url_for('index'))
    
    student = db.session.get(Student, int(student_id))
    if student:
        status_map = {'In': 'In Safe', 'Out': 'Out of Safe'}
        student.current_status = status_map[action_type]
        student.last_moved = datetime.now(timezone.utc)
        student.last_handled_by = getattr(current_user, 'username', 'System')
        
        log = TransactionLog(
            student_id=student.id,
            student_name=student.name,
            action=action_type,
            teacher_username=str(getattr(current_user, 'username', 'System'))
        )
        db.session.add(log)
        db.session.commit()
        trigger_auto_backup()
        flash(f"Updated status for {student.name}.", 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    students = db.session.scalars(select(Student).order_by(Student.name)).all()
    logs = db.session.scalars(select(TransactionLog).order_by(desc(TransactionLog.timestamp))).all()
    archive_periods = db.session.scalars(select(ArchivePeriod).order_by(desc(ArchivePeriod.archived_at))).all()
    return render_template('dashboard.html', students=students, logs=logs, archive_periods=archive_periods)

@app.route('/settings')
@login_required
def settings():
    if not verify_admin_role():
        return "Unauthorized", 403
    teachers = db.session.scalars(select(User).filter(User.username != current_user.username)).all()
    students = db.session.scalars(select(Student).filter(Student.grade != 'Graduated').order_by(Student.grade, Student.name)).all()
    graduates = db.session.scalars(select(Student).filter(Student.grade == 'Graduated').order_by(Student.name)).all()
    grade_configs = db.session.scalars(select(GradeConfig).order_by(GradeConfig.level, GradeConfig.track)).all()
    
    # Get configuration bounds
    start_grade = db.session.get(SystemConfig, 'start_grade_level')
    end_grade = db.session.get(SystemConfig, 'end_grade_level')
    start_val = start_grade.value if start_grade else "8"
    end_val = end_grade.value if end_grade else "12"

    # Fetch active scheduled tasks
    scheduled_rollovers = db.session.scalars(select(ScheduledRollover).order_by(ScheduledRollover.target_date)).all()

    # Get local active network parameters (hostnames, IP values) for printing QR labels
    hostname, local_ip = get_local_network_info()

    # Auto-backup configuration state
    auto_backup_cfg = db.session.get(SystemConfig, 'auto_backup_enabled')
    auto_backup_enabled = auto_backup_cfg.value if auto_backup_cfg else 'true'
    backup_exists = os.path.exists(backup_zip_path)

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
                           backup_exists=backup_exists)

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
                
                # Auto-populate all grades sequentially in range with empty track splits
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
            # Cascading dynamic update on student active records to prevent directory mismatches
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

    # Determine original configuration scope to extract level
    src_cfg = db.session.scalar(select(GradeConfig).filter(
        (GradeConfig.custom_display_name == source_grade) | 
        (GradeConfig.track == source_grade)
    ))
    
    level = 9
    if src_cfg:
        level = src_cfg.level
    else:
        # Fallback manual numeric parser
        cleaned = source_grade.replace("Grade", "").strip()
        num_str = "".join([c for c in cleaned if c.isdigit()])
        if num_str:
            level = int(num_str)

    # Check/Create destination category
    dest_cfg = db.session.scalar(select(GradeConfig).filter_by(level=level, track=dest_track))
    if not dest_cfg:
        dest_cfg = GradeConfig(level=level, track=dest_track)
        db.session.add(dest_cfg)
        db.session.flush()

    dest_display_name = dest_cfg.display_name

    # Move selected students
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
    if username and password:
        if db.session.scalar(select(User).filter_by(username=username)):
            flash('Error: Identity alias already exists.', 'danger')
        else:
            db.session.add(User(username=username, password_hash=generate_password_hash(password), role=role))
            db.session.commit()
            trigger_auto_backup()
            flash(f"Account for '{username}' created.", 'success')
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
    if not grade or not raw_names: return redirect(url_for('settings'))
    
    names = [n.strip() for n in raw_names.split('\n') if n.strip()]
    for name in names:
        db.session.add(Student(name=name, grade=grade))
    db.session.commit()
    trigger_auto_backup()
    flash(f"Enrolled {len(names)} profiles under category context '{grade}'.", "success")
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
    # If the system is already initialized with an Admin user, do not allow setup
    try:
        admin_exists = db.session.scalar(select(User).filter_by(role='Admin'))
        if admin_exists:
            flash("System is already initialized.", "info")
            return redirect(url_for('login'))
    except Exception:
        pass
        
    backup_exists = os.path.exists(backup_zip_path)
    
    if request.method == 'POST':
        # Check if they opted for Restore from Backup
        if request.form.get('restore') == 'true':
            if restore_from_backup_file():
                flash("Database successfully restored from passport_tracker_backup.zip! Log in with your previous credentials.", "success")
                return redirect(url_for('login'))
            else:
                flash("Restore failed. Please verify the backup file exists and is not corrupted.", "danger")
                return redirect(url_for('setup'))
        
        # Fresh setup
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
            # Create user
            admin = User(username=username, password_hash=generate_password_hash(password), role="Admin")
            db.session.add(admin)
            
            # Save system configurations
            cfg_start = SystemConfig(key='start_grade_level', value=str(start_val))
            cfg_end = SystemConfig(key='end_grade_level', value=str(end_val))
            cfg_backup = SystemConfig(key='auto_backup_enabled', value='true' if auto_backup else 'false')
            db.session.add_all([cfg_start, cfg_end, cfg_backup])
            
            # Sequentially create gradeconfigs in range
            for lvl in range(start_val, end_val + 1):
                db.session.add(GradeConfig(level=lvl, track=""))
                
            db.session.commit()
            
            # Log in newly created administrator
            login_user(admin)
            
            # Trigger initial backup if enabled
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

def open_browser():
    """Wait briefly for server spinup, then automatically trigger default local browser window."""
    try:
        webbrowser.open("http://127.0.0.1:5000/")
    except Exception:
        pass

if __name__ == '__main__':
    # Listen on all network interfaces so other local devices can access the server
    threading.Timer(1.5, open_browser).start()
    app.run(host='0.0.0.0', port=5000, debug=False)