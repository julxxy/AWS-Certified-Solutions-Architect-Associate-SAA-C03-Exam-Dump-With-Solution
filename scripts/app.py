#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — AWS SAA-C03 中英对照刷题程序（本地、离线、仅标准库）

    python3 scripts/app.py            # 监听 127.0.0.1:8765 并自动打开浏览器
    python3 scripts/app.py --no-open  # 不自动开浏览器
    python3 scripts/app.py --port N

模式 A 模拟考试：65 题 / 130 分钟 / 720 分及格
模式 B 滚动学习：三段式会话 + Leitner 间隔重复 + 四档复习强度 + 信心度打分
"""

import argparse
import json
import os
import random
import re
import threading
import uuid
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 脚本在 scripts/ 下，仓库根目录要再往上退一层。
# 别"简化"成 dirname(__file__) —— 那样 data/ 会解析到 scripts/data/，
# 题库读不到、进度写错地方，而且不报错。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
EXAMS = os.path.join(DATA, "exams")

F_QUESTIONS = os.path.join(DATA, "questions.json")
F_I18N = os.path.join(DATA, "i18n_zh.jsonl")
F_PROGRESS = os.path.join(DATA, "progress.json")
F_WRONG = os.path.join(DATA, "wrong_book.json")
F_SETTINGS = os.path.join(DATA, "settings.json")
F_INDEX = os.path.join(ROOT, "web", "index.html")

_INDEX_CACHE = None
_INDEX_MTIME = None

BOX_INTERVALS = [1, 2, 4, 9, 21]  # Leitner 5 档，单位天。box1 是 1 天不是 0 天
EXAM_SIZE = 65
EXAM_MINUTES = 130
PASS_SCORE = 720
DOMAIN_MIX = {"secure": 0.30, "resilient": 0.26, "performant": 0.24, "cost": 0.20}

DEFAULT_SETTINGS = {
    "lang_mode": "both",  # zh | en | both
    "timer_enabled": True,
    "scoring_mode": "linear",  # linear | aws_scaled
    "partial_credit": False,
    "theme": "system",  # system | light | dark
    "exam_date": None,  # "YYYY-MM-DD"
    "session_size": 25,
    "order": "sequential",  # sequential | random | review_first
}

_LOCK = threading.RLock()


# ==========================================================================
# 存储
# ==========================================================================

def now_iso():
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def new_id():
    """会话/考试 id。

    别退回 md5(now_iso())：now_iso() 只到秒，同一秒内开两次会话会算出同一个 id，
    后开的那个把前一个从 SESSIONS 里挤掉，前一个的答题进度当场丢失。
    """
    return uuid.uuid4().hex[:10]


def today_str():
    return date.today().isoformat()


def atomic_write_json(path, obj):
    """先写 .tmp 再 os.replace()，进程被杀也不会留下半截 JSON。

    临时文件名带上 pid + 随机后缀：ThreadingHTTPServer 下两个请求同时写同一个
    路径时，固定的 `path + ".tmp"` 会互相踩 —— 后到的 os.replace 找不到文件抛
    FileNotFoundError（HTTP 500、这次写入被吞），并发再高一点还能落出半截 JSON。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, default):
    if not os.path.exists(path):
        return json.loads(json.dumps(default))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        warn("%s 读取失败（%s），已使用默认值" % (os.path.basename(path), e))
        return json.loads(json.dumps(default))


def warn(msg):
    print("  [警告] " + msg, flush=True)


# ==========================================================================
# 题库 + 译文热加载
# ==========================================================================

