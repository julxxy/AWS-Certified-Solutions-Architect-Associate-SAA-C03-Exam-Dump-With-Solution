#!/usr/bin/env bash
# 一键进入学习状态。完整用法见 ./start.sh --help（usage() 是唯一事实来源，
# 别在这里再抄一份 —— 上一版头注释只写了 6 个入口里的 3 个，跟实际行为漂开了）。

set -euo pipefail
cd "$(dirname "$0")"

PORT="${SAA_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
PIDFILE=".saa-app.pid"
LOGFILE="data/app.log"

usage() {
  cat <<EOF
用法：./start.sh [页面] [选项]

一键进入学习状态：按需重建题库 → 启动本地服务 → 打开浏览器直达指定页面。
不带参数时等价于 ./start.sh learn。

页面（决定浏览器打开后停在哪儿，默认 learn）
  learn      滚动学习，并直接开始一轮新会话
  exam       模拟考试（65 题 / 130 分钟 / 720 分及格）
  home       首页：断点续学摘要、今日到期、译文覆盖率
  wrong      错题本
  stats      学习统计：box 分布、7 天到期预测、假掌握清单、各领域正确率
  browse     题库浏览（含被排除出题池的题）

选项
  --rebuild  强制重跑两阶段构建（阶段一抽 PDF + 阶段二合并），跳过时间戳判断
  --stop     停掉后台服务后退出。只在第一个参数的位置生效
  -h, --help 显示本帮助

环境变量
  SAA_PORT   监听端口，默认 ${PORT}

行为说明
  · 幂等：服务已在跑就不重复起，只把浏览器开过去
  · 按需构建：PDF / 两份解析文档 / manual_fixes.json / build_bank.py 任一比产物
    新就自动重建；改了抽取逻辑或 PDF 会连阶段一一起重跑
  · data/i18n_zh.jsonl 不触发重建 —— 程序会热加载译文，重建只是把它固化进题库
  · 服务在后台运行，日志写到 ${LOGFILE}，进程号记在 ${PIDFILE}
  · 所有进度与设置都在 data/ 下：progress.json、wrong_book.json、settings.json、exams/

示例
  ./start.sh                 # 最常用：接着上次的进度继续刷
  ./start.sh exam            # 直接进模拟考试
  ./start.sh stats           # 只看统计，不开新会话
  ./start.sh --rebuild home  # 改完题库数据后强制重建，然后开首页
  SAA_PORT=9000 ./start.sh   # 换端口跑
  ./start.sh --stop          # 收工
EOF
}

# --help 放在任何位置都认，且优先于其它参数
for a in "$@"; do
  case "$a" in
    -h|--help) usage; exit 0 ;;
  esac
done

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
    # 认不出来的参数别默默吞掉：`./start.sh exm` 原先会一声不吭地开到滚动学习，
    # 用户以为进的是考试。只提示、不中断，免得挡住正常启动。
    # 变量必须写成 ${a}：本机 LC_CTYPE=C，bash 会把紧跟其后的全角「（」的首字节
    # 当成变量名的一部分，$a（ 被解析成 ${a<0xEF>}，配合 set -u 直接报
    # 「unbound variable」把脚本打挂。
    *) echo "· 忽略无法识别的参数：${a}（可用参数见 ./start.sh --help）" ;;
  esac
done

# ---------- 构建（按需） ----------
PDF="AWS Certified Solutions Architect Associate SAA-C03.pdf"
SOL="AWS SAA-03 Solution.txt"
SOLZH="AWS SAA-03 Solution.zh-CN.txt"
BANK="data/questions.json"
BANK_EN="data/questions_en.json"

need_build=0
need_extract=0
[ ! -f "$BANK" ] && need_build=1
[ ! -f "$BANK_EN" ] && need_extract=1
[ "$FORCE" = "1" ] && need_build=1 && need_extract=1

# 阶段一（--extract）的输入是 PDF 和抽取逻辑，产物是 questions_en.json。
# 判断必须拿它们和 **questions_en.json** 比，不能和 questions.json 比：
#   · 按 reference.md 修了抽取逻辑后跑 start.sh，只会跑阶段二，而阶段二只读
#     questions_en.json —— 抽取修复一个字都不生效，脚本却打印"重新构建题库"；
#   · PDF 更新后手跑过一次不带 --extract 的 build，questions.json 就此新过 PDF，
#     之后每次都判"题库是最新的"，PDF 改动永远进不来。
for src in "$PDF" scripts/build_bank.py; do
  [ -f "$src" ] && [ -f "$BANK_EN" ] && [ "$src" -nt "$BANK_EN" ] && need_extract=1
done

# 阶段二的输入：阶段一产物 + 两份解析文档 + 人工修正 + 构建脚本本身。
# questions_en.json 必须在列，否则手跑过 --extract 之后阶段二不会跟着重跑。
# manual_fixes.json 也必须在列：改完手工修正不重建就完全不生效，而且一声不吭。
# i18n_zh.jsonl 故意不在列 —— app.py 会热加载译文，重建只是把它固化进 questions.json。
for src in "$BANK_EN" "$SOL" "$SOLZH" data/manual_fixes.json scripts/build_bank.py; do
  [ -f "$src" ] && [ -f "$BANK" ] && [ "$src" -nt "$BANK" ] && need_build=1
done

# 要重跑阶段一，阶段二必然也要重跑（否则新抽取的结果没人合并）。
[ "$need_extract" = "1" ] && need_build=1

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
