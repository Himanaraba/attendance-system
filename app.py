import os
import csv
import io
import re
import json
import secrets
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, date
from functools import wraps

import click
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, make_response, abort, g,
)
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, jwt_required, create_access_token,
    create_refresh_token, get_jwt_identity, verify_jwt_in_request,
)
from sqlalchemy import text
from models import db, User, Event, Attendance, WeeklyTemplate
import discord_service

# ── App & Config ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', secrets.token_hex(32)),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', 'sqlite:///attendance.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
db.init_app(app)

# JWT (Flutter モバイル用) — 再起動しても鍵が変わらないようファイルに永続化
_jwt_key_file = os.path.join(os.path.dirname(__file__), '.jwt_secret')
if os.environ.get('JWT_SECRET_KEY'):
    _jwt_secret = os.environ['JWT_SECRET_KEY']
elif os.path.exists(_jwt_key_file):
    with open(_jwt_key_file) as _f:
        _jwt_secret = _f.read().strip()
else:
    _jwt_secret = secrets.token_hex(32)
    with open(_jwt_key_file, 'w') as _f:
        _f.write(_jwt_secret)
app.config['JWT_SECRET_KEY'] = _jwt_secret
app.config['JWT_ACCESS_TOKEN_EXPIRES']  = timedelta(hours=2)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
JWTManager(app)

# CORS: /api/v1/* のみ Flutter からのアクセスを許可
CORS(app, resources={r'/api/v1/*': {'origins': '*'}})

MAX_ATTEMPTS = int(os.environ.get('MAX_LOGIN_ATTEMPTS', '5'))
LOCKOUT_MINUTES = int(os.environ.get('LOCKOUT_MINUTES', '30'))

VALID_POSITIONS = {'tech', 'ops', 'teacher'}
VALID_ROLES = {'user', 'manager', 'admin'}
GRADE_OPTIONS = ['M1', 'M2', 'M3', 'H1', 'H2', 'H3', 'H4', 'OB']
VALID_GRADES = set(GRADE_OPTIONS)
ROLE_LEVEL = {'user': 1, 'manager': 2, 'admin': 3}
STATUS_LABEL_JA = {'present': '出席', 'absent': '欠席', 'partial': '部分参加'}
DAY_NAMES_JA = ['月', '火', '水', '木', '金', '土', '日']
DAY_NAMES_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
POSITION_LABELS = {'tech': '技術班', 'ops': '運営班', 'teacher': '顧問'}
POSITION_COLORS = {'tech': 'primary', 'ops': 'success', 'teacher': 'warning'}

# ── Jinja helpers ──────────────────────────────────────────────────────────────

def msg(en, ja):
    return ja if session.get('lang', 'ja') == 'ja' else en


def day_name(dow):
    if session.get('lang', 'ja') == 'ja':
        return DAY_NAMES_JA[dow]
    return DAY_NAMES_EN[dow]


def generate_csrf():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


app.jinja_env.globals.update(csrf_token=generate_csrf, msg=msg, day_name=day_name,
                              zip=zip, enumerate=enumerate,
                              POSITION_LABELS=POSITION_LABELS,
                              POSITION_COLORS=POSITION_COLORS)


def validate_csrf():
    token = session.get('_csrf_token')
    form_token = (request.form.get('_csrf_token')
                  or request.headers.get('X-CSRF-Token'))
    if not token or token != form_token:
        abort(403)


def safe_redirect(url):
    if not url:
        return False
    from urllib.parse import urlparse, urljoin
    test = urljoin(request.host_url, url)
    return urlparse(test).netloc == urlparse(request.host_url).netloc


def auto_generate_events():
    """自動生成フラグONのテンプレートから今日〜5週先まで活動を生成する。"""
    templates = WeeklyTemplate.query.filter_by(is_auto=True).all()
    if not templates:
        return 0
    today = date.today()
    created = 0
    for week_offset in range(6):
        for tmpl in templates:
            base = today + timedelta(weeks=week_offset)
            days_ahead = (tmpl.day_of_week - base.weekday()) % 7
            ev_date = base + timedelta(days=days_ahead)
            if ev_date < today:
                continue
            exists = Event.query.filter_by(
                title=tmpl.title, date=ev_date,
                start_time=tmpl.start_time, end_time=tmpl.end_time).first()
            if not exists:
                db.session.add(Event(title=tmpl.title, date=ev_date,
                                     start_time=tmpl.start_time, end_time=tmpl.end_time))
                created += 1
    if created:
        db.session.commit()
    return created

# ── Startup migration ─────────────────────────────────────────────────────────
# モデル変更後に既存DBへカラムを自動追加（ALTER TABLE）

_migrations_applied = False

