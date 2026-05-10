# 部活動出欠管理システム (Flask Backend)

部活動向けの出欠管理 Web アプリ + REST API。Flutter モバイルクライアント ([attendance-app](https://github.com/Himanaraba/attendance-app)) と JWT 認証で連携。

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask)](https://flask.palletsprojects.com/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

## アーキテクチャ

```
┌──────────┐     セッション     ┌──────────────┐
│ Browser  │ ───── + CSRF ────▶ │   Flask 3.x  │
└──────────┘                    │              │      ┌──────────┐
                                │  Web (HTML)  │ ───▶ │ SQLite   │
┌──────────┐         JWT        │  + REST API  │      │ (volume) │
│ Flutter  │ ─────────────────▶ │              │      └──────────┘
└──────────┘                    └──────────────┘
                                       │
                              ┌────────┴────────┐
                              │ Coolify (VPS)   │
                              │ + Traefik+LE    │
                              │ + GitHub Auto   │
                              │   Deploy        │
                              └─────────────────┘
```

- **Web版**: Bootstrap 5 + FullCalendar、セッション + CSRF 認証
- **Mobile版**: JWT (Access 2h / Refresh 30d) アクセストークン認証

## 主な機能

### セキュリティ
- PBKDF2-SHA256 (600,000 iterations) パスワードハッシュ
- 初回ログイン強制パスワード変更
- 5回失敗で 30 分アカウントロック
- CSRF トークン (Web)、JWT (API)
- セキュリティヘッダー一式自動付与
- HTTPS リダイレクト (Traefik + Let's Encrypt)
- JWT 秘密鍵の永続化 (`.jwt_secret` ファイル)

### ユーザー管理
- ロール: `user` / `manager` / `admin`
- 班: 技術班 / 運営班 / 顧問
- 学年: M1〜M3 / H1〜H4 / OB (任意)
- 自動欠席曜日設定

### 出欠
- 出席 / 部分参加 / 欠席 + 任意コメント
- 部分参加時刻記録
- 個人ダッシュボード (出席率・直近活動)
- 管理者: 日付別一括編集

### 活動管理
- CRUD + 重複・移動操作 (FullCalendar連携)
- 週次テンプレートから自動生成
- 説明文サポート

### エクスポート
- CSV (日別 / 期間別 / 個人 / 統計サマリ)
- 全データバックアップ (ZIP / JSON / SQLite DB ファイル)

### 国際化
- 日本語 / English (セッション保存)

### モバイル連携
- `/api/v1/*` REST API
- `/api/v1/app/latest` で OTA アップデート情報配信
- CORS は `/api/v1/*` のみ許可

## 環境変数

| 変数 | デフォルト | 説明 |
|---|---|---|
| `SECRET_KEY` | ランダム生成 | Flask セッション用 (本番では固定値推奨) |
| `JWT_SECRET_KEY` | `.jwt_secret` ファイル | JWT 署名用 (ファイル自動永続化) |
| `DATABASE_URL` | `sqlite:///attendance.db` | DB接続 URL |
| `MAX_LOGIN_ATTEMPTS` | `5` | アカウントロックしきい値 |
| `LOCKOUT_MINUTES` | `30` | ロック時間 |

## ローカル開発

```bash
# 1. 仮想環境
python -m venv venv
.\venv\Scripts\activate    # Windows
source venv/bin/activate    # Mac/Linux

# 2. 依存
pip install -r requirements.txt

# 3. DB 初期化
flask --app app init_db

# 4. 管理者作成
flask --app app create_admin

# 5. 起動
python app.py
# または
flask --app app run --debug --host=0.0.0.0
```

http://localhost:5000 でアクセス。

## 本番デプロイ (Coolify)

このリポジトリは **Coolify セルフホスト PaaS** で自動デプロイされます。

```
git push origin main
   │
   ▼
GitHub webhook
   │
   ▼
Coolify ビルド (Nixpacks: Procfile + runtime.txt 自動検出)
   │
   ▼
Docker コンテナ再作成 (永続ボリューム /app/data に SQLite)
   │
   ▼
Traefik 経由で HTTPS 配信 (Let's Encrypt 自動更新)
```

### 必要ファイル

- `Procfile`: `web: gunicorn -b 0.0.0.0:${PORT:-5000} --workers 2 --timeout 60 app:app`
- `runtime.txt`: `python-3.12`
- `requirements.txt`: 依存パッケージ (gunicorn 含む)

### Coolify 設定

- Build Pack: **Nixpacks**
- Port: `5000`
- Storage: `/app/data` (永続ボリューム、SQLite DB ファイル用)
- Environment Variables: 上記 `SECRET_KEY` 等
- Auto Deploy: ON (GitHub App 連携)

## プロジェクト構成

```
attendance_system/
├── app.py                    # メインアプリ (~1900行、ルート定義)
├── models.py                 # SQLAlchemy: User / Event / Attendance / WeeklyTemplate
├── requirements.txt
├── Procfile                  # gunicorn 起動コマンド (Coolify用)
├── runtime.txt               # Python バージョン指定
├── app_version.json          # モバイルアプリの最新バージョン情報
├── deploy.sh                 # 手動デプロイ用 (バックアップ + git pull + 再起動)
├── attendance.db             # SQLite (gitignore、Coolifyボリュームで永続化)
├── .jwt_secret               # JWT 鍵 (gitignore、自動生成)
├── static/
│   ├── css/style.css
│   └── js/app.js
└── templates/                # Jinja2
    ├── base.html
    ├── login.html
    ├── dashboard.html
    ├── change_password.html
    ├── guide.html
    ├── error.html
    └── admin/
        ├── users.html
        ├── events.html
        ├── attendance.html
        ├── templates.html
        └── backup.html
```

## API エンドポイント (JWT)

### 認証
| Method | Path | 説明 |
|---|---|---|
| POST | `/api/v1/auth/login` | email + password でトークン取得 |
| POST | `/api/v1/auth/refresh` | refresh_token でアクセストークン更新 |
| GET  | `/api/v1/auth/me` | 自分のユーザー情報 |
| POST | `/api/v1/auth/change_password` | パスワード変更 |

### 活動・出欠 (一般ユーザ)
| Method | Path | 説明 |
|---|---|---|
| GET  | `/api/v1/events` | 全活動 (期間フィルタ可) |
| GET  | `/api/v1/events/upcoming` | 直近の活動 |
| GET  | `/api/v1/attendance/my` | 自分の出欠記録 |
| POST | `/api/v1/attendance/update` | 自分の出欠を更新 |

### 管理者向け (`@jwt_role('manager')`)
| Method | Path | 説明 |
|---|---|---|
| POST/PUT/DELETE | `/api/v1/events*` | 活動 CRUD |
| GET  | `/api/v1/attendance/date/<date>` | 日付別全員の出欠 |
| POST | `/api/v1/attendance/bulk` | 一括更新 |
| GET/POST/PUT/DELETE | `/api/v1/users*` | ユーザー管理 |
| GET/POST/PUT/DELETE | `/api/v1/templates*` | テンプレート管理 |
| POST | `/api/v1/templates/generate` | テンプレートからイベント一括生成 |
| GET  | `/api/v1/stats` | 全ユーザー出席統計 |

### モバイル OTA
| Method | Path | 説明 |
|---|---|---|
| GET | `/api/v1/app/latest` | 最新版情報 (認証不要) |

## DB マイグレーション

`apply_migrations()` (in `app.py`) で起動時に自動実行：

```python
_safe_add_column('weekly_templates', 'is_auto', 'BOOLEAN DEFAULT 0')
_safe_add_column('events', 'description', 'TEXT DEFAULT ""')
# ALTER TABLE が既に当たっていれば例外を握りつぶす
```

新カラム追加時はここに 1 行足してデプロイするだけ。

## セキュリティヘッダー (自動付与)

| ヘッダー | 値 |
|---|---|
| X-Content-Type-Options | nosniff |
| X-Frame-Options | DENY |
| X-XSS-Protection | 1; mode=block |
| Referrer-Policy | strict-origin-when-cross-origin |
| Permissions-Policy | geolocation=(), camera=(), microphone=() |
| Strict-Transport-Security | max-age=31536000 (本番のみ) |

## CLI コマンド

```bash
flask --app app init_db        # テーブル作成
flask --app app create_admin   # 管理者作成 (対話形式 or --email/--password/--name)
```

## ライセンス

[Apache License 2.0](LICENSE)

Copyright 2026 賀屋悠

## 関連リポジトリ

- **モバイルアプリ (Flutter)**: https://github.com/Himanaraba/attendance-app
