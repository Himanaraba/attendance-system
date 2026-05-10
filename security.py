"""セキュリティ関連のユーティリティ
- 監査ログ
- 強パスワードポリシー
- ログイン通知 (Discord)
"""
import hashlib
import json
import re
import urllib.error
from urllib.request import Request, urlopen

from flask import request, has_request_context
from models import db, AuditLog
import discord_service


# ── 監査ログ ──────────────────────────────────────────────────────────────────

def audit(action, user=None, target_type=None, target_id=None, detail=None):
    """主要アクションを audit_logs に記録。エラーは握り潰してアプリは止めない。"""
    try:
        ip = ua = None
        if has_request_context():
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
            ua = (request.headers.get('User-Agent') or '')[:255]
        log = AuditLog(
            user_id=user.id if user else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip=ip,
            user_agent=ua,
            detail=json.dumps(detail, ensure_ascii=False) if detail else None,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        try: db.session.rollback()
        except Exception: pass


# ── パスワードポリシー ────────────────────────────────────────────────────────

# 簡易共通弱パスワードリスト (本番では haveibeenpwned API も併用)
_COMMON = {
    'password', 'password1', '12345678', '123456789', 'qwerty123',
    'abc12345', 'admin1234', 'welcome1', 'iloveyou', 'monkey123',
    'football1', 'baseball1', 'sunshine1', 'master123', 'letmein1',
    'passw0rd', 'p@ssw0rd', '11111111', 'aaaaaaaa', 'qwertyui',
    'attendance', 'shukketsu',
}


def check_password_strength(pw, user_email=None):
    """強パスワードポリシーチェック。
    OK の場合は None、NG の場合はエラーメッセージ文字列を返す。
    """
    if not pw or len(pw) < 12:
        return '12文字以上で設定してください'
    if len(pw) > 128:
        return '128文字以下で設定してください'

    # 文字種チェック (3種類以上)
    classes = sum([
        bool(re.search(r'[a-z]', pw)),
        bool(re.search(r'[A-Z]', pw)),
        bool(re.search(r'\d',    pw)),
        bool(re.search(r'[^a-zA-Z\d]', pw)),
    ])
    if classes < 3:
        return '英大文字・英小文字・数字・記号のうち3種類以上を含めてください'

    # 共通弱パスワードチェック (ローカル辞書)
    if pw.lower() in _COMMON:
        return 'よく使われる弱いパスワードです。別のものを使用してください'

    # メアドの一部を含んでいないか
    if user_email:
        local = user_email.split('@')[0].lower()
        if local and len(local) >= 4 and local in pw.lower():
            return 'メールアドレスの一部を含めないでください'

    # haveibeenpwned k-anonymity 漏洩チェック
    try:
        sha1 = hashlib.sha1(pw.encode('utf-8')).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]
        req = Request(
            f'https://api.pwnedpasswords.com/range/{prefix}',
            headers={'User-Agent': 'AttendanceApp-PasswordCheck/1.0'},
        )
        with urlopen(req, timeout=3) as resp:
            for line in resp.read().decode('ascii').splitlines():
                hash_suffix, _count = line.split(':')
                if hash_suffix.strip() == suffix:
                    return 'このパスワードは過去に漏洩したことがあります。別のものを使用してください'
    except (urllib.error.URLError, OSError, TimeoutError):
        pass  # ネットワーク不通でも login 自体は通す
    except Exception:
        pass

    return None  # OK


# ── ログイン通知 ──────────────────────────────────────────────────────────────

def notify_login(user, success=True, reason=None):
    """ログイン成功/失敗を Discord 通知。Webhook 設定がなければ no-op。"""
    if not discord_service.is_webhook_enabled():
        return
    ip = '?'
    ua = '?'
    if has_request_context():
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
        ua = (request.headers.get('User-Agent') or '?')[:120]

    if success:
        title  = '🟢 ログイン成功'
        color  = 0x4CAF50
    else:
        title  = '🔴 ログイン失敗'
        color  = 0xE53935

    fields = [{'name': 'IP', 'value': ip, 'inline': True}]
    if user:
        fields.insert(0, {'name': 'ユーザー', 'value': f'{user.name} ({user.email})', 'inline': False})
    if reason:
        fields.append({'name': '理由', 'value': reason, 'inline': False})
    fields.append({'name': 'User-Agent', 'value': f'`{ua}`', 'inline': False})

    discord_service.send_webhook(embeds=[{
        'title': title,
        'color': color,
        'fields': fields,
    }], username='出欠管理BOT/Auth')
