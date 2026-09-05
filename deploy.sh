#!/usr/bin/env bash
# 服务器侧一键更新：拉最新代码 → 按需重建题库 → 自检 → 让服务用上新数据。
# 完整用法见 ./deploy.sh --help（usage() 是唯一事实来源，别在这里再抄一份）。
#
# 构建与启动的逻辑不在这里，全在 start.sh —— 这个脚本只管 git 那一侧、
# 决定要不要重启，以及把自检当成放行闸门。

set -euo pipefail
cd "$(dirname "$0")"

PORT="${SAA_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

# data/ 下这几个是 build_bank.py / verify_bank.py 的产物。服务器上跑过一次构建就会
# 被整份重写（哪怕内容一模一样，也可能只差一个末尾换行），于是每次 git pull 都撞
# 「Your local changes would be overwritten by merge」。它们的内容完全由三个源文件 +
# manual_fixes.json 决定，丢掉没有任何损失，所以这里按白名单自动丢弃。
#
# 名单之外的改动一律不碰：manual_fixes.json 和 i18n_zh.jsonl 是人写的输入，脚本和
# 前端更是代码 —— 那些脏了就停下来报错，让人自己决定，绝不替他 checkout 掉。
GENERATED=(
  data/questions.json
  data/questions_en.json
  data/build_report.md
  data/i18n_todo.jsonl
  data/verify_baseline.json
)

usage() {
  cat <<EOF
用法：./deploy.sh [选项]

拉最新代码 → 按需重建题库 → 自检 → 服务用上新数据。可重复执行，幂等。

流程
  1. 丢弃 data/ 下构建产物的本地改动（只丢白名单里那 5 个），其它文件脏了就中止
  2. git pull --ff-only
  3. ./start.sh --no-open：按需两阶段重建 + 幂等启动（服务没在跑就拉起来）
  4. scripts/verify_bank.py --strict：不过就非零退出
  5. app.py 有变更时重启服务；只有数据或前端变化则靠热加载，不打断正在做的题

选项
  --restart       无论 app.py 有没有变都重启服务
  --rebuild       强制重跑两阶段构建（透传给 start.sh）
  -h, --help      显示本帮助
  SAA_PORT=端口   环境变量，默认 ${PORT}

  ./deploy.sh              # 日常更新
  ./deploy.sh --restart    # 换了 app.py 之外的东西也想重启时
EOF
}

FORCE_RESTART=0
REBUILD=""
for a in "$@"; do
  case "$a" in
  -h | --help)
    usage
    exit 0
    ;;
  --restart) FORCE_RESTART=1 ;;
  --rebuild) REBUILD="--rebuild" ;;
  *) echo "· 忽略无法识别的参数：${a}（可用参数见 ./deploy.sh --help）" ;;
  esac
done

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "✗ 这里不是 git 仓库"
  exit 1
}
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[ -z "$UPSTREAM" ] && {
  echo "✗ 当前分支没有跟踪远端，先 git branch -u origin/<分支>"
  exit 1
}

# 先刷新索引里的 stat 缓存。构建会把产物整份重写，内容一样但 mtime 变了，
# git diff-index 只看 stat 就会判「有改动」—— 不刷新的话，下面的守卫会把一次
# 什么都没改的重建当成脏工作树，直接中止部署（实测第一次跑 deploy.sh 就误报了）。
git update-index -q --refresh || true

# ---------- 1. 清理构建产物 ----------
# 不用数组攒：macOS 自带的 bash 3.2 在 set -u 下把空数组当未定义，
# ${#ARR[@]} 会直接报 unbound variable 把脚本打挂。
DIRTY_GEN=""
for f in "${GENERATED[@]}"; do
  git diff --quiet -- "$f" 2>/dev/null || DIRTY_GEN="$DIRTY_GEN $f"
done
if [ -n "$DIRTY_GEN" ]; then
  echo "▸ 丢弃构建产物的本地改动：$DIRTY_GEN"
  # 路径都是上面写死的、不含空格，这里的词分割是有意的
  git checkout -- $DIRTY_GEN
