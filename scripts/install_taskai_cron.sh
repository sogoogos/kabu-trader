#!/usr/bin/env bash
# TaskAI への取引状況プッシュを cron に登録する（平日30分ごと・3市場）。
#
# 使い方:
#   export TASKAI_INGEST_URL=https://taskai.busystems.com/api/trading/ingest
#   export TASKAI_INGEST_TOKEN=<TaskAI と同じトークン>
#   bash scripts/install_taskai_cron.sh
#
# 既存の crontab は保持し、push_taskai 関連行のみ入れ替える（再実行で重複しない）。
set -euo pipefail

: "${TASKAI_INGEST_URL:?export TASKAI_INGEST_URL first}"
: "${TASKAI_INGEST_TOKEN:?export TASKAI_INGEST_TOKEN first}"

DIR="$(cd "$(dirname "$0")/.." && pwd)"

job() {  # container config source label
  printf '%s\n' "*/30 * * * 1-5 cd ${DIR} && docker cp scripts/push_taskai.py ${1}:/app/push_taskai.py >/dev/null 2>&1 && docker compose exec -T -e TASKAI_INGEST_URL -e TASKAI_INGEST_TOKEN ${1} python /app/push_taskai.py -c ${2} --source ${3} --label \"${4}\" >> /tmp/taskai_push.log 2>&1"
}

{
  crontab -l 2>/dev/null | grep -vE 'push_taskai|TASKAI_INGEST_' || true
  echo "TASKAI_INGEST_URL=${TASKAI_INGEST_URL}"
  echo "TASKAI_INGEST_TOKEN=${TASKAI_INGEST_TOKEN}"
  job kabu-trader-jp      config/default.json jp   "日本株(ペーパー)"
  job kabu-trader-jp-live config/live.json    live "日本株(ライブ)"
  job kabu-trader-us      config/us.json      us   "米国株"
} | crontab -

echo "installed. 登録内容:"
crontab -l | grep -E 'push_taskai|TASKAI_INGEST_'
