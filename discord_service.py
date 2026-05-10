"""Discord連携サービス
- Webhook: チャンネルへの通知投稿 (簡単・サーバー認証不要)
- Bot API: ロール付与など (Bot Token とロールID必要)

環境変数で全機能を制御。設定なしの場合は no-op (アプリ動作に影響しない)。
"""
import json
import os
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── 設定 ──────────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL  = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
DISCORD_BOT_TOKEN    = os.environ.get('DISCORD_BOT_TOKEN', '').strip()
DISCORD_GUILD_ID     = os.environ.get('DISCORD_GUILD_ID', '').strip()

# 出席率に応じたロールID (任意設定)
DISCORD_ROLE_HIGH    = os.environ.get('DISCORD_ROLE_HIGH', '').strip()  # 80%以上
DISCORD_ROLE_MID     = os.environ.get('DISCORD_ROLE_MID', '').strip()   # 60-80%
DISCORD_ROLE_LOW     = os.environ.get('DISCORD_ROLE_LOW', '').strip()   # 60%未満

# 通知用アプリURL (リンクボタン埋め込み用)
APP_PUBLIC_URL       = os.environ.get('APP_PUBLIC_URL', 'https://zenshin9498.duckdns.org').rstrip('/')

# ── Webhook 通知 ──────────────────────────────────────────────────────────────

def is_webhook_enabled():
    return bool(DISCORD_WEBHOOK_URL)


def send_webhook(content=None, embeds=None, username='出欠管理BOT'):
    """Webhook で Discord チャンネルに投稿。失敗してもアプリは止めない。"""
    if not DISCORD_WEBHOOK_URL:
        return False
    payload = {'username': username}
    if content: payload['content'] = content
    if embeds:  payload['embeds']  = embeds
    try:
        req = Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        urlopen(req, timeout=5)
        return True
    except (URLError, OSError):
        return False


def notify_event_added(event):
    send_webhook(embeds=[_event_embed(event, '🆕 新しい活動が追加されました', 0x1976D2)])


def notify_event_updated(event):
    send_webhook(embeds=[_event_embed(event, '✏️ 活動が更新されました', 0xFF9800)])


def notify_event_deleted(title, date_str):
    send_webhook(embeds=[{
        'title': '🗑️ 活動が削除されました',
        'color': 0xE53935,
        'description': f'**{title}**\n📅 {date_str}',
    }])


def notify_release(version, notes, forced=False):
    icon = '🚨' if forced else '🆕'
    label = '必須' if forced else '任意'
    send_webhook(embeds=[{
        'title': f'{icon} アプリ {label}アップデート v{version}',
        'color': 0xE53935 if forced else 0x4CAF50,
        'description': notes or '更新内容の詳細はGitHub Releaseを確認',
        'fields': [{
            'name': 'ダウンロード',
            'value': f'[GitHub Release](https://github.com/Himanaraba/attendance-app/releases/tag/v{version})',
        }],
    }])


def _event_embed(event, title, color):
    days = ['月', '火', '水', '木', '金', '土', '日']
    dow = days[event.date.weekday()]
    fields = [
        {'name': '📅 日付',
         'value': f'{event.date.strftime("%Y-%m-%d")} ({dow})', 'inline': True},
        {'name': '⏰ 時間',
         'value': f'{event.start_time.strftime("%H:%M")}–{event.end_time.strftime("%H:%M")}',
         'inline': True},
    ]
    return {
        'title': title,
        'color': color,
        'description': f'**{event.title}**' +
                       (f'\n{event.description}' if getattr(event, 'description', None) else ''),
        'fields': fields,
        'footer': {'text': APP_PUBLIC_URL},
    }


# ── Bot API (ロール操作) ──────────────────────────────────────────────────────

def is_bot_enabled():
    return bool(DISCORD_BOT_TOKEN and DISCORD_GUILD_ID)


def _bot_request(method, path, payload=None):
    url = f'https://discord.com/api/v10{path}'
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type':  'application/json',
        'User-Agent':    'AttendanceBot (https://github.com/Himanaraba/attendance-system, 1.0)',
    }
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except Exception as e:
        return 0, str(e).encode('utf-8')


def add_role(discord_user_id, role_id):
    """指定ユーザに役職を付与"""
    if not is_bot_enabled() or not role_id:
        return False
    status, _ = _bot_request(
        'PUT', f'/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}/roles/{role_id}')
    return 200 <= status < 300


def remove_role(discord_user_id, role_id):
    """指定ユーザから役職を削除"""
    if not is_bot_enabled() or not role_id:
        return False
    status, _ = _bot_request(
        'DELETE', f'/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}/roles/{role_id}')
    return 200 <= status < 300


def sync_attendance_roles(rate, discord_user_id):
    """出席率に応じてロールを切り替える。
    HIGH: 80%以上 / MID: 60-80% / LOW: 60%未満
    対応するロールを付与し、他は外す。"""
    if not is_bot_enabled() or not discord_user_id:
        return False, 'bot disabled or no discord_id'

    if rate >= 0.8:
        target = DISCORD_ROLE_HIGH
    elif rate >= 0.6:
        target = DISCORD_ROLE_MID
    else:
        target = DISCORD_ROLE_LOW

    others = [r for r in (DISCORD_ROLE_HIGH, DISCORD_ROLE_MID, DISCORD_ROLE_LOW)
              if r and r != target]

    # 古いロールを外す
    for r in others:
        remove_role(discord_user_id, r)
    # 新しいロールを付ける
    if target:
        ok = add_role(discord_user_id, target)
        return ok, f'set role {target}'
    return True, 'no role configured'
