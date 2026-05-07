# 部活動出欠管理システム

## セットアップ

```bash
# 1. Python 仮想環境の作成
python -m venv venv

# Windows
venv\Scripts\activate
# Mac/Linux
source venv/bin/activate

# 2. 依存パッケージのインストール
pip install -r requirements.txt

# 3. データベース初期化
flask --app app init_db

# 4. 管理者アカウント作成
flask --app app create_admin

# 5. 開発サーバー起動
flask --app app run --debug
```

ブラウザで http://localhost:5000 にアクセス。

## 本番環境

```bash
# SECRET_KEY を環境変数で設定すること
set SECRET_KEY=<ランダムな長い文字列>   # Windows
export SECRET_KEY=<...>                  # Linux/Mac

# gunicorn などの WSGI サーバーで起動
pip install gunicorn
gunicorn -w 4 app:app
```

## セキュリティヘッダー（自動付与）

| ヘッダー | 値 |
|---|---|
| X-Content-Type-Options | nosniff |
| X-Frame-Options | DENY |
| X-XSS-Protection | 1; mode=block |
| Referrer-Policy | strict-origin-when-cross-origin |
| Permissions-Policy | geolocation=(), camera=(), microphone=() |
| Strict-Transport-Security | max-age=31536000（本番のみ） |

## 機能一覧

- PBKDF2-SHA256 パスワードハッシュ
- 初回/一時パスワード後の強制パスワード変更
- ログイン失敗5回でアカウントロック（30分）
- 30日セッション維持
- CSRF トークン検証（フォーム・JSON API）
- 安全なリダイレクト検証
- ロールベースアクセス制御（user / manager / admin）
- 出欠ダッシュボード（統計・進捗バー・FullCalendar）
- 出欠ステータス（出席/欠席/部分参加）+ コメント + 部分参加時刻
- イベント管理（CRUD・カレンダードラッグ移動・複製 API）
- 週次テンプレートから4週間一括生成
- ユーザー管理（名前検索・役職フィルタ・ロール・自動欠席曜日）
- CSV エクスポート（日別・期間別・サマリ・個人用）UTF-8 BOM 付き
- 英語/日本語切替
- ダークモード（localStorage 保存）

## CLI コマンド

```bash
flask --app app init_db          # テーブル作成
flask --app app create_admin     # 管理者作成（対話形式）
```
