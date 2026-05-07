#!/bin/bash
# VPSで実行する自動デプロイスクリプト
# 使い方: ./deploy.sh

set -e

cd "$(dirname "$0")"

echo "🔵 デプロイ開始 $(date '+%Y-%m-%d %H:%M:%S')"

# 1. DBバックアップ (失敗時に戻せるように)
mkdir -p ~/backups
BACKUP_FILE=~/backups/attendance_$(date +%Y%m%d_%H%M%S).db
if [ -f attendance.db ]; then
    cp attendance.db "$BACKUP_FILE"
    echo "✅ DBバックアップ: $BACKUP_FILE"
fi

# 30日より古いバックアップを削除
find ~/backups -name 'attendance_*.db' -mtime +30 -delete 2>/dev/null || true

# 2. コード更新
echo "📥 git pull..."
git pull --ff-only

# 3. 依存関係更新
echo "📦 依存関係更新..."
source venv/bin/activate
pip install -q -r requirements.txt

# 4. サービス再起動 (DBマイグレーションは Flask が起動時に自動実行)
echo "🔄 再起動..."
sudo systemctl restart attendance

# 5. 動作確認
sleep 2
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/login)
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "✅ デプロイ成功 (HTTP $HTTP_CODE)"
else
    echo "❌ HTTP $HTTP_CODE - 異常検知。ログを確認してください："
    echo "   sudo journalctl -u attendance -n 30"
    exit 1
fi
