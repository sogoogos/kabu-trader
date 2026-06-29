#!/usr/bin/env bash
# 週次の成績レポート（scripts/perf_report.py）を cron に登録する。
# 毎週土曜 02:00 UTC（= 土 11:00 JST、米金曜クローズ後）に3環境のスコアカードを
# perf_reports/YYYY-MM-DD.txt へ書き出す。--since スライスで 2026-06-29 の変更
# （ローテーション・スコアマージンガード / ATR可変ストップ / US TP・ストップ再フィット）
# が効いているかを継続追跡する。
#
# 使い方（EC2 のリポジトリ直下で）:
#   bash scripts/install_perf_report_cron.sh
#
# 既存の crontab は保持し、perf_report 関連行のみ入れ替える（再実行で重複しない）。
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"

mkdir -p "${DIR}/perf_reports"

# crontab は % を改行と解釈するため date の % は \% でエスケープする。
JOB="0 2 * * 6 cd ${DIR} && ${PY} scripts/perf_report.py > perf_reports/\$(date +\\%Y-\\%m-\\%d).txt 2>&1"

{
  crontab -l 2>/dev/null | grep -vE 'perf_report\.py' || true
  echo "${JOB}"
} | crontab -

echo "installed. 登録内容:"
crontab -l | grep -E 'perf_report\.py'