class Bank:
    def __init__(self):
        self.questions = {}
        self.order = []
        self._mtimes = {}
        self.i18n_bad = 0
        self.load()

    # -- 加载 ------------------------------------------------------------
    def _stamp(self):
        m = {}
        for p in (F_QUESTIONS, F_I18N):
            m[p] = os.path.getmtime(p) if os.path.exists(p) else 0
        return m

    def load(self):
        if not os.path.exists(F_QUESTIONS):
            raise SystemExit(
                "找不到 data/questions.json，请先运行：python3 scripts/build_bank.py")
        with open(F_QUESTIONS, "r", encoding="utf-8") as f:
            qs = json.load(f)
        self.questions = {q["id"]: q for q in qs}
        self.order = sorted(self.questions)
        self._overlay_i18n()
        self._mtimes = self._stamp()

    def _overlay_i18n(self):
        """把 i18n_zh.jsonl 覆盖到内存题库上。

        这样翻译进程边产出、这边刷新就能看到新译文，不必重跑 build_bank.py。
        """
        self.i18n_bad = 0
        if not os.path.exists(F_I18N):
            return
        n_opt = n_stem = 0
        with open(F_I18N, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    rec = json.loads(line)
                    q = self.questions.get(int(rec["id"]))
                    zh = rec.get("zh")
                    if not q or not zh:
                        raise ValueError("bad record")
                    if rec.get("field") == "stem":
                        q["stem_zh"] = zh
                        q["stem_zh_source"] = "pdf_translation"
                        n_stem += 1
                    else:
                        letter = str(rec["letter"]).upper()
                        for o in q["options"]:
                            if o["letter"] == letter:
                                o["text_zh"] = zh
                                n_opt += 1
                                break
                except Exception:
                    # 单行坏了就跳过并计数，绝不中断整体加载
                    self.i18n_bad += 1
        if self.i18n_bad:
            warn("i18n_zh.jsonl 有 %d 行无法解析，已跳过" % self.i18n_bad)
        if n_opt or n_stem:
            print("  译文覆盖：选项 %d 条，题干 %d 条" % (n_opt, n_stem), flush=True)

    def refresh_if_changed(self):
        """每次取题时检查 mtime，有变化就热加载。"""
        with _LOCK:
            if self._stamp() != self._mtimes:
                print("  检测到题库/译文更新，重新加载", flush=True)
                self.load()

    # -- 查询 ------------------------------------------------------------
    def get(self, qid):
        return self.questions.get(int(qid))

    def pool(self):
        """可出题的题：答案可信。needs_review 的题不进池。"""
        return [q for q in self.questions.values()
                if not q["needs_review"] and q.get("answer")]

    def coverage(self):
        # 分母只算有英文文字的选项。图片内容尚未转录时不计入；
        # 像 477 这样人工补回文字和译文后，自动计入，保持与构建报告和自检一致。
        tot_o = sum(1 for q in self.questions.values()
                    for o in q["options"] if o.get("text_en"))
        zh_o = sum(1 for q in self.questions.values()
                   for o in q["options"] if o.get("text_zh"))
        return {
            "options_total": tot_o,
            "options_zh": zh_o,
            "options_pct": round(100.0 * zh_o / tot_o, 1) if tot_o else 0.0,
            "stem_zh": sum(1 for q in self.questions.values() if q.get("stem_zh")),
            "total": len(self.questions),
            "i18n_bad_lines": self.i18n_bad,
        }


BANK = None

# ==========================================================================
# 选项乱序 + 解析字母重映射
# ==========================================================================

# 只在明确是「选项引用」的语境下替换字母，避免把 "A company" 的 A 改掉。
# 中英文里出现过的写法：Option C / Options A / Correct answer A: / Answer: B /
# 选项 A / （选项 A）/ 正确答案 A：/ 答案：B / 行首 "B. " / "C)" 。

# 字母之间的连接符。解析里的并列写法实测有：`Option A/B`、`Options A and B`、
# `Options A, B`、`(A, B)`、`（选项 A、B）`、`选项 A 和 B`。
# 原先只吃「前缀 + 一个字母」，后半截字母原样留下 —— 而它在本次乱序里已经属于
# 另一个选项，半对半错的引用比完全不替换更难发现（651 的解析被改成过 `Option B/B`）。
_CONN = r"\s*(?:[,/、&]\s*(?:and\b|or\b)?|and\b|or\b|和|与|及|或)\s*"
_LETTER = r"[A-F](?![A-Za-z])"

_RE_OPTREF = re.compile(
    # ① 明确的选项引用前缀 + 一个字母或一串并列字母
    r"(?P<pfx>"
    r"\bOptions?\s+"
    r"|\bCorrect\s+answers?\s*[:：]?\s*"
    r"|\bAnswers?\s*[:：]\s*"
    r"|正确答案\s*[:：]?\s*"
    r"|答案\s*[:：]\s*"
    r"|选项\s*"
    r")(?P<run>" + _LETTER + r"(?:" + _CONN + _LETTER + r")*)"
    # ② 括号里只有字母和分隔符：(A, B) / （A、B）。里面没有别的内容，
    #    不可能误伤 `A company` 那类裸字母。
    r"|(?P<par>[（(]\s*[A-F](?:\s*[,/、&]\s*[A-F])+\s*[）)])"
    # ③ 裸字母后紧跟右括号：C) / A）
    r"|(?<![A-Za-z0-9])(?P<L2>[A-F])(?=\s*[\)）])"
    # ④ 行首 "B. " / "C、"
    r"|^(?P<L3>[A-F])(?=[\.、]\s)",
    re.M)

_RE_ONE_LETTER = re.compile(r"[A-F]")


def remap_letters(text, mapping):
    """把解析里的字母引用按本次乱序映射改写。中英两份都要过一遍。"""
    if not text:
        return text

    def sub_all(s):
        """只替换片段里的 A–F。连接词 and / or / 和 都是小写或中文，不会被误伤。"""
        return _RE_ONE_LETTER.sub(lambda m: mapping.get(m.group(0), m.group(0)), s)

    def rep(m):
        if m.group("run"):
            return m.group("pfx") + sub_all(m.group("run"))
        if m.group("par"):
            return sub_all(m.group("par"))
        if m.group("L2"):
            return mapping.get(m.group("L2"), m.group("L2"))
        return mapping.get(m.group("L3"), m.group("L3"))

    return _RE_OPTREF.sub(rep, text)


def render_question(q, seed, reveal=False, only_letters=None):
    """按 seed 打乱选项并重新分配字母。

    硬性约束：只对 options 数组整体 shuffle，text_en / text_zh 作为同一个对象
    一起搬运，绝不分别洗牌 —— 否则会出现「英文是 A 的内容、中文是 C 的内容」，
    在双语并排下几乎看不出来。
    """
    opts = [dict(o) for o in q["options"]]
    rng = random.Random(seed)
    rng.shuffle(opts)

    # only_letters（R2 干扰项狙击）只决定**展示哪几个选项**，字母映射必须覆盖
    # **全部**选项。原先是先过滤再编号，letter_map 里只有被保留的那两三个字母，
    # 而 remap_letters 是 mapping.get(L, L) —— 未展示选项的原始字母原样留在解析里，
    # 正好可能等于某个展示项被重新分配到的新字母。实测：屏幕 A 是正确答案，
    # 解析里却写着「Option A is incorrect」，指的其实是那个没展示的原始 A。
    # 现在展示项排前面拿 A、B…，未展示项排后面拿剩下的字母：解析里对未展示选项的
    # 引用会指向屏幕上不存在的字母（无害），但绝不会误指某个展示项（有害）。
    n_shown = len(opts)
    if only_letters:
        keep = [o for o in opts if o["letter"] in only_letters]
        drop = [o for o in opts if o["letter"] not in only_letters]
        opts, n_shown = keep + drop, len(keep)

    orig2new, shown = {}, []
    for i, o in enumerate(opts):
        new_letter = chr(ord("A") + i)
        orig2new[o["letter"]] = new_letter
        if i >= n_shown:
            continue
        shown.append({
            "letter": new_letter,
            "orig": o["letter"],
            "text_en": o["text_en"],
            "text_zh": o["text_zh"],
        })

    data = {
        "id": q["id"],
        "type": q["type"],
        "select_count": q["select_count"],
        "stem_en": q["stem_en"],
        "stem_zh": q.get("stem_zh"),
        "stem_zh_source": q.get("stem_zh_source"),
        "options": shown,
        "domain": q.get("domain"),
        "needs_review": q["needs_review"],
        "review_reason": q.get("review_reason"),
        "explanation_quality": q.get("explanation_quality", "ok"),
        "letter_map": orig2new,
    }
    if reveal:
        data["answer"] = sorted(orig2new.get(a, a) for a in q.get("answer", []))
        data["answer_orig"] = q.get("answer", [])
        data["explanation_en"] = remap_letters(q.get("explanation_en"), orig2new)
        data["explanation_zh"] = remap_letters(q.get("explanation_zh"), orig2new)
    return data


# ==========================================================================
# 进度 / SRS
# ==========================================================================

def default_progress():
    return {
        "version": 1,
        "updated_at": now_iso(),
        "cursor": {"mode": "sequential", "last_question_id": None, "position": 0},
        "stats": {"answered": 0, "correct": 0, "mastered": 0, "false_mastery": 0},
        "last_session": None,
        "questions": {},
    }


def default_qstate():
    return {
        "attempts": 0, "correct": 0, "wrong": 0,
        "last_result": None, "last_confidence": None,
        "correct_dates": [], "streak": 0, "box": 1,
        "mastered": False, "false_mastery": False, "high_conf_error": False,
        "wrong_picks": {}, "last_seen_at": None, "next_due_at": None,
    }


def load_progress():
    p = read_json(F_PROGRESS, default_progress())
    # 向前兼容：字段缺失就补默认值，不抛异常
    base = default_progress()
    for k, v in base.items():
        p.setdefault(k, v)
    if not isinstance(p.get("questions"), dict):
        p["questions"] = {}
    for qid, st in list(p["questions"].items()):
        if not isinstance(st, dict):
            p["questions"][qid] = default_qstate()
            continue
        for k, v in default_qstate().items():
            st.setdefault(k, v)
    return p


def save_progress(p):
    p["updated_at"] = now_iso()
    atomic_write_json(F_PROGRESS, p)


def qstate(prog, qid):
    return prog["questions"].setdefault(str(qid), default_qstate())


def as_qid(v):
    """把请求体里的题号转成 int，转不动返回 None（不抛异常）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def days_to_exam(settings):
    """距考试还有几天；没设 exam_date 或格式坏了返回 None。"""
    ed = settings.get("exam_date")
    if not ed:
        return None
    try:
        return (date.fromisoformat(ed) - date.today()).days
    except Exception:
        return None


def exam_phase(settings):
    """返回 (sprint, final48)，§4.2.7 的两个阶段。

    首页横幅和出题逻辑必须走同一个判据 —— 原先只有 api_bootstrap 算了这两个标志
    拿去渲染横幅，build_session 和 pick_review_mode 根本不读 exam_date，
    于是页面上写着「冲刺模式：只出 box≤3 与假掌握」，出题却一切照旧。
    """
    left = days_to_exam(settings)
    if left is None:
        return False, False
    return 0 < left <= 7, 0 < left <= 2


def interval_days(box, settings):
    """box → 间隔天数。设了 exam_date 就压缩，保证考前每题还能再过一遍。"""
    base = BOX_INTERVALS[max(0, min(box, 5)) - 1] if box >= 1 else 1
    ed = settings.get("exam_date")
    if not ed:
        return base
    try:
        left = (date.fromisoformat(ed) - date.today()).days
    except Exception:
        return base
    if left <= 0:
        return 1
    if base <= left:
        return base
    # 压缩到考前还能再过一遍
    return max(1, min(base, max(1, left // 2)))


def apply_result(prog, settings, qid, correct, confidence, picked_orig):
    """按 §4.2.3 的信心度规则升降 box，并更新掌握判定。"""
    st = qstate(prog, qid)
    st["attempts"] += 1
    st["last_seen_at"] = now_iso()
    st["last_confidence"] = confidence
    st["high_conf_error"] = False
    st["false_mastery"] = st.get("false_mastery", False)

    # 「对 + 蒙的」按答错处理（§4.2.3 表格第三行）。只做到「box 不升」是不够的：
    #
    #   · 若照样计入 correct / correct_dates，连着三天各蒙对一次就凑齐了「≥3 次答对、
    #     跨 ≥3 个不同日期」，第四次「对+有把握」当场判 mastered —— §4.2.6 明令禁止的
    #     「答对一次就算掌握」就这么被绕开了，全程只有 1 次真正的「对+有把握」。
    #   · 若 last_result 仍记 correct、next_due 又按**当前** box 全额推后
    #     （box5 → 21 天，比答错的 1 天长 20 倍），它在 build_session 的热身候选条件
    #     `is_due(st) or last_result == "wrong"` 里两边都不满足，21 天内一次都进不了
    #     热身段 —— 而 §4.2.5 把假掌握列为热身第 2 优先、称其「最容易在考场翻车」。
    #
    # 所以这里：box 保持不动（不升也不降），但计数、last_result、间隔全部按答错走。
    # 只有 st["wrong"] 不加 —— 那个数字在错题本上显示为「错 N / 对 M」，
    # 用户确实选对了，记成答错会看不懂；假掌握有 false_mastery 这个专门的标记。
    lucky_guess = correct and confidence == "guess"
    due_box = st.get("box", 1)  # 算 next_due 用的 box，不一定等于 st["box"]

    if correct and not lucky_guess:
        st["correct"] += 1
        st["last_result"] = "correct"
        st["streak"] = st.get("streak", 0) + 1
        d = today_str()
        if d not in st["correct_dates"]:
            st["correct_dates"].append(d)
        st["box"] = min(5, st["box"] + 1)
        due_box = st["box"]
        if confidence == "unsure":
            factor = 0.5  # 升 box 但 next_due 折半
        else:
            st["false_mastery"] = False
            factor = 1.0
    elif lucky_guess:
        st["last_result"] = "wrong"
        st["streak"] = 0
        st["false_mastery"] = True
        due_box = 1  # 间隔按 box 1 算，和答错一致；box 本身不动
        factor = 1.0
    else:
        st["wrong"] += 1
        st["last_result"] = "wrong"
        st["streak"] = 0
        st["box"] = 1
        due_box = 1
        factor = 1.0
        if confidence == "sure":
            # 错 + 有把握 → 置顶到下次会话热身段第一位
            st["high_conf_error"] = True
        if picked_orig:
            for L in picked_orig:
                st["wrong_picks"][L] = st["wrong_picks"].get(L, 0) + 1

    days = interval_days(due_box, settings) * factor
    st["next_due_at"] = (datetime.now().astimezone()
                         + timedelta(days=days)).replace(microsecond=0).isoformat()

    # §4.2.6 掌握判定：≥3 次答对、跨 ≥3 个不同日期、最近一次为「对+有把握」
    st["mastered"] = bool(
        st["correct"] >= 3
        and len(set(st["correct_dates"])) >= 3
        and st["last_result"] == "correct"
        and st["last_confidence"] == "sure"
    )

    prog["stats"]["answered"] += 1
    if correct:
        prog["stats"]["correct"] += 1
    prog["stats"]["mastered"] = sum(
        1 for s in prog["questions"].values() if s.get("mastered"))
    prog["stats"]["false_mastery"] = sum(
        1 for s in prog["questions"].values() if s.get("false_mastery"))
    return st


def _parse_due(s, ref):
    """解析 next_due_at。

    程序自己写的是带时区的，但手改过、或从别的机器/旧版本带过来的 progress.json
    里会出现 "2026-08-20T10:00:00" 这种无时区的 —— fromisoformat 会成功返回一个
    naive datetime，拿它和 aware 的 now() 比较直接抛 TypeError。原先比较写在
    try 之外，于是 /api/bootstrap 500、首页整页打不开，违反 §4.4「字段缺失走迁移
    /补默认值，不要直接抛异常」。这里按本地时区补上，判不出来就返回 None。
    """
    try:
        due = datetime.fromisoformat(s)
    except Exception:
        return None
    return due.replace(tzinfo=ref.tzinfo) if due.tzinfo is None else due


def is_due(st, ref=None):
    if not st.get("next_due_at"):
        return st.get("attempts", 0) > 0
    ref = ref or datetime.now().astimezone()
    due = _parse_due(st["next_due_at"], ref)
    return True if due is None else due <= ref


def overdue_days(st):
    ref = datetime.now().astimezone()
    due = _parse_due(st.get("next_due_at") or "", ref)
    return 0 if due is None else max(0, (ref - due).days)


# ==========================================================================
# 错题本
# ==========================================================================

def load_wrong():
    w = read_json(F_WRONG, {"version": 1, "ids": []})
    w.setdefault("ids", [])
    return w


def wrong_add(qid):
    w = load_wrong()
    if int(qid) not in w["ids"]:
        w["ids"].append(int(qid))
        atomic_write_json(F_WRONG, w)


def wrong_remove(qid):
    w = load_wrong()
    w["ids"] = [i for i in w["ids"] if i != int(qid)]
    atomic_write_json(F_WRONG, w)


# ==========================================================================
# 复习强度选档（§4.2.2）
# ==========================================================================

def pick_review_mode(st):
    if st.get("wrong_picks") and st.get("box", 1) <= 3:
        return "R2"  # 有错误记录 → 干扰项狙击，优先于 R1
    box = st.get("box", 1)
    if box <= 2:
        return "R1"
    if box <= 4:
        return "R3"
    return "R4"


# ==========================================================================
# 会话编排（§4.2.1）
# ==========================================================================

def build_session(prog, settings, bank, size=None):
    size = int(size or settings.get("session_size") or 25)
    pool = {q["id"]: q for q in bank.pool()}

    # §4.2.1 给的基准是 25 题 → 热身 8 / 新题 14 / 收尾 3。
    # 收尾段要**向下取整**：round(25*0.15) = round(3.75) = 4，会算出 8/13/4，
    # 每轮少推一道新题、多留一个收尾名额，和 SPEC 写死的数字对不上。
    n_warm = max(0, round(size * 0.30))
    n_cool = max(0, int(size * 0.15))
    n_new = size - n_warm - n_cool

    # §4.2.7 考试日期倒排。两个阶段都会改热身段的取题范围与档位。
    sprint, final48 = exam_phase(settings)

    # ---- 热身段：错题 + 今日到期题，按 §4.2.5 优先级排序 ----
    cands = []
    for qid, st in prog["questions"].items():
        qid = int(qid)
        if qid not in pool:
            continue
        # §4.2.6：已掌握的题退出常规轮转，「只在 R4 速览和考前冲刺中出现」——
        # 所以考前 48 小时的速览要把它们放回来。
        if st.get("mastered") and not final48:
            continue
        # 考前 48 小时以速览为主，不再挑到期与否，未掌握的全都过一遍。
        if not (final48 or is_due(st) or st.get("last_result") == "wrong"):
            continue
        # §4.2.7 冲刺模式（考前 7 天）：只出 box ≤ 3 与「假掌握」的题。
        if sprint and not final48 and not (st.get("box", 1) <= 3
                                           or st.get("false_mastery")):
            continue
        pri = (
            0 if st.get("high_conf_error") else
            1 if st.get("false_mastery") else
            2
        )
        cands.append((pri, -overdue_days(st), -st.get("wrong", 0), qid))
    cands.sort()
    warm_ids = [c[3] for c in cands[:n_warm * 3]]
    warm_ids = interleave(warm_ids, pool)[:n_warm]

    # 到期题不够，配额让给新题
    if len(warm_ids) < n_warm:
        n_new += n_warm - len(warm_ids)

    # ---- 新题段：从 cursor 继续 ----
    #
    # 「出题顺序」管的是**新题按什么次序推进**，不该改变过滤语义。原先的过滤条件写成
    # `qid not in warm_ids and (order_mode != "sequential" or qid not in seen)`，
    # 非 sequential 时右半边恒为 True，seen 检查被整个绕过，mastered 也从没出现在
    # 这个循环里 —— 结果 random 下按比例混入已掌握的题，review_first 下（ids 被排成
    # 「做过的在前」）整段新题全是旧题，还都按 mode="R1" 完整重做，
    # 违反 §4.2.6「已 mastered 的题退出常规轮转」。
    seen = set(int(k) for k in prog["questions"])
    order_mode = settings.get("order", "sequential")
    ids = sorted(pool)
    if order_mode == "random":
        random.shuffle(ids)

    # cursor.position 是「在 ids 里推进到哪儿了」，只有顺序稳定时才有意义。
    # random 模式每次会话重排一次 ids，拿上一轮的下标去索引这个全新排列毫无对应
    # 关系 —— 所以随机模式既不按 position 旋转，也不往回写 position，
    # 进度完全由 seen 集合驱动（新题段本来就只取没做过的题）。
    keeps_cursor = order_mode != "random"
    pos = int(prog["cursor"].get("position") or 0) if keeps_cursor else 0
    n = len(ids)
    rotated = [ids[(pos + k) % n] for k in range(n)] if n else []

    # 真·新题：没做过的。只绕**一圈** —— 原先 guard 允许绕两圈，剩余新题不够时
    # 第二圈会把同一批题原样再取一遍，同一场会话里同一道题出现两次，
    # 第二遍作答会立刻覆盖第一遍刚写的 box / next_due，Leitner 间隔当场失效。
    fresh = [q for q in rotated if q not in warm_ids and q not in seen]
    # 做过、但还没掌握的旧题。已 mastered 的一律不进常规轮转（§4.2.6）。
    stale = [q for q in rotated if q not in warm_ids and q in seen
             and not prog["questions"].get(str(q), {}).get("mastered")]

    # review_first =「错题与到期题优先」：先排没掌握的旧题，不够再推新题。
    # 其余顺序先推新题，新题用尽才拿旧题补齐。
    ordered = (stale + fresh) if order_mode == "review_first" else (fresh + stale)
    new_ids, chosen = [], set()
    for qid in ordered:
        if len(new_ids) >= n_new:
            break
        if qid in chosen:
            continue
        chosen.add(qid)
        new_ids.append(qid)

    # cursor 指向「下次从哪儿接着找新题」：最后一道被取用的**新**题的下一格。
    # 只认新题 —— 拿旧题补齐不代表进度推进了。
    cursor_next = (pos if n else 0) if keeps_cursor else int(prog["cursor"].get("position") or 0)
    if keeps_cursor:
        for k, qid in enumerate(rotated):
            if qid in chosen and qid not in seen:
                cursor_next = (pos + k + 1) % n

    queue = []
    for qid in warm_ids:
        st = qstate(prog, qid)
        # §4.2.7：考前 48 小时「自动切 R4 速览为主」。复习段统一压成速览，
        # 新题段保持 R1 —— 新材料还是得真答一遍，光看不算过。
        queue.append({"qid": qid, "phase": "warmup",
                      "mode": "R4" if final48 else pick_review_mode(st)})
    for qid in new_ids:
        queue.append({"qid": qid, "phase": "new", "mode": "R1"})

    return {
        "queue": queue,
        "cursor_next": cursor_next,
        "cursor_mode": order_mode,
        "quota": {"warmup": len(warm_ids), "new": len(new_ids), "cooldown": n_cool},
    }


def interleave(ids, pool):
    """交错：热身段禁止同一 domain 连续超过 2 题。"""
    out, buf = [], list(ids)
    while buf:
        placed = False
        for i, qid in enumerate(buf):
            d = pool[qid].get("domain")
            tail = [pool[x].get("domain") for x in out[-2:]]
            if len(tail) == 2 and tail[0] == tail[1] == d and d is not None:
                continue
            out.append(buf.pop(i))
            placed = True
            break
        if not placed:
            out.append(buf.pop(0))
    return out


SESSIONS = {}


# ==========================================================================
# 模拟考试
# ==========================================================================

def domain_quota(size, mix):
    """按 mix 比例给各 domain 分名额，用最大余数法保证合计**恰好** = size。

    别退回逐域 round(size * ratio)：比例和虽然是 1.0，各项独立取整后合计会漂。
    65 题时四个域取整得 20/17/16/13 = 66，多出的一个名额最后被 picked[:size]
    截掉 —— 而截断按 dict 顺序发生，等于每次都固定从最后一个域（cost）扣人，
    实测三个 seed 下 cost 恒为 12。这种偏差不报错，只是考试配比一直不对。
    """
    base = {d: int(size * r) for d, r in mix.items()}
    short = size - sum(base.values())
    # 余数大的先补；余数相同按域名排序，保证同 size 下结果稳定可复现
    order = sorted(mix, key=lambda d: (-(size * mix[d] - base[d]), d))
    for d in order[:max(0, short)]:
        base[d] += 1
    return base


def build_exam(bank, seed=None):
    rng = random.Random(seed)
    pool = bank.pool()
    tagged = [q for q in pool if q.get("domain")]
    picked = []
    # 分层抽样的名额按**标签覆盖率**分配，不能整卷都从 tagged 里挑。
    # §3.2 把 domain 定位成「可选加分项，无把握则填 null」，§4.1 的抽样池是
    # 「排除 needs_review 之后的全部题」—— 原先只要 tagged ≥ 65 就把 65 个名额
    # 全部按域分掉，617 行的补位恒为 0，domain=null 的 234 题（占可出题池 36%）
    # 在任何 seed、任何次数的考试里都抽不到。
    n_tagged = round(EXAM_SIZE * len(tagged) / len(pool)) if pool else 0
    n_tagged = min(n_tagged, len(tagged))
    if n_tagged >= len(DOMAIN_MIX):  # 名额太少就没有分层的意义，退化成纯随机
        by_dom = {}
        for q in tagged:
            by_dom.setdefault(q["domain"], []).append(q)
        quota = domain_quota(n_tagged, DOMAIN_MIX)
        for dom in DOMAIN_MIX:
            avail = by_dom.get(dom, [])
            rng.shuffle(avail)
            picked.extend(avail[:quota[dom]])
    # 用 id 判重：picked 里是 dict，`q not in picked` 会逐题做深比较，白烧 O(n²)
    taken = {q["id"] for q in picked}
    rest = [q for q in pool if q["id"] not in taken]
    rng.shuffle(rest)
    picked.extend(rest[: max(0, EXAM_SIZE - len(picked))])
    picked = picked[:EXAM_SIZE]
    rng.shuffle(picked)
    return picked


def score_exam(settings, results):
    n = len(results)
    right = sum(1 for r in results if r["correct"])
    if settings.get("scoring_mode") == "aws_scaled":
        score = round(100 + 900 * (right / n)) if n else 0
    else:
        score = round(1000 * right / n) if n else 0
    return score, right


# ==========================================================================
# HTTP
# ==========================================================================

class BadRequest(ValueError):
    """客户端传错了参数 —— 回 400，不打 traceback。"""


class Handler(BaseHTTPRequestHandler):
    server_version = "SAAQuiz/1.0"

    def log_message(self, *a):
        pass

    # -- helpers --------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ---------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, index_html(), "text/html; charset=utf-8")
            if u.path == "/api/bootstrap":
                return self._json(self.api_bootstrap())
            if u.path == "/api/session/next":
                return self._json(self.api_session_next())
            if u.path == "/api/stats":
                return self._json(self.api_stats())
            if u.path == "/api/wrongbook":
                return self._json(self.api_wrongbook())
            if u.path == "/api/browse":
                return self._json(self.api_browse(q))
            if u.path == "/api/question":
                return self._json(self.api_question(q))
            return self._json({"error": "not found"}, 404)
        except BadRequest as e:
            # 入参问题是客户端的事，回 400 就够了：既不该报 500，也不该往日志里
            # 打一份 traceback（原先所有异常一律 500 + 打印堆栈 + 把 Python 的
            # 原始异常文本回给浏览器，前端直接 toast 那段英文）。
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/session/start":
                return self._json(self.api_session_start(body))
            if u.path == "/api/session/advance":
                return self._json(self.api_session_advance(body))
            if u.path == "/api/answer":
                return self._json(self.api_answer(body))
            if u.path == "/api/selfassess":
                return self._json(self.api_selfassess(body))
            if u.path == "/api/exam/start":
                return self._json(self.api_exam_start(body))
            if u.path == "/api/exam/submit":
                return self._json(self.api_exam_submit(body))
            if u.path == "/api/settings":
                return self._json(self.api_settings(body))
            if u.path == "/api/wrongbook/remove":
                return self._json(self.api_wrongbook_remove(body))
            return self._json({"error": "not found"}, 404)
        except BadRequest as e:
            # 入参问题是客户端的事，回 400 就够了：既不该报 500，也不该往日志里
            # 打一份 traceback（原先所有异常一律 500 + 打印堆栈 + 把 Python 的
            # 原始异常文本回给浏览器，前端直接 toast 那段英文）。
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    # -- API ------------------------------------------------------------
    def api_bootstrap(self):
        BANK.refresh_if_changed()
        with _LOCK:
            prog = load_progress()
            settings = read_json(F_SETTINGS, DEFAULT_SETTINGS)
            for k, v in DEFAULT_SETTINGS.items():
                settings.setdefault(k, v)
            pool = BANK.pool()
            due = sum(1 for qid, st in prog["questions"].items()
                      if is_due(st) and not st.get("mastered")
                      and int(qid) in {q["id"] for q in pool})
            wrong = load_wrong()["ids"]
            info = {
                "total": len(BANK.questions),
                "pool": len(pool),
                "answered": len(prog["questions"]),
                "mastered": prog["stats"].get("mastered", 0),
                "false_mastery": prog["stats"].get("false_mastery", 0),
                "wrong": len(wrong),
                "due_today": due,
                "last_question_id": prog["cursor"].get("last_question_id"),
            }
            exam_info = None
            # 横幅上的 sprint / final48 与 build_session 实际生效的判据必须同源，
            # 否则又会退回「页面宣称一套、出题另一套」。
            left = days_to_exam(settings)
            if left is not None:
                sprint, final48 = exam_phase(settings)
                unmastered = len(pool) - info["mastered"]
                exam_info = {
                    "days_left": left,
                    "unmastered": unmastered,
                    "per_day": max(1, -(-unmastered // max(1, left))) if left > 0 else unmastered,
                    "sprint": sprint,
                    "final48": final48,
                }
            return {"settings": settings, "info": info,
                    "coverage": BANK.coverage(), "exam_countdown": exam_info}

    def api_session_start(self, body):
        BANK.refresh_if_changed()
        with _LOCK:
            prog = load_progress()
            settings = read_json(F_SETTINGS, DEFAULT_SETTINGS)
            for k, v in DEFAULT_SETTINGS.items():
                settings.setdefault(k, v)
            if body.get("only_wrong"):
                ids = [i for i in load_wrong()["ids"] if BANK.get(i)]
                plan = {"queue": [{"qid": i, "phase": "warmup",
                                   "mode": pick_review_mode(qstate(prog, i))}
                                  for i in ids],
                        "cursor_next": prog["cursor"].get("position", 0),
                        "quota": {"warmup": len(ids), "new": 0, "cooldown": 0}}
            else:
                plan = build_session(prog, settings, BANK, body.get("size"))
            sid = new_id()
            SESSIONS[sid] = {
                "id": sid, "queue": plan["queue"], "pos": 0,
                "quota": plan["quota"], "cursor_next": plan["cursor_next"],
                "cursor_mode": plan.get("cursor_mode"),
                "wrong": [], "cooldown_done": set(),
                "started": now_iso(), "results": [],
            }
            return {"session_id": sid, "quota": plan["quota"],
                    "total": len(plan["queue"])}

    def _session(self, sid):
        s = SESSIONS.get(sid)
        if not s:
            raise BadRequest("会话不存在或已过期，请重新开始")
        return s

    def api_session_next(self):
        BANK.refresh_if_changed()
        sid = parse_qs(urlparse(self.path).query).get("sid", [""])[0]
        with _LOCK:
            s = self._session(sid)
            # 主队列走完后，补 Cool-down：本次答错、且已间隔 ≥10 题的题
            if s["pos"] >= len(s["queue"]):
                quota = s["quota"].get("cooldown", 0)
                added = sum(1 for it in s["queue"] if it["phase"] == "cooldown")
                for w in s["wrong"]:
                    if added >= quota:
                        break
                    if w["qid"] in s["cooldown_done"]:
                        continue
                    if s["pos"] - w["pos"] < 10:
                        continue
                    s["queue"].append({"qid": w["qid"], "phase": "cooldown",
                                       "mode": "R1"})
                    s["cooldown_done"].add(w["qid"])
                    added += 1
            if s["pos"] >= len(s["queue"]):
                return {"done": True, "summary": self._finish(s)}

            item = s["queue"][s["pos"]]
            q = BANK.get(item["qid"])
            prog = load_progress()
            st = qstate(prog, item["qid"])
            seed = "%s-%s-%d" % (sid, item["qid"], s["pos"])
            mode = item["mode"]
            only = None
            if mode == "R2":
                keep = set(q.get("answer") or [])
                # 取「计数最高的、且不是正确答案的」那个字母当干扰项。
                # 不能只看 picks[0]：老的 progress.json 里 wrong_picks 混进了正确字母
                # （见 api_answer 里 missed 的注释），撞上就会白白降级成 R1。
                picks = sorted(st.get("wrong_picks", {}).items(),
                               key=lambda kv: -kv[1])
                distract = next((L for L, _ in picks
                                 if L not in keep and L in {o["letter"] for o in q["options"]}),
                                None)
                if distract:
                    keep.add(distract)
                    only = keep
                else:
                    mode = "R1"
            # R3 闪卡也要带上答案与解析：SPEC §4.2.2 写的是「心里回想 → 点『翻开』
            # **对照** → 自评」，没有答案就没法对照，自评三档等于瞎选。前端负责在
            # 「翻开」之前不渲染它们。R1/R2 是真答题，仍然不能下发。
            payload = render_question(q, seed, reveal=(mode in ("R3", "R4")),
                                      only_letters=only)
            counts = {p: sum(1 for it in s["queue"] if it["phase"] == p)
                      for p in ("warmup", "new", "cooldown")}
            idx_in_phase = sum(1 for it in s["queue"][:s["pos"] + 1]
                               if it["phase"] == item["phase"])
            return {
                "done": False, "question": payload, "phase": item["phase"],
                "mode": mode, "seed": seed,
                "pos": s["pos"] + 1, "total": len(s["queue"]),
                "phase_pos": idx_in_phase, "phase_total": counts[item["phase"]],
                "box": st.get("box", 1), "attempts": st.get("attempts", 0),
            }

    def api_session_advance(self, body):
        """R4 速览确认：只推进队列位置，不碰任何复习状态。

        SPEC §4.2.2 说「R4 不改变任何 box 状态」，所以 R4 屏不该调 /api/answer，
        也不该调 /api/selfassess —— 但队列位置只在那两个接口里 +1，于是 R4 题
        变成死循环：前端点「下一题」只发 GET /api/session/next，服务端 pos 不动，
        同一题无限返回，整场会话再也走不到 done，_finish() 永不执行。
        这个端点就是缺的那一半：推进位置，别的什么都不做。

        cursor.last_question_id 故意不更新 —— 速览的是复习旧题，
        首页「上次学到第 N 题」应该继续指向新题推进的进度。
        """
        with _LOCK:
            s = self._session(body.get("session_id"))
            if s["pos"] < len(s["queue"]):
                s["pos"] += 1
            return {"ok": True, "pos": s["pos"], "total": len(s["queue"])}

    def _finish(self, s):
        prog = load_progress()
        prog["cursor"]["position"] = s["cursor_next"]
        prog["cursor"]["mode"] = s.get("cursor_mode") or prog["cursor"].get("mode")
        # 「上次学到第 N 题」只跟新题段走。原先取 results[-1]，而收尾重测的是本场
        # 早期答错的旧题 —— 推进到第 21 题、首页却显示「上次学到第 4 题」。
        new_done = [r["qid"] for r in s["results"] if r.get("phase") == "new"]
        if new_done:
            prog["cursor"]["last_question_id"] = new_done[-1]
        prog["last_session"] = {
            "ended_at": now_iso(),
            "counts": {p: sum(1 for it in s["queue"] if it["phase"] == p)
                       for p in ("warmup", "new", "cooldown")},
            "wrong_ids": sorted({w["qid"] for w in s["wrong"]}),
        }
        save_progress(prog)
        right = sum(1 for r in s["results"] if r["correct"])
        return {"answered": len(s["results"]), "correct": right,
                "wrong_ids": sorted({w["qid"] for w in s["wrong"]}),
                "counts": prog["last_session"]["counts"]}

    def api_answer(self, body):
        sid = body.get("session_id")
        # 缺 id 时 int(None) 抛 TypeError、题号不存在时下面 q.get() 抛
        # AttributeError，两者都变成 HTTP 500 并把原始 Python 异常文本回给浏览器
        # （前端直接 toast 那段英文）。api_question 早就有这层校验，这里是漏了。
        qid = as_qid(body.get("id"))
        if qid is None or not BANK.get(qid):
            raise BadRequest("题号不存在或缺失：%r" % (body.get("id"),))
        picked_new = [str(x).upper() for x in (body.get("picked") or [])]
        confidence = body.get("confidence") or "unsure"
        letter_map = body.get("letter_map") or {}
        new2orig = {v: k for k, v in letter_map.items()}
        picked_orig = sorted(new2orig.get(p, p) for p in picked_new)

        with _LOCK:
            q = BANK.get(qid)
            correct = sorted(q.get("answer") or [])
            is_right = bool(correct) and picked_orig == correct
            settings = read_json(F_SETTINGS, DEFAULT_SETTINGS)
            for k, v in DEFAULT_SETTINGS.items():
                settings.setdefault(k, v)
            prog = load_progress()

            s = SESSIONS.get(sid)
            phase = "new"
            if s and s["pos"] < len(s["queue"]):
                phase = s["queue"][s["pos"]]["phase"]

            # wrong_picks 只记「真正选错的那些字母」。原先整题判错时把 picked_orig
            # 整个塞进去，多选题只要选中了部分正确项，正确字母也会被记成干扰项 ——
            # 而它的计数只增不减、永远排在最前，R2 取到它之后走
            # `distract not in keep` 的 else 分支静默降级成 R1，
            # 这道题的干扰项狙击档就此永久失效。86 道多选题几乎必然踩中。
            missed = [L for L in picked_orig if L not in correct]

            # Cool-down 的重测只用于当场巩固，不计入 box 升降
            if phase != "cooldown":
                st = apply_result(prog, settings, qid, is_right,
                                  confidence, None if is_right else missed)
            else:
                st = qstate(prog, qid)
            # 只有新题段才推进「上次学到第 N 题」——热身与收尾重测的是旧题，
            # 拿它们改 cursor 会让首页的进度倒退。
            if phase == "new":
                prog["cursor"]["last_question_id"] = qid
            save_progress(prog)

            if not is_right:
                wrong_add(qid)
            elif confidence == "sure" and st.get("mastered"):
                wrong_remove(qid)

            if s:
                if not is_right and phase != "cooldown":
                    s["wrong"].append({"qid": qid, "pos": s["pos"]})
                s["results"].append({"qid": qid, "correct": is_right, "phase": phase})
                s["pos"] += 1

            rq = render_question(q, body.get("seed") or "", reveal=True)
            # 用客户端本次的映射重新渲染解析，保证字母引用与屏幕一致
            rq["answer"] = sorted(letter_map.get(a, a) for a in correct)
            rq["explanation_en"] = remap_letters(q.get("explanation_en"), letter_map)
            rq["explanation_zh"] = remap_letters(q.get("explanation_zh"), letter_map)
            return {
                "correct": is_right,
                "answer": rq["answer"],
                "answer_orig": correct,
                "explanation_en": rq["explanation_en"],
                "explanation_zh": rq["explanation_zh"],
                "explanation_quality": q.get("explanation_quality", "ok"),
                "box": st.get("box", 1),
                "mastered": st.get("mastered", False),
                "false_mastery": st.get("false_mastery", False),
                "counts_as_review": phase != "cooldown",
            }

    def api_selfassess(self, body):
        """R3 闪卡自评：记得→等同答对，模糊→box 不动，忘了→等同答错。"""
        # 原先全程不查题库：任意题号都会返回 200，并在 progress.json 里凭空造出
        # 一条 SRS 状态，永久污染 stats["answered"] 与已练题数。
        qid = as_qid(body.get("id"))
        if qid is None or not BANK.get(qid):
            raise BadRequest("题号不存在或缺失：%r" % (body.get("id"),))
        grade = body.get("grade")  # remember | vague | forgot
        with _LOCK:
            settings = read_json(F_SETTINGS, DEFAULT_SETTINGS)
            for k, v in DEFAULT_SETTINGS.items():
                settings.setdefault(k, v)
            prog = load_progress()
            if grade == "remember":
                st = apply_result(prog, settings, qid, True, "sure", None)
            elif grade == "forgot":
                st = apply_result(prog, settings, qid, False, "unsure", None)
                wrong_add(qid)
            else:
                st = qstate(prog, qid)
                st["last_seen_at"] = now_iso()
                st["attempts"] += 1
                prog["stats"]["answered"] += 1
            # R3 只出现在热身段，不推进「上次学到第 N 题」（同 api_answer 的口径）
            save_progress(prog)
            s = SESSIONS.get(body.get("session_id"))
            if s:
                s["results"].append({"qid": qid, "correct": grade == "remember",
                                     "phase": "warmup"})
                if grade == "forgot":
                    s["wrong"].append({"qid": qid, "pos": s["pos"]})
                s["pos"] += 1
            return {"ok": True, "box": st.get("box", 1)}

    def api_question(self, q):
        BANK.refresh_if_changed()
        qid = int(q.get("id", ["0"])[0])
        item = BANK.get(qid)
        if not item:
            return {"error": "题号不存在"}
        seed = q.get("seed", [str(qid)])[0]
        return {"question": render_question(item, seed, reveal=True)}

    def api_browse(self, q):
        BANK.refresh_if_changed()
        page = int(q.get("page", ["1"])[0])
        size = 20
        only_review = q.get("review", ["0"])[0] == "1"
        ids = [i for i in BANK.order
               if (not only_review) or BANK.questions[i]["needs_review"]]
        total = len(ids)
        chunk = ids[(page - 1) * size: page * size]
        rows = []
        for i in chunk:
            x = BANK.questions[i]
            rows.append({
                "id": i, "stem_en": x["stem_en"][:150],
                "stem_zh": (x.get("stem_zh") or "")[:120],
                "answer": x.get("answer"), "type": x["type"],
                "needs_review": x["needs_review"],
                "review_reason": x.get("review_reason"),
                "domain": x.get("domain"),
            })
        return {"rows": rows, "total": total, "page": page,
                "pages": max(1, -(-total // size))}

    def api_wrongbook_remove(self, body):
        """从错题本移出一题。

        这里原先是路由里直接调 wrong_remove()，是全仓唯一一条**不持 _LOCK** 的
        写盘路径（其余 6 处 atomic_write_json 都在锁内）。和 api_answer 里的
        wrong_add 撞上就会读-改-写互相覆盖，移出被静默吞掉。
        """
        qid = as_qid(body.get("id"))
        if qid is None:
            raise BadRequest("缺少题号")
        with _LOCK:
            wrong_remove(qid)
        return {"ok": True}

    def api_wrongbook(self):
        BANK.refresh_if_changed()
        prog = load_progress()
        rows = []
        for i in load_wrong()["ids"]:
            q = BANK.get(i)
            if not q:
                continue
            st = qstate(prog, i)
            rows.append({
                "id": i, "stem_en": q["stem_en"][:160],
                "stem_zh": (q.get("stem_zh") or "")[:130],
                "box": st.get("box", 1), "wrong": st.get("wrong", 0),
                "correct": st.get("correct", 0),
                "false_mastery": st.get("false_mastery", False),
                "domain": q.get("domain"),
            })
        return {"rows": rows}

    def api_stats(self):
        BANK.refresh_if_changed()
        prog = load_progress()
        pool_ids = {q["id"] for q in BANK.pool()}
        boxes = {i: 0 for i in range(1, 6)}
        mastered = 0
        for qid, st in prog["questions"].items():
            if int(qid) not in pool_ids:
                continue
            if st.get("mastered"):
                mastered += 1
                continue
            boxes[max(1, min(5, st.get("box", 1)))] += 1
        untouched = len(pool_ids) - sum(boxes.values()) - mastered

        # 未来 7 天到期预测
        forecast = []
        today = datetime.now().astimezone().date()
        for d in range(7):
            day = today + timedelta(days=d)
            n = 0
            for qid, st in prog["questions"].items():
                if int(qid) not in pool_ids or st.get("mastered"):
                    continue
                try:
                    due = datetime.fromisoformat(st["next_due_at"]).date()
                except Exception:
                    continue
                if due <= day if d == 0 else due == day:
                    n += 1
            forecast.append({"date": day.isoformat(), "count": n})

        fm = []
        for qid, st in prog["questions"].items():
            if st.get("false_mastery") and int(qid) in pool_ids:
                q = BANK.get(int(qid))
                fm.append({"id": int(qid), "stem": (q.get("stem_zh") or q["stem_en"])[:90],
                           "box": st.get("box", 1),
                           "guesses": st.get("attempts", 0)})
        fm.sort(key=lambda r: -r["guesses"])

        dom = {}
        for qid, st in prog["questions"].items():
            q = BANK.get(int(qid))
            if not q or not q.get("domain"):
                continue
            d = dom.setdefault(q["domain"], {"a": 0, "c": 0})
            d["a"] += st.get("attempts", 0)
            d["c"] += st.get("correct", 0)
        domain_rows = [{"domain": k, "attempts": v["a"], "correct": v["c"],
                        "pct": round(100.0 * v["c"] / v["a"], 1) if v["a"] else 0.0}
                       for k, v in sorted(dom.items())]
        domain_rows.sort(key=lambda r: r["pct"])

        return {"boxes": boxes, "mastered": mastered, "untouched": max(0, untouched),
                "forecast": forecast, "false_mastery": fm[:40],
                "domains": domain_rows, "stats": prog["stats"],
                "coverage": BANK.coverage()}

    def api_exam_start(self, body):
        BANK.refresh_if_changed()
        seed = body.get("seed")
        with _LOCK:
            picked = build_exam(BANK, seed)
            eid = new_id()
            qs = []
            for idx, q in enumerate(picked):
                s = "%s-%d" % (eid, q["id"])
                qs.append(render_question(q, s))
            SESSIONS["exam:" + eid] = {
                "ids": [q["id"] for q in picked],
                "seeds": {q["id"]: "%s-%d" % (eid, q["id"]) for q in picked},
                "started": now_iso(),
            }
            return {"exam_id": eid, "questions": qs,
                    "minutes": EXAM_MINUTES, "pass_score": PASS_SCORE}

    def api_exam_submit(self, body):
        eid = body.get("exam_id")
        answers = body.get("answers") or {}
        elapsed = int(body.get("elapsed_sec") or 0)
        with _LOCK:
            settings = read_json(F_SETTINGS, DEFAULT_SETTINGS)
            for k, v in DEFAULT_SETTINGS.items():
                settings.setdefault(k, v)
            sess = SESSIONS.get("exam:" + eid)
            if not sess:
                raise BadRequest("考试会话不存在")
            results = []
            prog = load_progress()
            for qid in sess["ids"]:
                q = BANK.get(qid)
                a = answers.get(str(qid)) or {}
                lm = a.get("letter_map") or {}
                n2o = {v: k for k, v in lm.items()}
                picked = sorted(n2o.get(p, p) for p in (a.get("picked") or []))
                corr = sorted(q.get("answer") or [])
                ok = bool(corr) and picked == corr
                results.append({
                    "qid": qid, "correct": ok, "picked": picked,
                    "answer": corr, "domain": q.get("domain"),
                })
                if not ok:
                    wrong_add(qid)
            score, right = score_exam(settings, results)
            by_dom = {}
            for r in results:
                d = by_dom.setdefault(r["domain"] or "未分类", {"n": 0, "c": 0})
                d["n"] += 1
                d["c"] += 1 if r["correct"] else 0
            record = {
                "exam_id": eid, "started": sess["started"], "ended": now_iso(),
                "elapsed_sec": elapsed, "size": len(results),
                "correct": right, "score": score,
                "passed": score >= PASS_SCORE,
                "scoring_mode": settings.get("scoring_mode"),
                "by_domain": by_dom, "results": results,
            }
            os.makedirs(EXAMS, exist_ok=True)
            # 带上 exam_id，避免同一秒内交两次卷互相覆盖
            fn = "exam_%s_%s.json" % (datetime.now().strftime("%Y%m%d_%H%M%S"), eid[:4])
            atomic_write_json(os.path.join(EXAMS, fn), record)
            save_progress(prog)
            review = []
            for r in results:
                q = BANK.get(r["qid"])
                seed = sess["seeds"][r["qid"]]
                rq = render_question(q, seed, reveal=True)
                review.append({
                    "id": r["qid"], "correct": r["correct"],
                    "picked_orig": r["picked"], "question": rq,
                })
            return {"record": {k: v for k, v in record.items() if k != "results"},
                    "review": review, "file": fn}

    def api_settings(self, body):
        with _LOCK:
            s = read_json(F_SETTINGS, DEFAULT_SETTINGS)
            for k, v in DEFAULT_SETTINGS.items():
                s.setdefault(k, v)
            for k in DEFAULT_SETTINGS:
                if k in body:
                    s[k] = body[k]
            atomic_write_json(F_SETTINGS, s)
            return {"settings": s}


# ==========================================================================
# 前端
# ==========================================================================
#
# 页面本体在 web/index.html。SPEC §技术选型 要求「单文件 HTML，CSS/JS 全部内联，
# 禁止引用任何 CDN」—— 那条管的是**发出去的页面**，所以这里整份读出来原样返回，
# 不拆成 /static/*.css、/static/*.js 之类的额外请求。

def index_html():
    """读 web/index.html，按 mtime 热加载。

    改完前端刷新浏览器就行，不用重启服务 —— 和 Bank 对题库/译文的做法一致。
    文件在不在由 main() 启动时查；这里不做 SystemExit，那是 BaseException，
    handler 的 `except Exception` 拦不住，会直接掐掉当前请求线程。
    """
    global _INDEX_CACHE, _INDEX_MTIME
    mt = os.path.getmtime(F_INDEX)
    if _INDEX_CACHE is None or mt != _INDEX_MTIME:
        with open(F_INDEX, "r", encoding="utf-8") as f:
            _INDEX_CACHE = f.read()
        _INDEX_MTIME = mt
    return _INDEX_CACHE


# ==========================================================================

def main():
    global BANK
    ap = argparse.ArgumentParser(description="AWS SAA-C03 刷题程序")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    os.makedirs(EXAMS, exist_ok=True)
    print("启动 AWS SAA-C03 刷题程序")
    if not os.path.exists(F_INDEX):
        raise SystemExit("找不到 %s —— 前端页面在这个文件里，别删也别挪走"
                         % os.path.relpath(F_INDEX, ROOT))
    BANK = Bank()
    cov = BANK.coverage()
    print("  题库 %d 题，可出题 %d 题" % (len(BANK.questions), len(BANK.pool())))
    print("  选项中文覆盖率 %.1f%%（%d/%d）"
          % (cov["options_pct"], cov["options_zh"], cov["options_total"]))
    if cov["options_pct"] < 95:
        warn("选项中文覆盖率未达 95%，缺译处将显示英文原文 + 灰色角标")
    if not os.path.exists(F_I18N):
        warn("未找到 data/i18n_zh.jsonl，选项暂无中文（程序照常可用）")

    url = "http://127.0.0.1:%d/" % args.port
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("  监听 %s" % url)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")


if __name__ == "__main__":
    main()
