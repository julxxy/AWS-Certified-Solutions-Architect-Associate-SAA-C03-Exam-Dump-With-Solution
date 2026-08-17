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
    """先写 .tmp 再 os.replace()，进程被杀也不会留下半截 JSON。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


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
        # 分母只算「可译」的选项。477 题的选项在 PDF 里是图片、没有英文原文，
        # 永远补不出译文；把它们算进分母，覆盖率就会永久卡在 99.9% 下不来，
        # 而 verify_bank.py 用的是可译分母、报 100%，两边对不上会让人以为漏译了。
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
_RE_OPTREF = re.compile(
    r"(?P<pfx>"
    r"\bOptions?\s+"
    r"|\bCorrect\s+answers?\s*[:：]?\s*"
    r"|\bAnswers?\s*[:：]\s*"
    r"|正确答案\s*[:：]?\s*"
    r"|答案\s*[:：]\s*"
    r"|选项\s*"
    r")(?P<L1>[A-F])(?![A-Za-z])"
    r"|(?<![A-Za-z0-9])(?P<L2>[A-F])(?=\s*[\)）])"
    r"|^(?P<L3>[A-F])(?=[\.、]\s)",
    re.M)


def remap_letters(text, mapping):
    """把解析里的字母引用按本次乱序映射改写。中英两份都要过一遍。"""
    if not text:
        return text

    def rep(m):
        if m.group("L1"):
            return m.group("pfx") + mapping.get(m.group("L1"), m.group("L1"))
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
    if only_letters:
        opts = [o for o in opts if o["letter"] in only_letters]
    rng = random.Random(seed)
    rng.shuffle(opts)

    orig2new, shown = {}, []
    for i, o in enumerate(opts):
        new_letter = chr(ord("A") + i)
        orig2new[o["letter"]] = new_letter
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

    if correct:
        st["correct"] += 1
        st["last_result"] = "correct"
        st["streak"] = st.get("streak", 0) + 1
        d = today_str()
        if d not in st["correct_dates"]:
            st["correct_dates"].append(d)
        if confidence == "guess":
            # 对 + 蒙的 → 按答错处理，box 不升，记为「假掌握」
            st["false_mastery"] = True
            factor = 1.0
        elif confidence == "unsure":
            st["box"] = min(5, st["box"] + 1)
            factor = 0.5  # 升 box 但 next_due 折半
        else:
            st["box"] = min(5, st["box"] + 1)
            st["false_mastery"] = False
            factor = 1.0
    else:
        st["wrong"] += 1
        st["last_result"] = "wrong"
        st["streak"] = 0
        st["box"] = 1
        factor = 1.0
        if confidence == "sure":
            # 错 + 有把握 → 置顶到下次会话热身段第一位
            st["high_conf_error"] = True
        if picked_orig:
            for L in picked_orig:
                st["wrong_picks"][L] = st["wrong_picks"].get(L, 0) + 1

    days = interval_days(st["box"], settings) * factor
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


def is_due(st, ref=None):
    if not st.get("next_due_at"):
        return st.get("attempts", 0) > 0
    try:
        due = datetime.fromisoformat(st["next_due_at"])
    except Exception:
        return True
    return due <= (ref or datetime.now().astimezone())


def overdue_days(st):
    try:
        return max(0, (datetime.now().astimezone()
                       - datetime.fromisoformat(st["next_due_at"])).days)
    except Exception:
        return 0


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

    n_warm = max(0, round(size * 0.30))
    n_cool = max(0, round(size * 0.15))
    n_new = size - n_warm - n_cool

    # ---- 热身段：错题 + 今日到期题，按 §4.2.5 优先级排序 ----
    cands = []
    for qid, st in prog["questions"].items():
        qid = int(qid)
        if qid not in pool or st.get("mastered"):
            continue
        if not (is_due(st) or st.get("last_result") == "wrong"):
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
    seen = set(int(k) for k in prog["questions"])
    order_mode = settings.get("order", "sequential")
    ids = sorted(pool)
    if order_mode == "random":
        random.shuffle(ids)
    elif order_mode == "review_first":
        ids = [i for i in ids if i in seen] + [i for i in ids if i not in seen]

    pos = int(prog["cursor"].get("position") or 0)
    new_ids, i, guard = [], pos, 0
    while len(new_ids) < n_new and guard < len(ids) * 2:
        qid = ids[i % len(ids)]
        if qid not in warm_ids and (order_mode != "sequential" or qid not in seen):
            new_ids.append(qid)
        i += 1
        guard += 1
    if len(new_ids) < n_new:  # 新题用尽，用未掌握的旧题补齐
        for qid in ids:
            if len(new_ids) >= n_new:
                break
            if qid in warm_ids or qid in new_ids:
                continue
            if prog["questions"].get(str(qid), {}).get("mastered"):
                continue
            new_ids.append(qid)

    queue = []
    for qid in warm_ids:
        st = qstate(prog, qid)
        queue.append({"qid": qid, "phase": "warmup", "mode": pick_review_mode(st)})
    for qid in new_ids:
        queue.append({"qid": qid, "phase": "new", "mode": "R1"})

    return {
        "queue": queue,
        "cursor_next": i % len(ids) if ids else 0,
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
    if len(tagged) >= EXAM_SIZE:
        by_dom = {}
        for q in tagged:
            by_dom.setdefault(q["domain"], []).append(q)
        quota = domain_quota(EXAM_SIZE, DOMAIN_MIX)
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
                wrong_remove(body.get("id"))
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
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
            ed = settings.get("exam_date")
            if ed:
                try:
                    left = (date.fromisoformat(ed) - date.today()).days
                    unmastered = len(pool) - info["mastered"]
                    exam_info = {
                        "days_left": left,
                        "unmastered": unmastered,
                        "per_day": max(1, -(-unmastered // max(1, left))) if left > 0 else unmastered,
                        "sprint": 0 < left <= 7,
                        "final48": 0 < left <= 2,
                    }
                except Exception:
                    exam_info = None
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
                "wrong": [], "cooldown_done": set(),
                "started": now_iso(), "results": [],
            }
            return {"session_id": sid, "quota": plan["quota"],
                    "total": len(plan["queue"])}

    def _session(self, sid):
        s = SESSIONS.get(sid)
        if not s:
            raise ValueError("会话不存在或已过期，请重新开始")
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
                picks = sorted(st.get("wrong_picks", {}).items(),
                               key=lambda kv: -kv[1])
                distract = picks[0][0] if picks else None
                keep = set(q.get("answer") or [])
                if distract and distract not in keep:
                    keep.add(distract)
                    only = keep
                else:
                    mode = "R1"
            payload = render_question(q, seed, reveal=(mode == "R4"),
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

    def _finish(self, s):
        prog = load_progress()
        prog["cursor"]["position"] = s["cursor_next"]
        if s["results"]:
            prog["cursor"]["last_question_id"] = s["results"][-1]["qid"]
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
        qid = int(body.get("id"))
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

            # Cool-down 的重测只用于当场巩固，不计入 box 升降
            if phase != "cooldown":
                st = apply_result(prog, settings, qid, is_right,
                                  confidence, None if is_right else picked_orig)
            else:
                st = qstate(prog, qid)
            prog["cursor"]["last_question_id"] = qid
            save_progress(prog)

            if not is_right:
                wrong_add(qid)
            elif confidence == "sure" and st.get("mastered"):
                wrong_remove(qid)

            if s:
                if not is_right and phase != "cooldown":
                    s["wrong"].append({"qid": qid, "pos": s["pos"]})
                s["results"].append({"qid": qid, "correct": is_right})
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
        qid = int(body.get("id"))
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
            prog["cursor"]["last_question_id"] = qid
            save_progress(prog)
            s = SESSIONS.get(body.get("session_id"))
            if s:
                s["results"].append({"qid": qid, "correct": grade == "remember"})
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
                raise ValueError("考试会话不存在")
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
