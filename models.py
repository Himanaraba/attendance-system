from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    grade = db.Column(db.Integer, nullable=True)
    user_class = db.Column(db.String(20), nullable=True)
    role = db.Column(db.String(20), default='user')  # user / manager / admin
    _positions = db.Column('positions', db.Text, default='[]')
    _auto_absent_days = db.Column('auto_absent_days', db.Text, default='[]')
    must_change_password = db.Column(db.Boolean, default=False)
    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendances = db.relationship('Attendance', backref='user', lazy=True,
                                  cascade='all, delete-orphan')

    @property
    def positions(self):
        try:
            return json.loads(self._positions or '[]')
        except Exception:
            return []

    @positions.setter
    def positions(self, value):
        self._positions = json.dumps(value or [])

    @property
    def auto_absent_days(self):
        try:
            return json.loads(self._auto_absent_days or '[]')
        except Exception:
            return []

    @auto_absent_days.setter
    def auto_absent_days(self, value):
        self._auto_absent_days = json.dumps(value or [])

    def is_locked(self):
        return bool(self.locked_until and datetime.utcnow() < self.locked_until)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')


class Event(db.Model):
    __tablename__ = 'events'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default='')
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendances = db.relationship('Attendance', backref='event', lazy=True,
                                  cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description or '',
            'date': self.date.isoformat(),
            'start': f"{self.date.isoformat()}T{self.start_time.strftime('%H:%M')}",
            'end': f"{self.date.isoformat()}T{self.end_time.strftime('%H:%M')}",
            'start_time': self.start_time.strftime('%H:%M'),
            'end_time': self.end_time.strftime('%H:%M'),
        }


class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)
    status = db.Column(db.String(20), default='absent')  # present / absent / partial
    partial_start = db.Column(db.Time, nullable=True)
    partial_end = db.Column(db.Time, nullable=True)
    comment = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'event_id'),)


class WeeklyTemplate(db.Model):
    __tablename__ = 'weekly_templates'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=月 … 6=日
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    is_auto = db.Column(db.Boolean, default=False)  # 自動生成ON/OFF
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