@app.before_request
def apply_migrations():
    global _migrations_applied
    if _migrations_applied:
        return
    _migrations_applied = True
    # weekly_templates.is_auto が存在しない古いDBに対して追加
    _safe_add_column('weekly_templates', 'is_auto', 'BOOLEAN DEFAULT 0')
    _safe_add_column('events', 'description', 'TEXT DEFAULT ""')
    _safe_add_column('users', 'discord_id', 'VARCHAR(32)')
    _safe_add_column('users', 'birthday', 'DATE')
    # 既存の整数 grade を NULL にクリア (運用者が新形式 M1〜OB で再設定)
    try:
        db.session.execute(text(
            "UPDATE users SET grade = NULL WHERE grade IS NOT NULL "
            "AND grade NOT IN ('M1','M2','M3','H1','H2','H3','H4','OB')"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _safe_add_column(table, column, col_def):
    try:
        db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {col_def}'))
        db.session.commit()
    except Exception:
        db.session.rollback()  # 既にカラムがある場合は無視

# ── Security headers ───────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    h = response.headers
    h['X-Content-Type-Options'] = 'nosniff'
    h['X-Frame-Options'] = 'DENY'
    h['X-XSS-Protection'] = '1; mode=block'
    h['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    h['Permissions-Policy'] = 'geolocation=(), camera=(), microphone=()'
    if not app.debug:
        h['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ── Context processor ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    current_user = None
    if 'user_id' in session:
        current_user = db.session.get(User, session['user_id'])
    return {'current_user': current_user,
            'lang': session.get('lang', 'ja'),
            'STATUS_LABEL_JA': STATUS_LABEL_JA}

# ── Auth decorator ─────────────────────────────────────────────────────────────

def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                next_url = request.url
                return redirect(url_for('login',
                                        next=next_url if safe_redirect(next_url) else None))
            user = db.session.get(User, session['user_id'])
            if not user or not user.is_active:
                session.clear()
                return redirect(url_for('login'))
            if user.must_change_password and request.endpoint != 'change_password':
                return redirect(url_for('change_password'))
            if role:
                required = [role] if isinstance(role, str) else list(role)
                min_level = min(ROLE_LEVEL.get(r, 99) for r in required)
                if ROLE_LEVEL.get(user.role, 0) < min_level:
                    abort(403)
            g.current_user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        validate_csrf()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        next_url = request.form.get('next', '')

        user = User.query.filter_by(email=email, is_active=True).first()

        if user and user.is_locked():
            flash(msg('Account locked. Try again later.',
                      'アカウントがロックされています。しばらく後にお試しください。'), 'danger')
            return render_template('login.html', next=next_url)

        if user and user.check_password(password):
            user.failed_login_attempts = 0
            user.locked_until = None
            db.session.commit()
            session.permanent = True
            session['user_id'] = user.id
            if user.must_change_password:
                return redirect(url_for('onboarding'))
            dest = next_url if (next_url and safe_redirect(next_url)) else url_for('dashboard')
            return redirect(dest)
        else:
            if user:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= MAX_ATTEMPTS:
                    user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
                    flash(msg(f'Too many failures. Locked for {LOCKOUT_MINUTES} min.',
                              f'失敗が多すぎます。{LOCKOUT_MINUTES}分間ロックされました。'), 'danger')
                else:
                    rem = MAX_ATTEMPTS - user.failed_login_attempts
                    flash(msg(f'Invalid credentials. {rem} attempts left.',
                              f'メールまたはパスワードが違います。残り{rem}回。'), 'danger')
                db.session.commit()
            else:
                flash(msg('Invalid credentials.',
                          'メールアドレスまたはパスワードが違います。'), 'danger')

    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/logout')
def logout():
    session.clear()
    flash(msg('Logged out.', 'ログアウトしました。'), 'info')
    return redirect(url_for('login'))


@app.route('/change_password', methods=['GET', 'POST'])
@login_required()
def change_password():
    user = g.current_user
    if request.method == 'POST':
        validate_csrf()
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        if not user.check_password(current_pw):
            flash(msg('Current password is incorrect.', '現在のパスワードが違います。'), 'danger')
        elif len(new_pw) < 8:
            flash(msg('Password must be at least 8 characters.',
                      'パスワードは8文字以上必要です。'), 'danger')
        elif new_pw != confirm_pw:
            flash(msg('Passwords do not match.', 'パスワードが一致しません。'), 'danger')
        else:
            user.set_password(new_pw)
            user.must_change_password = False
            db.session.commit()
            flash(msg('Password changed successfully.', 'パスワードを変更しました。'), 'success')
            return redirect(url_for('dashboard'))

    return render_template('change_password.html')


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required()
def onboarding():
    """初回ログイン用セットアップ。パスワード設定+プロフィール入力。"""
    user = g.current_user
    # 既に必須項目をクリアしている場合はダッシュボードへ
    if not user.must_change_password and request.method == 'GET':
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        validate_csrf()
        new_pw     = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')
        if len(new_pw) < 8:
            flash(msg('Password must be at least 8 characters.',
                      'パスワードは8文字以上にしてください。'), 'danger')
            return redirect(url_for('onboarding'))
        if new_pw != confirm_pw:
            flash(msg('Passwords do not match.',
                      'パスワードが一致しません。'), 'danger')
            return redirect(url_for('onboarding'))

        user.set_password(new_pw)
        user.must_change_password = False

        # 任意プロフィール
        g_in = request.form.get('grade', '').strip() or None
        if g_in is None or g_in in VALID_GRADES:
            user.grade = g_in
        b_in = request.form.get('birthday', '').strip()
        if b_in:
            try:
                user.birthday = datetime.strptime(b_in, '%Y-%m-%d').date()
            except ValueError:
                pass
        else:
            user.birthday = None
        user.positions = [p for p in request.form.getlist('positions') if p in VALID_POSITIONS]
        d_in = request.form.get('discord_id', '').strip()
        if not d_in:
            user.discord_id = None
        elif d_in.isdigit():
            user.discord_id = d_in

        db.session.commit()
        flash(msg('Setup complete. Welcome!', 'セットアップ完了。ようこそ！'), 'success')
        return redirect(url_for('dashboard'))

    return render_template('onboarding.html')


@app.route('/toggle_lang')
def toggle_lang():
    session['lang'] = 'en' if session.get('lang', 'ja') == 'ja' else 'ja'
    return redirect(request.referrer or url_for('dashboard'))

# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route('/')
@login_required()
def dashboard():
    user = g.current_user
    today = date.today()
    thirty_ago = today - timedelta(days=30)

    # 自動生成テンプレートがあれば今日〜5週先を補完
    auto_created = auto_generate_events()

    total_events = Event.query.count()
    user_atts = Attendance.query.filter_by(user_id=user.id).all()
    att_map = {a.event_id: a for a in user_atts}

    present = sum(1 for a in user_atts if a.status == 'present')
    partial = sum(1 for a in user_atts if a.status == 'partial')
    rate = round((present + partial) / total_events * 100) if total_events else 0

    today_events = (Event.query.filter_by(date=today)
                    .order_by(Event.start_time).all())
    recent_events = (Event.query
                     .filter(Event.date >= thirty_ago)
                     .order_by(Event.date.desc(), Event.start_time.desc())
                     .limit(30).all())

    return render_template('dashboard.html',
                           today=today,
                           total_events=total_events,
                           present=present,
                           partial=partial,
                           absent=total_events - present - partial,
                           rate=rate,
                           today_events=today_events,
                           recent_events=recent_events,
                           att_map=att_map,
                           auto_created=auto_created)


@app.route('/update_attendance', methods=['POST'])
@login_required()
def update_attendance():
    validate_csrf()
    user = g.current_user
    event_id = request.form.get('event_id', type=int)
    status = request.form.get('status', 'absent')
    comment = request.form.get('comment', '').strip() or None
    partial_start = request.form.get('partial_start', '').strip()
    partial_end = request.form.get('partial_end', '').strip()

    if not event_id or status not in ('present', 'absent', 'partial'):
        flash(msg('Invalid input.', '入力値が不正です。'), 'danger')
        return redirect(url_for('dashboard'))

    if not Event.query.get(event_id):
        abort(404)

    att = Attendance.query.filter_by(user_id=user.id, event_id=event_id).first()
    if not att:
        att = Attendance(user_id=user.id, event_id=event_id)
        db.session.add(att)

    att.status = status
    att.comment = comment
    att.updated_at = datetime.utcnow()

    if status == 'partial':
        try:
            att.partial_start = (datetime.strptime(partial_start, '%H:%M').time()
                                 if partial_start else None)
            att.partial_end = (datetime.strptime(partial_end, '%H:%M').time()
                               if partial_end else None)
        except ValueError:
            flash(msg('Invalid time format.', '時刻形式が正しくありません（HH:MM）。'), 'danger')
            return redirect(url_for('dashboard'))
    else:
        att.partial_start = None
        att.partial_end = None

    db.session.commit()
    flash(msg('Attendance updated.', '出欠を更新しました。'), 'success')
    return redirect(request.referrer or url_for('dashboard'))

# ── CSV Export (user) ──────────────────────────────────────────────────────────

@app.route('/export/my_attendance')
@login_required()
def export_my_attendance():
    user = g.current_user
    d_from = request.args.get('from', '')
    d_to = request.args.get('to', '')

    q = (db.session.query(Attendance, Event)
         .join(Event, Attendance.event_id == Event.id)
         .filter(Attendance.user_id == user.id))
    if d_from:
        try:
            q = q.filter(Event.date >= datetime.strptime(d_from, '%Y-%m-%d').date())
        except ValueError:
            pass
    if d_to:
        try:
            q = q.filter(Event.date <= datetime.strptime(d_to, '%Y-%m-%d').date())
        except ValueError:
            pass
    rows = q.order_by(Event.date, Event.start_time).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['日付', '活動', '開始', '終了', 'ステータス',
                '部分参加開始', '部分参加終了', 'コメント'])
    for att, ev in rows:
        w.writerow([
            ev.date.isoformat(), ev.title,
            ev.start_time.strftime('%H:%M'), ev.end_time.strftime('%H:%M'),
            STATUS_LABEL_JA.get(att.status, att.status),
            att.partial_start.strftime('%H:%M') if att.partial_start else '',
            att.partial_end.strftime('%H:%M') if att.partial_end else '',
            att.comment or '',
        ])
    resp = make_response('﻿' + buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename=my_attendance.csv'
    return resp

# ── Admin: User Management ─────────────────────────────────────────────────────

@app.route('/admin/users')
@login_required(['manager', 'admin'])
def admin_users():
    q = request.args.get('q', '').strip()
    pos_filter = request.args.get('position', '').strip()

    users = User.query.filter_by(is_active=True)
    if q:
        users = users.filter(User.name.ilike(f'%{q}%'))
    users = users.order_by(User.name).all()
    if pos_filter:
        users = [u for u in users if pos_filter in u.positions]

    return render_template('admin/users.html', users=users, q=q, pos_filter=pos_filter)


@app.route('/admin/users/add', methods=['POST'])
@login_required('admin')
def admin_add_user():
    validate_csrf()
    email = request.form.get('email', '').strip().lower()
    name = request.form.get('name', '').strip()
    grade = request.form.get('grade', '').strip() or None
    if grade and grade not in VALID_GRADES: grade = None
    user_class = request.form.get('user_class', '').strip()
    role = request.form.get('role', 'user')
    positions = [p for p in request.form.getlist('positions') if p in VALID_POSITIONS]
    auto_absent = [int(d) for d in request.form.getlist('auto_absent_days') if d.isdigit()]

    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        flash(msg('Invalid email format.', 'メール形式が不正です。'), 'danger')
        return redirect(url_for('admin_users'))
    if User.query.filter_by(email=email).first():
        flash(msg('Email already in use.', 'このメールアドレスは既に使われています。'), 'danger')
        return redirect(url_for('admin_users'))
    if role not in VALID_ROLES:
        role = 'user'

    temp_pw = secrets.token_urlsafe(12)
    discord_id = request.form.get('discord_id', '').strip() or None
    if discord_id and not discord_id.isdigit(): discord_id = None
    user = User(email=email, name=name, grade=grade, user_class=user_class,
                role=role, discord_id=discord_id, must_change_password=True)
    user.set_password(temp_pw)
    user.positions = positions
    user.auto_absent_days = auto_absent
    db.session.add(user)
    db.session.commit()
    flash(msg(f'User added. Temp password: {temp_pw}',
              f'ユーザーを追加しました。一時パスワード: {temp_pw}'), 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:uid>/edit', methods=['POST'])
@login_required('admin')
def admin_edit_user(uid):
    validate_csrf()
    user = db.session.get(User, uid)
    if not user:
        abort(404)

    email = request.form.get('email', '').strip().lower()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        flash(msg('Invalid email format.', 'メール形式が不正です。'), 'danger')
        return redirect(url_for('admin_users'))
    if User.query.filter(User.email == email, User.id != uid).first():
        flash(msg('Email already in use.', 'このメールアドレスは既に使われています。'), 'danger')
        return redirect(url_for('admin_users'))

    user.email = email
    user.name = request.form.get('name', '').strip()
    g_in = request.form.get('grade', '').strip() or None
    user.grade = g_in if (g_in is None or g_in in VALID_GRADES) else None
    user.user_class = request.form.get('user_class', '').strip()
    role = request.form.get('role', 'user')
    if role in VALID_ROLES:
        user.role = role
    user.positions = [p for p in request.form.getlist('positions') if p in VALID_POSITIONS]
    user.auto_absent_days = [int(d) for d in request.form.getlist('auto_absent_days')
                             if d.isdigit()]
    discord_id = request.form.get('discord_id', '').strip() or None
    user.discord_id = discord_id if (discord_id is None or discord_id.isdigit()) else user.discord_id
    db.session.commit()
    flash(msg('User updated.', 'ユーザーを更新しました。'), 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:uid>/delete', methods=['POST'])
@login_required('admin')
def admin_delete_user(uid):
    validate_csrf()
    user = db.session.get(User, uid)
    if not user:
        abort(404)
    if user.id == g.current_user.id:
        flash(msg('Cannot delete yourself.', '自分自身は削除できません。'), 'danger')
        return redirect(url_for('admin_users'))
    user.is_active = False
    db.session.commit()
    flash(msg('User deactivated.', 'ユーザーを無効化しました。'), 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:uid>/reset_password', methods=['POST'])
@login_required(['manager', 'admin'])
def admin_reset_password(uid):
    validate_csrf()
    user = db.session.get(User, uid)
    if not user:
        abort(404)
    temp_pw = secrets.token_urlsafe(12)
    user.set_password(temp_pw)
    user.must_change_password = True
    user.failed_login_attempts = 0
    user.locked_until = None
    db.session.commit()
    flash(msg(f'Password reset. Temp password: {temp_pw}',
              f'パスワードをリセットしました。一時パスワード: {temp_pw}'), 'success')
    return redirect(url_for('admin_users'))

# ── Admin: Event Management ────────────────────────────────────────────────────

@app.route('/admin/events')
@login_required(['manager', 'admin'])
def admin_events():
    events = (Event.query
              .order_by(Event.date.desc(), Event.start_time.desc()).all())
    return render_template('admin/events.html', events=events)


@app.route('/admin/events/add', methods=['POST'])
@login_required(['manager', 'admin'])
def admin_add_event():
    validate_csrf()
    title = request.form.get('title', '').strip()
    ev_date = request.form.get('date', '').strip()
    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()

    try:
        d = datetime.strptime(ev_date, '%Y-%m-%d').date()
        st = datetime.strptime(start_time, '%H:%M').time()
        et = datetime.strptime(end_time, '%H:%M').time()
    except ValueError:
        flash(msg('Invalid date/time format.', '日時の形式が正しくありません。'), 'danger')
        return redirect(url_for('admin_events'))
    if st >= et:
        flash(msg('Start must be before end.', '開始時刻は終了時刻より前にしてください。'), 'danger')
        return redirect(url_for('admin_events'))

    ev = Event(title=title, date=d, start_time=st, end_time=et)
    db.session.add(ev)
    db.session.commit()
    discord_service.notify_event_added(ev)
    flash(msg('Event added.', '活動を追加しました。'), 'success')
    return redirect(url_for('admin_events'))


@app.route('/admin/events/<int:eid>/edit', methods=['POST'])
@login_required('admin')
def admin_edit_event(eid):
    validate_csrf()
    event = db.session.get(Event, eid)
    if not event:
        abort(404)
    title = request.form.get('title', '').strip()
    ev_date = request.form.get('date', '').strip()
    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()

    try:
        d = datetime.strptime(ev_date, '%Y-%m-%d').date()
        st = datetime.strptime(start_time, '%H:%M').time()
        et = datetime.strptime(end_time, '%H:%M').time()
    except ValueError:
        flash(msg('Invalid date/time format.', '日時の形式が正しくありません。'), 'danger')
        return redirect(url_for('admin_events'))
    if st >= et:
        flash(msg('Start must be before end.', '開始時刻は終了時刻より前にしてください。'), 'danger')
        return redirect(url_for('admin_events'))

    event.title, event.date, event.start_time, event.end_time = title, d, st, et
    db.session.commit()
    discord_service.notify_event_updated(event)
    flash(msg('Event updated.', '活動を更新しました。'), 'success')
    return redirect(url_for('admin_events'))


@app.route('/admin/events/<int:eid>/delete', methods=['POST'])
@login_required('admin')
def admin_delete_event(eid):
    validate_csrf()
    event = db.session.get(Event, eid)
    if not event:
        abort(404)
    title_snap = event.title
    date_snap  = event.date.isoformat()
    db.session.delete(event)
    db.session.commit()
    discord_service.notify_event_deleted(title_snap, date_snap)
    flash(msg('Event deleted.', '活動を削除しました。'), 'success')
    return redirect(url_for('admin_events'))

# ── Admin: Attendance View ─────────────────────────────────────────────────────

@app.route('/admin/attendance')
@login_required(['manager', 'admin'])
def admin_attendance():
    date_str = request.args.get('date', date.today().isoformat())
    try:
        sel_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        sel_date = date.today()

    events = Event.query.filter_by(date=sel_date).order_by(Event.start_time).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    att_map = {}
    if events:
        for a in Attendance.query.filter(
                Attendance.event_id.in_([e.id for e in events])).all():
            att_map[(a.user_id, a.event_id)] = a

    return render_template('admin/attendance.html',
                           sel_date=sel_date, events=events,
                           users=users, att_map=att_map)


@app.route('/admin/attendance/bulk_update', methods=['POST'])
@login_required(['manager', 'admin'])
def admin_bulk_update():
    token = request.headers.get('X-CSRF-Token')
    if not token or token != session.get('_csrf_token'):
        return jsonify({'error': 'CSRF'}), 403

    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({'error': 'Expected list'}), 400

    for item in data:
        uid = item.get('user_id')
        eid = item.get('event_id')
        status = item.get('status', 'absent')
        if not uid or not eid or status not in ('present', 'absent', 'partial'):
            continue
        att = Attendance.query.filter_by(user_id=uid, event_id=eid).first()
        if not att:
            att = Attendance(user_id=uid, event_id=eid)
            db.session.add(att)
        att.status = status
        att.comment = item.get('comment') or None
        att.updated_at = datetime.utcnow()
        if status == 'partial':
            try:
                ps = item.get('partial_start')
                pe = item.get('partial_end')
                att.partial_start = datetime.strptime(ps, '%H:%M').time() if ps else None
                att.partial_end = datetime.strptime(pe, '%H:%M').time() if pe else None
            except (ValueError, TypeError):
                pass
        else:
            att.partial_start = att.partial_end = None

    db.session.commit()
    return jsonify({'ok': True})

# ── CSV Export (admin) ─────────────────────────────────────────────────────────

@app.route('/admin/export/date')
@login_required(['manager', 'admin'])
def admin_export_date():
    date_str = request.args.get('date', date.today().isoformat())
    try:
        sel_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        sel_date = date.today()

    events = Event.query.filter_by(date=sel_date).order_by(Event.start_time).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    att_map = {}
    if events:
        for a in Attendance.query.filter(
                Attendance.event_id.in_([e.id for e in events])).all():
            att_map[(a.user_id, a.event_id)] = a

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['名前', '学年', 'クラス'] +
               [f"{e.title}({e.start_time.strftime('%H:%M')})" for e in events])
    for u in users:
        row = [u.name, u.grade or '', u.user_class or '']
        for e in events:
            a = att_map.get((u.id, e.id))
            row.append(STATUS_LABEL_JA.get(a.status, '未記入') if a else '未記入')
        w.writerow(row)

    resp = make_response('﻿' + buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=attendance_{date_str}.csv'
    return resp


@app.route('/admin/export/range')
@login_required(['manager', 'admin'])
def admin_export_range():
    d_from = request.args.get('from', '')
    d_to = request.args.get('to', '')
    try:
        df = datetime.strptime(d_from, '%Y-%m-%d').date()
        dt = datetime.strptime(d_to, '%Y-%m-%d').date()
    except ValueError:
        flash(msg('Invalid date range.', '日付範囲が正しくありません。'), 'danger')
        return redirect(url_for('admin_attendance'))

    rows = (db.session.query(Attendance, Event, User)
            .join(Event, Attendance.event_id == Event.id)
            .join(User, Attendance.user_id == User.id)
            .filter(Event.date >= df, Event.date <= dt, User.is_active == True)
            .order_by(Event.date, Event.start_time, User.name).all())

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['日付', '活動', '開始', '終了', '名前', '学年', 'クラス',
                'ステータス', '部分参加開始', '部分参加終了', 'コメント'])
    for att, ev, u in rows:
        w.writerow([
            ev.date.isoformat(), ev.title,
            ev.start_time.strftime('%H:%M'), ev.end_time.strftime('%H:%M'),
            u.name, u.grade or '', u.user_class or '',
            STATUS_LABEL_JA.get(att.status, att.status),
            att.partial_start.strftime('%H:%M') if att.partial_start else '',
            att.partial_end.strftime('%H:%M') if att.partial_end else '',
            att.comment or '',
        ])

    resp = make_response('﻿' + buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = (
        f'attachment; filename=attendance_{d_from}_{d_to}.csv')
    return resp


@app.route('/admin/export/summary')
@login_required(['manager', 'admin'])
def admin_export_summary():
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    total = Event.query.count()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['名前', '学年', 'クラス', 'ロール', '出席', '部分参加', '欠席', '出席率(%)'])
    for u in users:
        c = defaultdict(int)
        for a in u.attendances:
            c[a.status] += 1
        attended = c['present'] + c['partial']
        rate = round(attended / total * 100) if total else 0
        w.writerow([u.name, u.grade or '', u.user_class or '', u.role,
                    c['present'], c['partial'], c['absent'], rate])

    resp = make_response('﻿' + buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename=attendance_summary.csv'
    return resp

# ── Weekly Templates ───────────────────────────────────────────────────────────

@app.route('/admin/templates')
@login_required(['manager', 'admin'])
def admin_templates():
    templates = (WeeklyTemplate.query
                 .order_by(WeeklyTemplate.day_of_week, WeeklyTemplate.start_time).all())
    return render_template('admin/templates.html', templates=templates)


@app.route('/admin/templates/add', methods=['POST'])
@login_required(['manager', 'admin'])
def admin_add_template():
    validate_csrf()
    title = request.form.get('title', '').strip()
    dow = request.form.get('day_of_week', type=int)
    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()

    if dow is None or dow not in range(7):
        flash(msg('Invalid day.', '曜日が不正です。'), 'danger')
        return redirect(url_for('admin_templates'))
    try:
        st = datetime.strptime(start_time, '%H:%M').time()
        et = datetime.strptime(end_time, '%H:%M').time()
    except ValueError:
        flash(msg('Invalid time format.', '時刻形式が正しくありません。'), 'danger')
        return redirect(url_for('admin_templates'))
    if st >= et:
        flash(msg('Start must be before end.', '開始時刻は終了時刻より前にしてください。'), 'danger')
        return redirect(url_for('admin_templates'))

    is_auto = request.form.get('is_auto') == '1'
    db.session.add(WeeklyTemplate(title=title, day_of_week=dow,
                                  start_time=st, end_time=et, is_auto=is_auto))
    db.session.commit()
    flash(msg('Template added.', 'テンプレートを追加しました。'), 'success')
    return redirect(url_for('admin_templates'))


@app.route('/admin/templates/<int:tid>/edit', methods=['POST'])
@login_required('admin')
def admin_edit_template(tid):
    validate_csrf()
    tmpl = db.session.get(WeeklyTemplate, tid)
    if not tmpl:
        abort(404)
    title = request.form.get('title', '').strip()
    dow = request.form.get('day_of_week', type=int)
    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()
    try:
        st = datetime.strptime(start_time, '%H:%M').time()
        et = datetime.strptime(end_time, '%H:%M').time()
    except ValueError:
        flash(msg('Invalid time format.', '時刻形式が正しくありません。'), 'danger')
        return redirect(url_for('admin_templates'))
    if st >= et:
        flash(msg('Start must be before end.', '開始時刻は終了時刻より前にしてください。'), 'danger')
        return redirect(url_for('admin_templates'))
    tmpl.title = title
    tmpl.day_of_week = dow
    tmpl.start_time = st
    tmpl.end_time = et
    tmpl.is_auto = request.form.get('is_auto') == '1'
    db.session.commit()
    flash(msg('Template updated.', 'テンプレートを更新しました。'), 'success')
    return redirect(url_for('admin_templates'))


@app.route('/admin/templates/<int:tid>/delete', methods=['POST'])
@login_required('admin')
def admin_delete_template(tid):
    validate_csrf()
    tmpl = db.session.get(WeeklyTemplate, tid)
    if not tmpl:
        abort(404)
    db.session.delete(tmpl)
    db.session.commit()
    flash(msg('Template deleted.', 'テンプレートを削除しました。'), 'success')
    return redirect(url_for('admin_templates'))


@app.route('/admin/templates/generate', methods=['POST'])
@login_required(['manager', 'admin'])
def admin_generate_from_templates():
    validate_csrf()
    start_str = request.form.get('start_date', '').strip()
    try:
        start = datetime.strptime(start_str, '%Y-%m-%d').date()
    except ValueError:
        flash(msg('Invalid date.', '日付形式が正しくありません。'), 'danger')
        return redirect(url_for('admin_templates'))

    templates = WeeklyTemplate.query.all()
    if not templates:
        flash(msg('No templates defined.', 'テンプレートが登録されていません。'), 'warning')
        return redirect(url_for('admin_templates'))

    created = skipped = 0
    for week_offset in range(4):
        for tmpl in templates:
            base = start + timedelta(weeks=week_offset)
            days_ahead = (tmpl.day_of_week - base.weekday()) % 7
            ev_date = base + timedelta(days=days_ahead)
            exists = Event.query.filter_by(
                title=tmpl.title, date=ev_date,
                start_time=tmpl.start_time, end_time=tmpl.end_time).first()
            if exists:
                skipped += 1
            else:
                db.session.add(Event(title=tmpl.title, date=ev_date,
                                     start_time=tmpl.start_time, end_time=tmpl.end_time))
                created += 1
    db.session.commit()
    flash(msg(f'Generated {created} events, skipped {skipped} duplicates.',
              f'{created}件生成、{skipped}件重複スキップ。'), 'success')
    return redirect(url_for('admin_templates'))

# ── Full Backup ────────────────────────────────────────────────────────────────

@app.route('/admin/backup')
@login_required('admin')
def admin_backup():
    counts = {
        'users':    User.query.count(),
        'events':   Event.query.count(),
        'attendance': Attendance.query.count(),
        'templates': WeeklyTemplate.query.count(),
    }
    return render_template('admin/backup.html', counts=counts)


@app.route('/admin/export/zip')
@login_required('admin')
def admin_export_zip():
    """全データを ZIP（CSV×4 + backup_info.json）でダウンロード。"""
    buf = io.BytesIO()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:

        # ── users.csv ──
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(['id', 'email', 'password_hash', 'name', 'grade', 'user_class',
                    'role', 'positions', 'auto_absent_days', 'must_change_password',
                    'failed_login_attempts', 'is_active', 'created_at'])
        for u in User.query.order_by(User.id).all():
            w.writerow([
                u.id, u.email, u.password_hash, u.name, u.grade or '',
                u.user_class or '', u.role,
                json.dumps(u.positions, ensure_ascii=False),
                json.dumps(u.auto_absent_days),
                int(u.must_change_password), u.failed_login_attempts,
                int(u.is_active),
                u.created_at.isoformat() if u.created_at else '',
            ])
        zf.writestr('users.csv', '﻿' + s.getvalue())

        # ── events.csv ──
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(['id', 'title', 'date', 'start_time', 'end_time', 'created_at'])
        for ev in Event.query.order_by(Event.date, Event.start_time).all():
            w.writerow([
                ev.id, ev.title, ev.date.isoformat(),
                ev.start_time.strftime('%H:%M'), ev.end_time.strftime('%H:%M'),
                ev.created_at.isoformat() if ev.created_at else '',
            ])
        zf.writestr('events.csv', '﻿' + s.getvalue())

        # ── attendance.csv ──
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(['id', 'user_id', 'user_name', 'event_id', 'event_title',
                    'event_date', 'status', 'partial_start', 'partial_end',
                    'comment', 'updated_at'])
        rows = (db.session.query(Attendance, User, Event)
                .join(User,  Attendance.user_id  == User.id)
                .join(Event, Attendance.event_id == Event.id)
                .order_by(Event.date, User.name).all())
        for att, u, ev in rows:
            w.writerow([
                att.id, att.user_id, u.name, att.event_id, ev.title,
                ev.date.isoformat(), att.status,
                att.partial_start.strftime('%H:%M') if att.partial_start else '',
                att.partial_end.strftime('%H:%M')   if att.partial_end   else '',
                att.comment or '',
                att.updated_at.isoformat() if att.updated_at else '',
            ])
        zf.writestr('attendance.csv', '﻿' + s.getvalue())

        # ── weekly_templates.csv ──
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(['id', 'title', 'day_of_week', 'day_name',
                    'start_time', 'end_time', 'is_auto', 'created_at'])
        for t in WeeklyTemplate.query.order_by(WeeklyTemplate.day_of_week).all():
            w.writerow([
                t.id, t.title, t.day_of_week, DAY_NAMES_JA[t.day_of_week],
                t.start_time.strftime('%H:%M'), t.end_time.strftime('%H:%M'),
                int(t.is_auto),
                t.created_at.isoformat() if t.created_at else '',
            ])
        zf.writestr('weekly_templates.csv', '﻿' + s.getvalue())

        # ── backup_info.json ──
        info = {
            'exported_at': datetime.utcnow().isoformat() + 'Z',
            'exported_by': g.current_user.name,
            'app': 'attendance_system',
            'counts': {
                'users':      User.query.count(),
                'events':     Event.query.count(),
                'attendance': Attendance.query.count(),
                'templates':  WeeklyTemplate.query.count(),
            },
        }
        zf.writestr('backup_info.json',
                    json.dumps(info, ensure_ascii=False, indent=2))

    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers['Content-Type'] = 'application/zip'
    resp.headers['Content-Disposition'] = f'attachment; filename=backup_{ts}.zip'
    return resp


@app.route('/admin/export/json_full')
@login_required('admin')
def admin_export_json_full():
    """全データを1つの JSON ファイルでダウンロード（インポート復元用）。"""

    def time_or_none(t):
        return t.strftime('%H:%M') if t else None

    users = [{
        'id': u.id, 'email': u.email, 'password_hash': u.password_hash,
        'name': u.name, 'grade': u.grade, 'user_class': u.user_class,
        'role': u.role, 'positions': u.positions,
        'auto_absent_days': u.auto_absent_days,
        'must_change_password': u.must_change_password,
        'failed_login_attempts': u.failed_login_attempts,
        'is_active': u.is_active,
        'created_at': u.created_at.isoformat() if u.created_at else None,
    } for u in User.query.order_by(User.id).all()]

    events = [{
        'id': ev.id, 'title': ev.title, 'date': ev.date.isoformat(),
        'start_time': ev.start_time.strftime('%H:%M'),
        'end_time':   ev.end_time.strftime('%H:%M'),
        'created_at': ev.created_at.isoformat() if ev.created_at else None,
    } for ev in Event.query.order_by(Event.date, Event.start_time).all()]

    attendance = [{
        'id': a.id, 'user_id': a.user_id, 'event_id': a.event_id,
        'status': a.status,
        'partial_start': time_or_none(a.partial_start),
        'partial_end':   time_or_none(a.partial_end),
        'comment': a.comment,
        'updated_at': a.updated_at.isoformat() if a.updated_at else None,
    } for a in Attendance.query.order_by(Attendance.id).all()]

    templates = [{
        'id': t.id, 'title': t.title, 'day_of_week': t.day_of_week,
        'start_time': t.start_time.strftime('%H:%M'),
        'end_time':   t.end_time.strftime('%H:%M'),
        'is_auto': t.is_auto,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    } for t in WeeklyTemplate.query.order_by(WeeklyTemplate.id).all()]

    payload = {
        'exported_at': datetime.utcnow().isoformat() + 'Z',
        'exported_by': g.current_user.name,
        'app': 'attendance_system',
        'users': users,
        'events': events,
        'attendance': attendance,
        'weekly_templates': templates,
    }

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    resp = make_response(json.dumps(payload, ensure_ascii=False, indent=2))
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=backup_{ts}.json'
    return resp


@app.route('/admin/export/sqlite_db')
@login_required('admin')
def admin_export_sqlite_db():
    """SQLite DB ファイルそのものをダウンロード。"""
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    if not db_uri.startswith('sqlite'):
        flash(msg('SQLite export is only available for SQLite databases.',
                  'SQLite以外のDBではこのエクスポートは使えません。'), 'warning')
        return redirect(url_for('admin_backup'))

    # URI から実ファイルパスを取得（sqlite:///相対 or 絶対）
    db_path = db_uri.replace('sqlite:///', '', 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.getcwd(), db_path)

    if not os.path.exists(db_path):
        flash(msg('Database file not found.', 'DBファイルが見つかりません。'), 'danger')
        return redirect(url_for('admin_backup'))

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    with open(db_path, 'rb') as f:
        data = f.read()

    resp = make_response(data)
    resp.headers['Content-Type'] = 'application/x-sqlite3'
    resp.headers['Content-Disposition'] = f'attachment; filename=attendance_{ts}.db'
    return resp

# ── API ────────────────────────────────────────────────────────────────────────

@app.route('/api/events')
@login_required()
def api_events():
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    q = Event.query
    if start:
        try:
            q = q.filter(Event.date >= datetime.fromisoformat(start[:10]).date())
        except ValueError:
            pass
    if end:
        try:
            q = q.filter(Event.date <= datetime.fromisoformat(end[:10]).date())
        except ValueError:
            pass

    user = db.session.get(User, session['user_id'])
    att_map = {a.event_id: a.status for a in
               Attendance.query.filter_by(user_id=user.id).all()}

    result = []
    for ev in q.order_by(Event.date, Event.start_time).all():
        d = ev.to_dict()
        status = att_map.get(ev.id, 'absent')
        d['color'] = {'present': '#28a745', 'partial': '#ffc107', 'absent': '#dc3545'}.get(
            status, '#6c757d')
        d['extendedProps'] = {'status': status}
        result.append(d)
    return jsonify(result)


@app.route('/api/attendance_by_date/<date_str>')
@login_required(['manager', 'admin'])
def api_attendance_by_date(date_str):
    try:
        sel_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    events = Event.query.filter_by(date=sel_date).order_by(Event.start_time).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    att_map = {}
    if events:
        for a in Attendance.query.filter(
                Attendance.event_id.in_([e.id for e in events])).all():
            att_map[(a.user_id, a.event_id)] = a

    result = []
    for u in users:
        ud = {'id': u.id, 'name': u.name, 'grade': u.grade, 'class': u.user_class, 'events': []}
        for e in events:
            a = att_map.get((u.id, e.id))
            ud['events'].append({
                'event_id': e.id, 'title': e.title,
                'status': a.status if a else 'absent',
                'comment': a.comment if a else None,
            })
        result.append(ud)
    return jsonify({'date': date_str, 'events': [e.to_dict() for e in events], 'users': result})


@app.route('/api/events/<int:eid>/move', methods=['POST'])
@login_required('admin')
def api_event_move(eid):
    event = db.session.get(Event, eid)
    if not event:
        return jsonify({'error': 'Not found'}), 404
    token = request.headers.get('X-CSRF-Token')
    if not token or token != session.get('_csrf_token'):
        return jsonify({'error': 'CSRF'}), 403
    data = request.get_json() or {}
    try:
        event.date = datetime.fromisoformat(data['date']).date()
        db.session.commit()
        return jsonify(event.to_dict())
    except (KeyError, ValueError):
        return jsonify({'error': 'Invalid data'}), 400


@app.route('/api/events/<int:eid>/inline_edit', methods=['POST'])
@login_required('admin')
def api_event_inline_edit(eid):
    event = db.session.get(Event, eid)
    if not event:
        return jsonify({'error': 'Not found'}), 404
    token = request.headers.get('X-CSRF-Token')
    if not token or token != session.get('_csrf_token'):
        return jsonify({'error': 'CSRF'}), 403
    data = request.get_json() or {}
    try:
        if 'title' in data:
            event.title = data['title'].strip()
        if 'start_time' in data:
            event.start_time = datetime.strptime(data['start_time'], '%H:%M').time()
        if 'end_time' in data:
            event.end_time = datetime.strptime(data['end_time'], '%H:%M').time()
        if 'date' in data:
            event.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
        if event.start_time >= event.end_time:
            return jsonify({'error': 'Start must be before end'}), 400
        db.session.commit()
        return jsonify(event.to_dict())
    except (ValueError, KeyError) as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/events/<int:eid>/duplicate', methods=['POST'])
@login_required('admin')
def api_event_duplicate(eid):
    event = db.session.get(Event, eid)
    if not event:
        return jsonify({'error': 'Not found'}), 404
    token = request.headers.get('X-CSRF-Token')
    if not token or token != session.get('_csrf_token'):
        return jsonify({'error': 'CSRF'}), 403
    data = request.get_json() or {}
    new_date_str = data.get('date')
    try:
        new_date = datetime.strptime(new_date_str, '%Y-%m-%d').date() if new_date_str else event.date
    except ValueError:
        new_date = event.date
    new_ev = Event(title=event.title, date=new_date,
                   start_time=event.start_time, end_time=event.end_time)
    db.session.add(new_ev)
    db.session.commit()
    return jsonify(new_ev.to_dict()), 201

# ── JWT / Flutter Mobile API (/api/v1/*) ──────────────────────────────────────

def jwt_role(min_role='user'):
    """JWT版ロールデコレータ。セッション認証とは独立して動作する。"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                verify_jwt_in_request()
            except Exception as e:
                return jsonify({'error': 'Token required', 'detail': str(e)}), 401
            uid = get_jwt_identity()
            user = db.session.get(User, int(uid))  # str/int 両対応
            if not user or not user.is_active:
                return jsonify({'error': 'User inactive'}), 401
            if ROLE_LEVEL.get(user.role, 0) < ROLE_LEVEL.get(min_role, 0):
                return jsonify({'error': 'Forbidden'}), 403
            g.jwt_user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator


def _user_dict(u):
    return {'id': u.id, 'email': u.email, 'name': u.name,
            'grade': u.grade, 'user_class': u.user_class,
            'role': u.role, 'positions': u.positions,
            'auto_absent_days': u.auto_absent_days,
            'discord_id': u.discord_id,
            'birthday': u.birthday.isoformat() if u.birthday else None,
            'must_change_password': u.must_change_password}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/api/v1/auth/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    user = User.query.filter_by(email=email, is_active=True).first()
    if user and user.is_locked():
        return jsonify({'error': 'Account locked'}), 423
    if user and user.check_password(password):
        user.failed_login_attempts = 0
        user.locked_until = None
        db.session.commit()
        return jsonify({
            'access_token':  create_access_token(identity=str(user.id)),
            'refresh_token': create_refresh_token(identity=str(user.id)),
            'user': _user_dict(user),
        })
    if user:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_ATTEMPTS:
            user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        db.session.commit()
    return jsonify({'error': 'Invalid credentials'}), 401


@app.route('/api/v1/auth/refresh', methods=['POST'])
@jwt_required(refresh=True)
def api_refresh():
    uid = get_jwt_identity()
    return jsonify({'access_token': create_access_token(identity=uid)})


@app.route('/api/v1/auth/me')
@jwt_role()
def api_me():
    return jsonify(_user_dict(g.jwt_user))


@app.route('/api/v1/auth/change_password', methods=['POST'])
@jwt_role()
def api_change_password():
    data = request.get_json() or {}
    user = g.jwt_user
    if not user.check_password(data.get('current_password', '')):
        return jsonify({'error': 'Wrong current password'}), 400
    new_pw = data.get('new_password', '')
    if len(new_pw) < 8:
        return jsonify({'error': 'Password too short'}), 400
    user.set_password(new_pw)
    user.must_change_password = False
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/v1/auth/onboarding', methods=['POST'])
@jwt_role()
def api_onboarding():
    """初回ログイン時のセットアップ。
    - 新パスワード(必須)
    - 学年 / 班 / 誕生日 / Discord ID (任意)
    完了すると must_change_password=False に。
    """
    data = request.get_json() or {}
    user = g.jwt_user

    new_pw = data.get('new_password', '')
    if len(new_pw) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    # current_password は強制変更状態なら省略可、通常変更なら必須
    if not user.must_change_password:
        if not user.check_password(data.get('current_password', '')):
            return jsonify({'error': 'Wrong current password'}), 400

    user.set_password(new_pw)
    user.must_change_password = False

    # 任意項目
    if 'grade' in data:
        g_in = (data.get('grade') or '').strip() if isinstance(data.get('grade'), str) else None
        user.grade = g_in if g_in in VALID_GRADES else None

    if 'positions' in data:
        positions = data.get('positions') or []
        if isinstance(positions, list):
            user.positions = [p for p in positions if p in VALID_POSITIONS]

    if 'birthday' in data:
        b = data.get('birthday')
        if not b:
            user.birthday = None
        else:
            try:
                user.birthday = datetime.strptime(str(b), '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'error': 'Invalid birthday format (YYYY-MM-DD)'}), 400

    if 'discord_id' in data:
        d_in = (data.get('discord_id') or '').strip() if isinstance(data.get('discord_id'), str) else ''
        user.discord_id = d_in if d_in.isdigit() else (None if not d_in else user.discord_id)

    db.session.commit()
    return jsonify({'ok': True, 'user': _user_dict(user)})

# ── Events ─────────────────────────────────────────────────────────────────────

@app.route('/api/v1/events')
@jwt_role()
def api_v1_events():
    start = request.args.get('start', '')
    end   = request.args.get('end', '')
    q = Event.query
    if start:
        try: q = q.filter(Event.date >= datetime.strptime(start, '%Y-%m-%d').date())
        except ValueError: pass
    if end:
        try: q = q.filter(Event.date <= datetime.strptime(end, '%Y-%m-%d').date())
        except ValueError: pass
    user = g.jwt_user
    att_map = {a.event_id: a for a in Attendance.query.filter_by(user_id=user.id).all()}
    result = []
    for ev in q.order_by(Event.date, Event.start_time).all():
        d = ev.to_dict()
        a = att_map.get(ev.id)
        d['my_status']        = a.status       if a else 'absent'
        d['my_comment']       = a.comment      if a else None
        d['my_partial_start'] = a.partial_start.strftime('%H:%M') if a and a.partial_start else None
        d['my_partial_end']   = a.partial_end.strftime('%H:%M')   if a and a.partial_end   else None
        result.append(d)
    return jsonify(result)


@app.route('/api/v1/events/upcoming')
@jwt_role()
def api_v1_events_upcoming():
    today = date.today()
    events = (Event.query
              .filter(Event.date >= today)
              .order_by(Event.date, Event.start_time)
              .limit(60).all())
    user = g.jwt_user
    att_map = {a.event_id: a for a in Attendance.query.filter_by(user_id=user.id).all()}
    result = []
    for ev in events:
        d = ev.to_dict()
        a = att_map.get(ev.id)
        d['my_status']  = a.status  if a else 'absent'
        d['my_comment'] = a.comment if a else None
        result.append(d)
    return jsonify(result)


@app.route('/api/v1/events', methods=['POST'])
@jwt_role('manager')
def api_v1_add_event():
    data = request.get_json() or {}
    try:
        d  = datetime.strptime(data['date'],       '%Y-%m-%d').date()
        st = datetime.strptime(data['start_time'], '%H:%M').time()
        et = datetime.strptime(data['end_time'],   '%H:%M').time()
    except (KeyError, ValueError):
        return jsonify({'error': 'Invalid data'}), 400
    if st >= et:
        return jsonify({'error': 'Start must be before end'}), 400
    ev = Event(title=data.get('title', '').strip(),
               description=data.get('description', '').strip(),
               date=d, start_time=st, end_time=et)
    db.session.add(ev)
    db.session.commit()
    discord_service.notify_event_added(ev)
    return jsonify(ev.to_dict()), 201


@app.route('/api/v1/events/<int:eid>', methods=['PUT'])
@jwt_role('admin')
def api_v1_edit_event(eid):
    ev = db.session.get(Event, eid)
    if not ev: return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    try:
        if 'date'       in data: ev.date       = datetime.strptime(data['date'],       '%Y-%m-%d').date()
        if 'start_time' in data: ev.start_time = datetime.strptime(data['start_time'], '%H:%M').time()
        if 'end_time'   in data: ev.end_time   = datetime.strptime(data['end_time'],   '%H:%M').time()
        if 'title'       in data: ev.title       = data['title'].strip()
        if 'description' in data: ev.description = data['description'].strip()
    except ValueError:
        return jsonify({'error': 'Invalid data'}), 400
    if ev.start_time >= ev.end_time:
        return jsonify({'error': 'Start must be before end'}), 400
    db.session.commit()
    discord_service.notify_event_updated(ev)
    return jsonify(ev.to_dict())


@app.route('/api/v1/events/<int:eid>', methods=['DELETE'])
@jwt_role('admin')
def api_v1_delete_event(eid):
    ev = db.session.get(Event, eid)
    if not ev: return jsonify({'error': 'Not found'}), 404
    title_snap = ev.title
    date_snap  = ev.date.isoformat()
    db.session.delete(ev)
    db.session.commit()
    discord_service.notify_event_deleted(title_snap, date_snap)
    return jsonify({'ok': True})

# ── Attendance ─────────────────────────────────────────────────────────────────

@app.route('/api/v1/attendance/my')
@jwt_role()
def api_v1_my_attendance():
    user = g.jwt_user
    rows = (db.session.query(Attendance, Event)
            .join(Event, Attendance.event_id == Event.id)
            .filter(Attendance.user_id == user.id)
            .order_by(Event.date.desc(), Event.start_time.desc()).all())
    result = []
    for a, ev in rows:
        d = ev.to_dict()
        d['status']        = a.status
        d['comment']       = a.comment
        d['partial_start'] = a.partial_start.strftime('%H:%M') if a.partial_start else None
        d['partial_end']   = a.partial_end.strftime('%H:%M')   if a.partial_end   else None
        result.append(d)
    return jsonify(result)


@app.route('/api/v1/attendance/update', methods=['POST'])
@jwt_role()
def api_v1_update_attendance():
    user = g.jwt_user
    data = request.get_json() or {}
    eid    = data.get('event_id')
    status = data.get('status', 'absent')
    if not eid or status not in ('present', 'absent', 'partial'):
        return jsonify({'error': 'Invalid data'}), 400
    if not db.session.get(Event, eid):
        return jsonify({'error': 'Event not found'}), 404
    att = Attendance.query.filter_by(user_id=user.id, event_id=eid).first()
    if not att:
        att = Attendance(user_id=user.id, event_id=eid)
        db.session.add(att)
    att.status     = status
    att.comment    = data.get('comment') or None
    att.updated_at = datetime.utcnow()
    if status == 'partial':
        try:
            ps = data.get('partial_start')
            pe = data.get('partial_end')
            att.partial_start = datetime.strptime(ps, '%H:%M').time() if ps else None
            att.partial_end   = datetime.strptime(pe, '%H:%M').time() if pe else None
        except (ValueError, TypeError):
            pass
    else:
        att.partial_start = att.partial_end = None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/v1/attendance/date/<date_str>')
@jwt_role('manager')
def api_v1_attendance_date(date_str):
    try: sel_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError: return jsonify({'error': 'Invalid date'}), 400
    events = Event.query.filter_by(date=sel_date).order_by(Event.start_time).all()
    users  = User.query.filter_by(is_active=True).order_by(User.name).all()
    att_map = {}
    if events:
        for a in Attendance.query.filter(
                Attendance.event_id.in_([e.id for e in events])).all():
            att_map[(a.user_id, a.event_id)] = a
    result = []
    for u in users:
        ud = {**_user_dict(u), 'events': []}
        for ev in events:
            a = att_map.get((u.id, ev.id))
            ud['events'].append({
                'event_id': ev.id, 'title': ev.title,
                'status':   a.status  if a else 'absent',
                'comment':  a.comment if a else None,
            })
        result.append(ud)
    return jsonify({'date': date_str,
                    'events': [e.to_dict() for e in events],
                    'users':  result})


@app.route('/api/v1/attendance/bulk', methods=['POST'])
@jwt_role('manager')
def api_v1_bulk_attendance():
    items = request.get_json()
    if not isinstance(items, list):
        return jsonify({'error': 'Expected list'}), 400
    for item in items:
        uid = item.get('user_id'); eid = item.get('event_id')
        status = item.get('status', 'absent')
        if not uid or not eid or status not in ('present', 'absent', 'partial'): continue
        att = Attendance.query.filter_by(user_id=uid, event_id=eid).first()
        if not att:
            att = Attendance(user_id=uid, event_id=eid); db.session.add(att)
        att.status = status; att.comment = item.get('comment') or None
        att.updated_at = datetime.utcnow()
        if status == 'partial':
            try:
                att.partial_start = datetime.strptime(item['partial_start'], '%H:%M').time() if item.get('partial_start') else None
                att.partial_end   = datetime.strptime(item['partial_end'],   '%H:%M').time() if item.get('partial_end')   else None
            except (ValueError, KeyError): pass
        else:
            att.partial_start = att.partial_end = None
    db.session.commit()
    return jsonify({'ok': True})

# ── Users (admin) ──────────────────────────────────────────────────────────────

@app.route('/api/v1/users')
@jwt_role('manager')
def api_v1_users():
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    return jsonify([_user_dict(u) for u in users])


@app.route('/api/v1/users', methods=['POST'])
@jwt_role('admin')
def api_v1_add_user():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'error': 'Invalid email'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already in use'}), 409
    temp_pw = secrets.token_urlsafe(12)
    g_in = (data.get('grade') or '').strip() if isinstance(data.get('grade'), str) else None
    u = User(email=email, name=data.get('name', '').strip(),
             grade=g_in if g_in in VALID_GRADES else None,
             user_class=data.get('user_class', ''),
             role=data.get('role', 'user') if data.get('role') in VALID_ROLES else 'user',
             must_change_password=True)
    u.set_password(temp_pw)
    u.positions       = [p for p in data.get('positions', []) if p in VALID_POSITIONS]
    u.auto_absent_days = data.get('auto_absent_days', [])
    db.session.add(u); db.session.commit()
    return jsonify({**_user_dict(u), 'temp_password': temp_pw}), 201


@app.route('/api/v1/users/<int:uid>', methods=['PUT'])
@jwt_role('admin')
def api_v1_edit_user(uid):
    u = db.session.get(User, uid)
    if not u: return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    if 'email' in data:
        email = data['email'].strip().lower()
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            return jsonify({'error': 'Invalid email'}), 400
        if User.query.filter(User.email == email, User.id != uid).first():
            return jsonify({'error': 'Email already in use'}), 409
        u.email = email
    if 'name'             in data: u.name       = data['name'].strip()
    if 'grade' in data:
        g_in = data.get('grade')
        if g_in is None or g_in == '':
            u.grade = None
        elif isinstance(g_in, str) and g_in.strip() in VALID_GRADES:
            u.grade = g_in.strip()
        # それ以外は無視 (不正値)
    if 'user_class'       in data: u.user_class = data.get('user_class', '')
    if 'role'             in data and data['role'] in VALID_ROLES: u.role = data['role']
    if 'positions'        in data: u.positions       = [p for p in data['positions'] if p in VALID_POSITIONS]
    if 'auto_absent_days' in data: u.auto_absent_days = data['auto_absent_days']
    db.session.commit()
    return jsonify(_user_dict(u))


@app.route('/api/v1/users/<int:uid>', methods=['DELETE'])
@jwt_role('admin')
def api_v1_delete_user(uid):
    u = db.session.get(User, uid)
    if not u: return jsonify({'error': 'Not found'}), 404
    if u.id == g.jwt_user.id: return jsonify({'error': 'Cannot delete yourself'}), 400
    u.is_active = False; db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/v1/users/<int:uid>/reset_password', methods=['POST'])
@jwt_role('manager')
def api_v1_reset_password(uid):
    u = db.session.get(User, uid)
    if not u: return jsonify({'error': 'Not found'}), 404
    d = request.get_json() or {}
    temp_pw = (d.get('password') or '').strip() or secrets.token_urlsafe(12)
    u.set_password(temp_pw); u.must_change_password = True
    u.failed_login_attempts = 0; u.locked_until = None
    db.session.commit()
    return jsonify({'temp_password': temp_pw})

# ── Templates API ─────────────────────────────────────────────────────────────

@app.route('/api/v1/templates')
@jwt_role('manager')
def api_v1_get_templates():
    ts = WeeklyTemplate.query.order_by(WeeklyTemplate.day_of_week, WeeklyTemplate.start_time).all()
    return jsonify([{
        'id': t.id, 'title': t.title, 'day_of_week': t.day_of_week,
        'start_time': t.start_time.strftime('%H:%M'),
        'end_time':   t.end_time.strftime('%H:%M'),
        'is_auto': t.is_auto,
    } for t in ts])


@app.route('/api/v1/templates', methods=['POST'])
@jwt_role('manager')
def api_v1_add_template():
    d = request.get_json() or {}
    title = (d.get('title') or '').strip()
    dow   = d.get('day_of_week')
    is_auto = bool(d.get('is_auto', False))
    if not title or dow is None or dow not in range(7):
        return jsonify({'error': 'Invalid input'}), 400
    try:
        st = datetime.strptime(d.get('start_time', ''), '%H:%M').time()
        et = datetime.strptime(d.get('end_time',   ''), '%H:%M').time()
    except ValueError:
        return jsonify({'error': 'Invalid time format'}), 400
    if st >= et:
        return jsonify({'error': 'Start must be before end'}), 400
    t = WeeklyTemplate(title=title, day_of_week=dow, start_time=st, end_time=et, is_auto=is_auto)
    db.session.add(t); db.session.commit()
    return jsonify({'id': t.id, 'title': t.title, 'day_of_week': t.day_of_week,
                    'start_time': t.start_time.strftime('%H:%M'),
                    'end_time': t.end_time.strftime('%H:%M'), 'is_auto': t.is_auto}), 201


@app.route('/api/v1/templates/<int:tid>', methods=['PUT'])
@jwt_role('manager')
def api_v1_edit_template(tid):
    t = db.session.get(WeeklyTemplate, tid)
    if not t: return jsonify({'error': 'Not found'}), 404
    d = request.get_json() or {}
    try:
        st = datetime.strptime(d.get('start_time', t.start_time.strftime('%H:%M')), '%H:%M').time()
        et = datetime.strptime(d.get('end_time',   t.end_time.strftime('%H:%M')),   '%H:%M').time()
    except ValueError:
        return jsonify({'error': 'Invalid time format'}), 400
    dow = d.get('day_of_week', t.day_of_week)
    if dow not in range(7): return jsonify({'error': 'Invalid day'}), 400
    if st >= et: return jsonify({'error': 'Start must be before end'}), 400
    t.title = (d.get('title') or t.title).strip()
    t.day_of_week = dow; t.start_time = st; t.end_time = et
    t.is_auto = bool(d.get('is_auto', t.is_auto))
    db.session.commit()
    return jsonify({'id': t.id, 'title': t.title, 'day_of_week': t.day_of_week,
                    'start_time': t.start_time.strftime('%H:%M'),
                    'end_time': t.end_time.strftime('%H:%M'), 'is_auto': t.is_auto})


@app.route('/api/v1/templates/<int:tid>', methods=['DELETE'])
@jwt_role('admin')
def api_v1_delete_template(tid):
    t = db.session.get(WeeklyTemplate, tid)
    if not t: return jsonify({'error': 'Not found'}), 404
    db.session.delete(t); db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/v1/templates/generate', methods=['POST'])
@jwt_role('manager')
def api_v1_generate_from_templates():
    d = request.get_json() or {}
    try:
        start = datetime.strptime(d.get('start_date', ''), '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400
    weeks = max(1, min(int(d.get('weeks', 4)), 52))
    templates = WeeklyTemplate.query.all()
    if not templates:
        return jsonify({'error': 'No templates defined'}), 400
    created = 0
    for i in range(weeks * 7):
        day = start + timedelta(days=i)
        for t in templates:
            if t.day_of_week == day.weekday():
                exists = Event.query.filter_by(
                    date=day, start_time=t.start_time, title=t.title).first()
                if not exists:
                    ev = Event(title=t.title, date=day,
                               start_time=t.start_time, end_time=t.end_time)
                    db.session.add(ev); created += 1
    db.session.commit()
    return jsonify({'created': created})


# ── Stats API ──────────────────────────────────────────────────────────────────

@app.route('/api/v1/stats')
@jwt_role('manager')
def api_v1_stats():
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    total = Event.query.count()
    result = []
    for u in users:
        c = defaultdict(int)
        for a in u.attendances:
            c[a.status] += 1
        attended = c['present'] + c['partial']
        result.append({
            'id': u.id, 'name': u.name, 'grade': u.grade,
            'user_class': u.user_class, 'positions': u.positions,
            'present': c['present'], 'partial': c['partial'], 'absent': c['absent'],
            'total': total,
            'rate': round(attended / total, 3) if total else 0.0,
        })
    return jsonify({'total_events': total, 'users': result})


# ── App Version (OTA) ─────────────────────────────────────────────────────────

@app.route('/api/v1/app/latest')
def api_app_latest():
    """モバイルアプリの最新バージョン情報を返す (認証不要 - 起動時チェック用)"""
    path = os.path.join(os.path.dirname(__file__), 'app_version.json')
    if not os.path.exists(path):
        return jsonify({'error': 'Version file missing'}), 404
    try:
        with open(path, encoding='utf-8') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({'error': f'Invalid version file: {e}'}), 500


# ── Guide ──────────────────────────────────────────────────────────────────────

@app.route('/guide')
@login_required()
def guide():
    return render_template('guide.html')

# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403,
                           message=msg('Access Denied', 'アクセスが拒否されました')), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404,
                           message=msg('Page Not Found', 'ページが見つかりません')), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500,
                           message=msg('Internal Server Error', 'サーバーエラーが発生しました')), 500

# ── CLI commands ───────────────────────────────────────────────────────────────

@app.cli.command('init_db')
def init_db():
    """Initialize the database."""
    db.create_all()
    click.echo('Database initialized.')


@app.cli.command('sync_discord_roles')
@click.option('--dry-run', is_flag=True, help='実行せず結果だけ表示')
def sync_discord_roles(dry_run):
    """全ユーザの出席率に応じて Discord ロールを更新 (cron向け)。

    使用例 (Coolify Scheduled Task など):
      flask sync_discord_roles
      flask sync_discord_roles --dry-run
    """
    if not discord_service.is_bot_enabled():
        click.echo('[skip] Bot disabled (DISCORD_BOT_TOKEN/DISCORD_GUILD_ID 未設定)')
        return
    total = Event.query.count()
    if total == 0:
        click.echo('[skip] No events yet — nothing to score')
        return

    users = User.query.filter(
        User.is_active == True,
        User.discord_id.isnot(None)
    ).all()
    if not users:
        click.echo('[skip] No users with discord_id')
        return

    click.echo(f'Syncing roles for {len(users)} users (total events: {total})')
    for u in users:
        c_present = sum(1 for a in u.attendances if a.status == 'present')
        c_partial = sum(1 for a in u.attendances if a.status == 'partial')
        rate = (c_present + c_partial) / total if total else 0.0
        tier = 'HIGH' if rate >= 0.8 else ('MID' if rate >= 0.6 else 'LOW')
        if dry_run:
            click.echo(f'  [dry] {u.name} ({u.discord_id}): {rate*100:.1f}% → {tier}')
        else:
            ok, msg_ = discord_service.sync_attendance_roles(rate, u.discord_id)
            mark = '✓' if ok else '✗'
            click.echo(f'  {mark} {u.name} ({u.discord_id}): {rate*100:.1f}% → {tier} ({msg_})')
    click.echo('Done.')


@app.cli.command('create_admin')
@click.option('--email', prompt='Admin email')
@click.option('--password', prompt='Password', hide_input=True, confirmation_prompt=True)
@click.option('--name', prompt='Display name')
def create_admin(email, password, name):
    """Create an admin user."""
    db.create_all()
    if User.query.filter_by(email=email.lower()).first():
        click.echo('Error: email already exists.')
        return
    u = User(email=email.lower(), name=name, role='admin', must_change_password=False)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    click.echo(f'Admin "{name}" ({email}) created.')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
