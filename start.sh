#!/usr/bin/env bash
# 一键进入学习状态。
#   ./start.sh            构建（按需）+ 启动 + 直接开到滚动学习并开始新会话
#   ./start.sh exam       直接开到模拟考试
#   ./start.sh home       只开首页
#   ./start.sh --rebuild  强制重跑两阶段构建
#   ./start.sh --stop     停掉后台服务
#
# 幂等：已在跑就不重复起，只把浏览器开过去。

set -euo pipefail
cd "$(dirname "$0")"

PORT="${SAA_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
PIDFILE=".saa-app.pid"
LOGFILE="data/app.log"

PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "✗ 找不到 python3"; exit 1; }

# ---------- 停止 ----------
if [ "${1:-}" = "--stop" ]; then
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE" && echo "✓ 已停止"
  else
    pkill -f "scripts/app.py" 2>/dev/null && echo "✓ 已停止" || echo "· 本来就没在跑"
    rm -f "$PIDFILE"
  fi
  exit 0
fi

FORCE=0
GO="learn"
for a in "$@"; do
  case "$a" in
    --rebuild) FORCE=1 ;;
    learn|exam|home|wrong|stats|browse) GO="$a" ;;
    *) ;;
  esac
done

# ---------- 构建（按需） ----------
PDF="AWS Certified Solutions Architect Associate SAA-C03.pdf"
SOL="AWS SAA-03 Solution.txt"
SOLZH="AWS SAA-03 Solution.zh-CN.txt"
BANK="data/questions.json"

need_build=0
need_extract=0
[ ! -f "$BANK" ] && need_build=1
[ "$FORCE" = "1" ] && need_build=1 && need_extract=1
# 任一数据源比题库新 → 重建
for src in "$PDF" "$SOL" "$SOLZH" scripts/build_bank.py; do
  [ -f "$src" ] && [ -f "$BANK" ] && [ "$src" -nt "$BANK" ] && need_build=1
done
# PDF 变了必须重跑阶段一。阶段二只读 questions_en.json，光跑它对 PDF 改动是空转：
# 会打印"重新构建题库"，但题干和选项一个字都不会变。
{ [ -f "$PDF" ] && [ -f "$BANK" ] && [ "$PDF" -nt "$BANK" ]; } && need_extract=1

if [ "$need_build" = "1" ]; then
  echo "▸ 数据源有更新，重新构建题库…"
  [ "$need_extract" = "1" ] && "$PY" scripts/build_bank.py --extract
  "$PY" scripts/build_bank.py
  echo
else
  echo "▸ 题库是最新的，跳过构建（要强制重建用 --rebuild）"
fi

# ---------- 启动（幂等） ----------
alive() { curl -sf -m 1 "${URL}/api/bootstrap" >/dev/null 2>&1; }

if alive; then
  echo "▸ 服务已在 ${URL} 运行，直接打开"
else
  mkdir -p data
  nohup "$PY" scripts/app.py --no-open --port "$PORT" >"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 40); do alive && break; sleep 0.25; done
  if alive; then
    echo "▸ 已启动 ${URL}（日志 ${LOGFILE}，停止用 ./start.sh --stop）"
  else
    echo "✗ 启动失败，日志："; tail -20 "$LOGFILE"; exit 1
  fi
fi

# ---------- 打开浏览器并进入指定状态 ----------
TARGET="${URL}/?go=${GO}"
case "$(uname -s)" in
  Darwin) open "$TARGET" ;;
  Linux)  command -v xdg-open >/dev/null && xdg-open "$TARGET" >/dev/null 2>&1 || echo "请手动打开 $TARGET" ;;
  *)      echo "请手动打开 $TARGET" ;;
esac

# ---------- 顺带报一下译文进度 ----------
"$PY" - <<'PYEOF'
import json, os
try:
    qs = json.load(open("data/questions.json", encoding="utf-8"))
    # 分母只算可译的选项：477 题的选项是图片、无英文原文，算进去覆盖率永远到不了 100%
    tot = sum(1 for q in qs for o in q["options"] if o.get("text_en"))
    zh = sum(1 for q in qs for o in q["options"] if o.get("text_zh"))
    todo = sum(1 for _ in open("data/i18n_todo.jsonl", encoding="utf-8")) \
        if os.path.exists("data/i18n_todo.jsonl") else 0
    print("▸ 选项中文覆盖 %d/%d (%.1f%%)，待译清单 %d 条" % (zh, tot, 100.0*zh/tot if tot else 0, todo))
except Exception:
    pass
PYEOF