fi

# 名单之外还有脏的就停下。这里用 diff-index 而不是 status --porcelain：
# 未跟踪文件（比如本地的 TODO 草稿、data/progress.json）不该挡住部署。
OTHER="$(git diff-index --name-only HEAD -- || true)"
if [ -n "$OTHER" ]; then
  echo "✗ 以下文件有未提交的改动，不是构建产物，不敢替你丢："
  echo "$OTHER" | sed 's/^/    /'
  echo "  先 git stash 或提交掉，再跑 ./deploy.sh"
  exit 1
fi

# ---------- 2. 拉取 ----------
OLD="$(git rev-parse HEAD)"
echo "▸ git pull --ff-only（${UPSTREAM}）"
git pull --ff-only
NEW="$(git rev-parse HEAD)"

if [ "$OLD" = "$NEW" ]; then
  echo "  已经是最新：$(git log --oneline -1)"
else
  echo "  $(git rev-parse --short "$OLD") → $(git rev-parse --short "$NEW")，新提交："
  git log --oneline "$OLD..$NEW" | sed 's/^/    /'
fi

CHANGED="$(git diff --name-only "$OLD" "$NEW" || true)"

# 这次 pull 把 deploy.sh 自己换了的话，用新版重跑一遍：bash 是边读边执行脚本文件的，
# 执行中途被替换有读到错位内容的风险。重跑是幂等的 —— 第二次 pull 无新提交，
# CHANGED 为空，不会再 exec 一次。
if echo "$CHANGED" | grep -qx "deploy.sh"; then
  echo "▸ deploy.sh 自身有更新，用新版重跑"
  exec ./deploy.sh "$@"
fi

# app.py 是常驻进程，改了必须重启才生效。questions.json / i18n_zh.jsonl /
# web/index.html 都按 mtime 热加载，重启只会打断正在做的题，所以不重启。
# 注意这里必须写成 if：set -e 下 `grep -q … && VAR=1` 不命中会让整个脚本退出。
NEED_RESTART=$FORCE_RESTART
if echo "$CHANGED" | grep -qx "scripts/app.py"; then
  NEED_RESTART=1
fi

# ---------- 3. 构建 + 启动（逻辑都在 start.sh 里） ----------
if [ "$NEED_RESTART" = "1" ]; then
  echo "▸ app.py 有变更，先停掉旧进程"
  ./start.sh stop || true
fi
./start.sh --no-open ${REBUILD:+$REBUILD}

# ---------- 4. 自检当放行闸门 ----------
echo "▸ 自检"
if ! python3 scripts/verify_bank.py --strict; then
  echo "✗ 自检未通过 —— 题库可能已经劣化，服务仍在跑着旧进程读到的数据。"
  echo "  先查 data/build_report.md 和上面的失败项，别放着不管。"
  exit 1
fi

# ---------- 5. 交代结果 ----------
echo
echo "✅ 部署完成"
echo "   版本：$(git log --oneline -1)"
python3 - <<'PYEOF'
import json
qs = json.load(open("data/questions.json", encoding="utf-8"))
usable = sum(1 for q in qs if not q["needs_review"] and q["answer"])
zh = sum(1 for q in qs for o in q["options"] if o.get("text_zh"))
tot = sum(1 for q in qs for o in q["options"] if o.get("text_en"))
print("   题库：%d 题，可出题 %d，选项中文 %d/%d" % (len(qs), usable, zh, tot))
PYEOF
git update-index -q --refresh || true
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
  # 重建产物和仓库里那份不完全一致时说一声：不是错误（内容由源文件决定），
  # 下次 ./deploy.sh 第 1 步会自动丢掉，但闷着不说容易让人以为 pull 坏了。
  echo "   备注：构建产物与仓库版本有差异，下次 ./deploy.sh 会自动丢弃"
fi
if curl -sf -m 2 "${URL}/api/bootstrap" >/dev/null 2>&1; then
  echo "   服务：${URL} 正常"
else
  echo "   服务：${URL} 探测失败，看 data/app.log"
  exit 1
fi
