#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — AWS SAA-C03 中英对照刷题程序（本地、离线、仅标准库）

    python3 app.py            # 监听 127.0.0.1:8765 并自动打开浏览器
    python3 app.py --no-open  # 不自动开浏览器
    python3 app.py --port N

模式 A 模拟考试：65 题 / 130 分钟 / 720 分及格
模式 B 滚动学习：三段式会话 + Leitner 间隔重复 + 四档复习强度 + 信心度打分
"""

import argparse
import hashlib
import json
import os
import random
import re
import threading
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
EXAMS = os.path.join(DATA, "exams")

F_QUESTIONS = os.path.join(DATA, "questions.json")
F_I18N = os.path.join(DATA, "i18n_zh.jsonl")
F_PROGRESS = os.path.join(DATA, "progress.json")
F_WRONG = os.path.join(DATA, "wrong_book.json")
F_SETTINGS = os.path.join(DATA, "settings.json")

BOX_INTERVALS = [1, 2, 4, 9, 21]        # Leitner 5 档，单位天。box1 是 1 天不是 0 天
EXAM_SIZE = 65
EXAM_MINUTES = 130
PASS_SCORE = 720
DOMAIN_MIX = {"secure": 0.30, "resilient": 0.26, "performant": 0.24, "cost": 0.20}

DEFAULT_SETTINGS = {
    "lang_mode": "both",          # zh | en | both
    "timer_enabled": True,
    "scoring_mode": "linear",     # linear | aws_scaled
    "partial_credit": False,
    "theme": "system",            # system | light | dark
    "exam_date": None,            # "YYYY-MM-DD"
    "session_size": 25,
    "order": "sequential",        # sequential | random | review_first
}

_LOCK = threading.RLock()


# ==========================================================================
# 存储
# ==========================================================================

def now_iso():
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


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
                "找不到 data/questions.json，请先运行：python3 build_bank.py")
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
        tot_o = sum(len(q["options"]) for q in self.questions.values())
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
            factor = 0.5           # 升 box 但 next_due 折半
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
        return "R2"                      # 有错误记录 → 干扰项狙击，优先于 R1
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
    if len(new_ids) < n_new:      # 新题用尽，用未掌握的旧题补齐
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

def build_exam(bank, seed=None):
    rng = random.Random(seed)
    pool = bank.pool()
    tagged = [q for q in pool if q.get("domain")]
    picked = []
    if len(tagged) >= EXAM_SIZE:
        by_dom = {}
        for q in tagged:
            by_dom.setdefault(q["domain"], []).append(q)
        for dom, ratio in DOMAIN_MIX.items():
            want = round(EXAM_SIZE * ratio)
            avail = by_dom.get(dom, [])
            rng.shuffle(avail)
            picked.extend(avail[:want])
    rest = [q for q in pool if q not in picked]
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
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
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
            sid = hashlib.md5(now_iso().encode()).hexdigest()[:10]
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
        grade = body.get("grade")     # remember | vague | forgot
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
            eid = hashlib.md5((now_iso() + str(seed)).encode()).hexdigest()[:10]
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
# 前端（单文件，CSS/JS 全内联，无任何外链）
# ==========================================================================

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWS SAA-C03 刷题</title>
<style>
:root{
  --bg:#f6f7f9; --fg:#1a1d21; --muted:#6b7280; --card:#fff; --line:#e3e6ea;
  --pri:#0f62fe; --pri-fg:#fff; --ok:#0e8a4f; --bad:#d32f2f; --warn:#b26a00;
  --chip:#eef1f5; --shadow:0 1px 3px rgba(0,0,0,.07);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#14171a; --fg:#e6e8ea; --muted:#9aa3ad; --card:#1d2126; --line:#2c3238;
    --pri:#5b9bff; --pri-fg:#0b0d10; --ok:#3ec97f; --bad:#ff6b6b; --warn:#f0a848;
    --chip:#262b31; --shadow:0 1px 3px rgba(0,0,0,.4);
  }
}
:root[data-theme=dark]{
  --bg:#14171a; --fg:#e6e8ea; --muted:#9aa3ad; --card:#1d2126; --line:#2c3238;
  --pri:#5b9bff; --pri-fg:#0b0d10; --ok:#3ec97f; --bad:#ff6b6b; --warn:#f0a848;
  --chip:#262b31; --shadow:0 1px 3px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Segoe UI,Roboto,sans-serif}
code,kbd,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:var(--pri)}
header{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);
  display:flex;gap:8px;align-items:center;padding:10px 14px;flex-wrap:wrap;box-shadow:var(--shadow)}
header .sp{flex:1}
.brand{font-weight:700;letter-spacing:.2px;margin-right:6px}
button,select,input[type=date],input[type=number]{font:inherit;color:inherit;background:var(--card);
  border:1px solid var(--line);border-radius:8px;padding:6px 11px;cursor:pointer}
button:hover{border-color:var(--pri)}
button.pri{background:var(--pri);color:var(--pri-fg);border-color:var(--pri);font-weight:600}
button.ghost{background:transparent}
button:disabled{opacity:.45;cursor:not-allowed}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;padding:6px 10px}
.seg button.on{background:var(--pri);color:var(--pri-fg)}
main{max-width:940px;margin:0 auto;padding:18px 14px 80px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:var(--shadow)}
h2{margin:.2em 0 .6em;font-size:19px}
h3{margin:1.2em 0 .5em;font-size:15px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.kpi{background:var(--chip);border-radius:10px;padding:12px}
.kpi b{display:block;font-size:24px;line-height:1.2}
.kpi span{color:var(--muted);font-size:12px}
.muted{color:var(--muted)}
.small{font-size:13px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 9px;font-size:12px;color:var(--muted);margin-right:6px}
.chip.warn{color:var(--warn)}
.badge{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:var(--muted);margin-left:6px;vertical-align:middle}
.stem{font-size:16px;white-space:pre-wrap}
.stem .zh{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}
.opt{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;
  padding:11px 13px;margin:9px 0;cursor:pointer;background:var(--card)}
.opt:hover{border-color:var(--pri)}
.opt.sel{border-color:var(--pri);box-shadow:0 0 0 2px color-mix(in srgb,var(--pri) 22%,transparent)}
.opt.ok{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,var(--card))}
.opt.no{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 9%,var(--card))}
.opt .L{font-weight:700;min-width:20px}
.opt .tx{flex:1}
.opt .tx .zh{color:var(--muted);margin-top:3px}
.opt.hide .tx{filter:blur(6px);user-select:none}
.bar{height:6px;background:var(--chip);border-radius:99px;overflow:hidden;margin:10px 0}
.bar i{display:block;height:100%;background:var(--pri)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}
.stack{display:flex;height:26px;border-radius:6px;overflow:hidden;background:var(--chip)}
.stack i{display:block;height:100%}
.spark{display:flex;gap:5px;align-items:flex-end;height:80px}
.spark div{flex:1;background:var(--pri);border-radius:4px 4px 0 0;min-height:2px;position:relative}
.spark div span{position:absolute;top:-17px;left:0;right:0;text-align:center;font-size:11px;color:var(--muted)}
.hide{display:none!important}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--fg);color:var(--bg);
  padding:9px 16px;border-radius:8px;font-size:14px;z-index:99;opacity:0;transition:.2s}
.toast.on{opacity:1}
.qmap{display:flex;flex-wrap:wrap;gap:6px}
.qmap b{width:32px;height:30px;display:grid;place-items:center;border:1px solid var(--line);
  border-radius:6px;font-weight:500;font-size:12px;cursor:pointer}
.qmap b.done{background:var(--chip)}
.qmap b.mark{border-color:var(--warn);color:var(--warn)}
.qmap b.cur{border-color:var(--pri);box-shadow:0 0 0 2px color-mix(in srgb,var(--pri) 25%,transparent)}
pre.expl{white-space:pre-wrap;background:var(--chip);border-radius:8px;padding:12px;margin:8px 0;font:14px/1.7 inherit}
@media(max-width:640px){ .stem{font-size:15px} main{padding:12px 10px 70px} }
</style></head><body>

<header>
  <span class="brand">SAA-C03</span>
  <button data-nav="home" class="ghost">首页</button>
  <button data-nav="learn" class="ghost">滚动学习</button>
  <button data-nav="exam" class="ghost">模拟考试</button>
  <button data-nav="wrong" class="ghost">错题本</button>
  <button data-nav="stats" class="ghost">学习统计</button>
  <button data-nav="browse" class="ghost">题库</button>
  <span class="sp"></span>
  <span class="seg" id="langSeg">
    <button data-lang="zh">中</button><button data-lang="en">英</button><button data-lang="both">双语</button>
  </span>
  <button id="themeBtn" class="ghost" title="切换主题">◐</button>
  <button data-nav="settings" class="ghost">设置</button>
</header>

<main>
  <section id="v-home"></section>
  <section id="v-learn" class="hide"></section>
  <section id="v-exam" class="hide"></section>
  <section id="v-wrong" class="hide"></section>
  <section id="v-stats" class="hide"></section>
  <section id="v-browse" class="hide"></section>
  <section id="v-settings" class="hide"></section>
</main>
<div class="toast" id="toast"></div>

<script>
"use strict";
const $=(s,r)=>(r||document).querySelector(s), $$=(s,r)=>[...(r||document).querySelectorAll(s)];
const api=async(p,body)=>{
  const r=await fetch(p,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});
  const j=await r.json(); if(j.error) toast(j.error); return j;
};
let TOAST_T; function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");
  clearTimeout(TOAST_T);TOAST_T=setTimeout(()=>t.classList.remove("on"),2200);}
const esc=s=>(s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const DOM_CN={secure:"安全架构",resilient:"弹性架构",performant:"高性能架构",cost:"成本优化"};

let ST={settings:{lang_mode:"both",theme:"system"},info:{},coverage:{}};
let VIEW="home";

/* ---------- 语言 / 主题 ---------- */
function applyLang(){
  $$("#langSeg button").forEach(b=>b.classList.toggle("on",b.dataset.lang===ST.settings.lang_mode));
  document.body.dataset.lang=ST.settings.lang_mode;
  $$(".j-zh").forEach(e=>e.classList.toggle("hide",ST.settings.lang_mode==="en"));
  $$(".j-en").forEach(e=>e.classList.toggle("hide",ST.settings.lang_mode==="zh"));
}
function applyTheme(){
  const t=ST.settings.theme;
  if(t==="system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme",t);
}
$$("#langSeg button").forEach(b=>b.onclick=()=>{ST.settings.lang_mode=b.dataset.lang;applyLang();
  api("/api/settings",{lang_mode:b.dataset.lang});});
$("#themeBtn").onclick=()=>{const o=["system","light","dark"];
  ST.settings.theme=o[(o.indexOf(ST.settings.theme)+1)%3];applyTheme();
  api("/api/settings",{theme:ST.settings.theme});toast("主题："+ST.settings.theme);};

/* ---------- 路由 ---------- */
$$("header [data-nav]").forEach(b=>b.onclick=()=>nav(b.dataset.nav));
function nav(v){
  VIEW=v; $$("main section").forEach(s=>s.classList.add("hide"));
  $("#v-"+v).classList.remove("hide");
  ({home:renderHome,learn:renderLearnHome,exam:renderExamHome,wrong:renderWrong,
    stats:renderStats,browse:renderBrowse,settings:renderSettings}[v])();
}

/* ---------- 题目渲染 ---------- */
function stemHTML(q){
  let h=`<div class="stem"><div class="j-en">${esc(q.stem_en)}</div>`;
  if(q.stem_zh) h+=`<div class="zh j-zh">${esc(q.stem_zh)}</div>`;
  else h+=`<div class="zh j-zh muted small">暂无中文译文</div>`;
  h+=`</div>`;
  return h;
}
function optHTML(o,cls){
  const zh=o.text_zh?`<div class="zh j-zh">${esc(o.text_zh)}</div>`
                    :`<div class="zh j-zh muted small">暂无中文译文</div>`;
  const en=o.text_en?`<div class="j-en">${esc(o.text_en)}</div>`
                    :`<div class="j-en muted small">（该选项在 PDF 中为图片，无文字）</div>`;
  return `<div class="opt ${cls||""}" data-letter="${o.letter}">
    <div class="L">${o.letter}</div><div class="tx">${en}${zh}</div></div>`;
}
function explHTML(en,zh,quality){
  let h="";
  if(quality==="none"&&!en&&!zh) return `<p class="muted small">该题在源文档中没有解析。</p>`;
  if(en) h+=`<div class="j-en"><h3>解析（EN）</h3><pre class="expl">${esc(en)}</pre></div>`;
  if(zh) h+=`<div class="j-zh"><h3>解析（中文）</h3><pre class="expl">${esc(zh)}</pre></div>`;
  else h+=`<div class="j-zh muted small">暂无中文解析</div>`;
  return h;
}

/* ---------- 首页 ---------- */
async function renderHome(){
  const b=await api("/api/bootstrap"); ST.settings=b.settings; ST.info=b.info; ST.coverage=b.coverage;
  applyLang(); applyTheme();
  const i=b.info, c=b.coverage, e=b.exam_countdown;
  let cd="";
  if(e) cd=`<div class="card"><h2>距考试 ${e.days_left} 天</h2>
    <div class="row"><span class="chip">未掌握 ${e.unmastered} 题</span>
    <span class="chip">建议每天 ${e.per_day} 题</span>
    ${e.sprint?'<span class="chip warn">冲刺模式：只出 box≤3 与假掌握</span>':""}
    ${e.final48?'<span class="chip warn">考前 48 小时：以 R4 速览为主</span>':""}</div></div>`;
  $("#v-home").innerHTML=`
  ${cd}
  <div class="card">
    <h2>继续学习</h2>
    <div class="grid">
      <div class="kpi"><b>${i.last_question_id||"—"}</b><span>上次学到第 N 题</span></div>
      <div class="kpi"><b>${i.answered}</b><span>已练题数</span></div>
      <div class="kpi"><b>${i.mastered}</b><span>已掌握</span></div>
      <div class="kpi"><b>${i.wrong}</b><span>错题</span></div>
      <div class="kpi"><b>${i.due_today}</b><span>今日到期</span></div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="pri" onclick="startSession()">继续上次</button>
      <button onclick="nav('exam')">模拟考试</button>
      <button onclick="startSession(true)">只刷错题</button>
    </div>
  </div>
  <div class="card small muted">
    题库 ${i.total} 题，可出题 ${i.pool} 题（${i.total-i.pool} 题因答案存疑已排除）。
    选项中文覆盖率 <b>${c.options_pct}%</b>（${c.options_zh}/${c.options_total}）
    ${c.options_pct<95?'<span class="chip warn">未达 95% 门槛，缺译处显示英文</span>':""}
    ${c.i18n_bad_lines?`<span class="chip warn">i18n_zh.jsonl 有 ${c.i18n_bad_lines} 行无法解析已跳过</span>`:""}
  </div>`;
}

/* ---------- 滚动学习 ---------- */
let S={sid:null,cur:null,picked:new Set(),answered:false,revealed:false};
async function startSession(onlyWrong){
  const r=await api("/api/session/start",{only_wrong:!!onlyWrong});
  if(r.error) return;
  S.sid=r.session_id; nav("learn"); nextQ();
}
async function renderLearnHome(){
  if(!S.sid) $("#v-learn").innerHTML=`<div class="card"><h2>滚动学习</h2>
    <p class="muted">会话按三段式编排：热身复习 → 新题推进 → 收尾重测。</p>
    <div class="row"><button class="pri" onclick="startSession()">开始</button>
    <button onclick="startSession(true)">只刷错题</button></div></div>`;
}
async function nextQ(){
  const r=await api("/api/session/next?sid="+S.sid);
  if(r.error){S.sid=null;return renderLearnHome();}
  if(r.done) return finishSession(r.summary);
  S.cur=r; S.picked=new Set(); S.answered=false; S.revealed=(r.mode==="R4");
  drawQ();
}
const PHASE_CN={warmup:"热身复习",new:"新题推进",cooldown:"收尾重测"};
const MODE_CN={R1:"完整重做",R2:"干扰项狙击",R3:"闪卡自评",R4:"速览确认"};
function drawQ(){
  const r=S.cur,q=r.question;
  const pct=Math.round(r.pos/r.total*100);
  let opts="";
  if(r.mode==="R4"){
    opts=q.options.map(o=>optHTML(o,(q.answer||[]).includes(o.letter)?"ok":"")).join("");
  }else if(r.mode==="R3"&&!S.revealed){
    // 闪卡：翻开前连文本都不渲染，只留占位（不是靠 CSS 遮一下）
    opts=q.options.map(o=>`<div class="opt hide" data-letter="${o.letter}">
      <div class="L">${o.letter}</div><div class="tx">　</div></div>`).join("");
  }else{
    opts=q.options.map(o=>optHTML(o)).join("");
  }
  let ctrl="";
  if(r.mode==="R4"){
    ctrl=`<button class="pri" onclick="nextQ()">下一题 <kbd>Enter</kbd></button>
      <span class="muted small">速览不改变任何复习状态</span>`;
  }else if(r.mode==="R3"&&!S.revealed){
    ctrl=`<button class="pri" onclick="S.revealed=true;drawQ()">翻开</button>
      <span class="muted small">先在心里回想答案</span>`;
  }else if(r.mode==="R3"){
    ctrl=`<span class="muted small">自评：</span>
      <button onclick="selfAssess('remember')">记得</button>
      <button onclick="selfAssess('vague')">模糊</button>
      <button onclick="selfAssess('forgot')">忘了</button>`;
  }else{
    ctrl=`<span class="muted small">信心：</span>
      <button data-conf="sure">有把握 <kbd>J</kbd></button>
      <button data-conf="unsure">不确定 <kbd>K</kbd></button>
      <button data-conf="guess">蒙的 <kbd>L</kbd></button>`;
  }
  $("#v-learn").innerHTML=`
  <div class="card">
    <div class="row small muted">
      <span class="chip">${PHASE_CN[r.phase]} ${r.phase_pos}/${r.phase_total}</span>
      <span class="chip">${MODE_CN[r.mode]}</span>
      <span class="chip">box ${r.box}</span>
      ${q.domain?`<span class="chip">${DOM_CN[q.domain]}</span>`:""}
      <span class="sp" style="flex:1"></span>
      <span>#${q.id} · ${r.pos}/${r.total}</span>
    </div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    ${stemHTML(q)}
    ${q.select_count>1?`<p class="small" style="color:var(--warn)">本题需选 ${q.select_count} 项</p>`:""}
    <div id="opts">${opts}</div>
    <div class="row" id="ctrl" style="margin-top:12px">${ctrl}</div>
    <div id="fb"></div>
  </div>`;
  applyLang();
  if(r.mode!=="R4"&&!(r.mode==="R3"&&!S.revealed)&&r.mode!=="R3"){
    $$("#opts .opt").forEach(el=>el.onclick=()=>{
      if(S.answered) return;
      const L=el.dataset.letter;
      if(q.select_count===1){S.picked=new Set([L]);}
      else{S.picked.has(L)?S.picked.delete(L):S.picked.add(L);}
      $$("#opts .opt").forEach(x=>x.classList.toggle("sel",S.picked.has(x.dataset.letter)));
    });
    $$("#ctrl [data-conf]").forEach(b=>b.onclick=()=>submit(b.dataset.conf));
  }
}
async function submit(conf){
  const q=S.cur.question;
  if(S.answered) return;
  if(S.picked.size!==q.select_count) return toast("请选择 "+q.select_count+" 项");
  S.answered=true;
  const r=await api("/api/answer",{session_id:S.sid,id:q.id,picked:[...S.picked],
    confidence:conf,letter_map:q.letter_map,seed:S.cur.seed});
  $$("#opts .opt").forEach(el=>{
    const L=el.dataset.letter;
    if((r.answer||[]).includes(L)) el.classList.add("ok");
    else if(S.picked.has(L)) el.classList.add("no");
    el.classList.remove("sel");
  });
  const tags=[];
  if(r.false_mastery) tags.push('<span class="chip warn">假掌握：对+蒙的不计入升级</span>');
  if(r.mastered) tags.push('<span class="chip">已掌握</span>');
  if(!r.counts_as_review) tags.push('<span class="chip">收尾重测，不计入 box</span>');
  $("#fb").innerHTML=`<h3>${r.correct?'<span style="color:var(--ok)">✔ 正确</span>':'<span style="color:var(--bad)">✘ 错误</span>'}
    　正确答案 <b>${(r.answer||[]).join(" ")}</b>　<span class="chip">box ${r.box}</span>${tags.join("")}</h3>
    ${explHTML(r.explanation_en,r.explanation_zh,r.explanation_quality)}`;
  $("#ctrl").innerHTML=`<button class="pri" onclick="nextQ()">下一题 <kbd>Enter</kbd></button>`;
  applyLang();
}
async function selfAssess(g){
  if(S.answered) return; S.answered=true;
  await api("/api/selfassess",{session_id:S.sid,id:S.cur.question.id,grade:g});
  nextQ();
}
function finishSession(sum){
  S.sid=null;
  $("#v-learn").innerHTML=`<div class="card"><h2>本次会话结束</h2>
    <div class="grid">
      <div class="kpi"><b>${sum.answered}</b><span>作答</span></div>
      <div class="kpi"><b>${sum.correct}</b><span>答对</span></div>
      <div class="kpi"><b>${sum.counts.warmup}</b><span>热身</span></div>
      <div class="kpi"><b>${sum.counts.new}</b><span>新题</span></div>
      <div class="kpi"><b>${sum.counts.cooldown}</b><span>收尾重测</span></div>
    </div>
    <p class="muted small">错题：${sum.wrong_ids.join(", ")||"无"}</p>
    <div class="row"><button class="pri" onclick="startSession()">再来一轮</button>
      <button onclick="nav('stats')">看统计</button></div></div>`;
}

/* ---------- 模拟考试 ---------- */
let E={id:null,qs:[],ans:{},marks:new Set(),i:0,t0:0,timer:null,left:0};
function renderExamHome(){
  if(E.id) return drawExam();
  $("#v-exam").innerHTML=`<div class="card"><h2>模拟考试</h2>
    <p class="muted small">65 题 · ${ST.settings.timer_enabled?"130 分钟倒计时":"不计时"} · ≥720 分通过 ·
      抽样自动排除答案存疑的题</p>
    <div class="row"><label class="small muted">随机种子（可选，便于复现）</label>
      <input type="number" id="seed" placeholder="留空则随机" style="width:150px"></div>
    <div class="row" style="margin-top:10px"><button class="pri" onclick="startExam()">开始考试</button></div>
  </div>`;
}
async function startExam(){
  const s=$("#seed").value;
  const r=await api("/api/exam/start",{seed:s?Number(s):null});
  if(r.error) return;
  E={id:r.exam_id,qs:r.questions,ans:{},marks:new Set(),i:0,t0:Date.now(),
     timer:null,left:r.minutes*60};
  if(ST.settings.timer_enabled){
    E.timer=setInterval(()=>{E.left--;const el=$("#clock");
      if(el)el.textContent=fmt(E.left);
      if(E.left<=0){clearInterval(E.timer);toast("时间到，自动交卷");submitExam();}},1000);
  }
  drawExam();
}
const fmt=s=>`${String(Math.floor(s/3600)).padStart(2,"0")}:${String(Math.floor(s%3600/60)).padStart(2,"0")}:${String(s%60).padStart(2,"0")}`;
function drawExam(){
  const q=E.qs[E.i], a=E.ans[q.id]||{picked:[]};
  $("#v-exam").innerHTML=`<div class="card">
    <div class="row small muted">
      <span class="chip">第 ${E.i+1} / ${E.qs.length} 题</span>
      <span class="chip">#${q.id}</span>
      ${q.domain?`<span class="chip">${DOM_CN[q.domain]}</span>`:""}
      <span style="flex:1"></span>
      ${ST.settings.timer_enabled?`<span class="chip" id="clock">${fmt(E.left)}</span>`:""}
      <button onclick="toggleMark()">${E.marks.has(q.id)?"★ 已标记":"☆ 标记"} <kbd>M</kbd></button>
    </div>
    ${stemHTML(q)}
    ${q.select_count>1?`<p class="small" style="color:var(--warn)">本题需选 ${q.select_count} 项</p>`:""}
    <div id="opts">${q.options.map(o=>optHTML(o,a.picked.includes(o.letter)?"sel":"")).join("")}</div>
    <div class="row" style="margin-top:12px">
      <button onclick="goExam(-1)" ${E.i===0?"disabled":""}>← 上一题</button>
      <button onclick="goExam(1)" ${E.i===E.qs.length-1?"disabled":""}>下一题 →</button>
      <span style="flex:1"></span>
      <button class="pri" onclick="confirmSubmit()">交卷</button>
    </div></div>
    <div class="card"><h3>题号总览（已答 / 未答 / 已标记）</h3>
      <div class="qmap">${E.qs.map((x,k)=>{
        const done=(E.ans[x.id]||{picked:[]}).picked.length>0;
        return `<b class="${done?"done":""} ${E.marks.has(x.id)?"mark":""} ${k===E.i?"cur":""}"
          onclick="E.i=${k};drawExam()">${k+1}</b>`;}).join("")}</div></div>`;
  applyLang();
  $$("#opts .opt").forEach(el=>el.onclick=()=>{
    const L=el.dataset.letter, cur=new Set((E.ans[q.id]||{picked:[]}).picked);
    if(q.select_count===1) cur.clear()||cur.add(L);
    else cur.has(L)?cur.delete(L):cur.add(L);
    E.ans[q.id]={picked:[...cur],letter_map:q.letter_map};
    $$("#opts .opt").forEach(x=>x.classList.toggle("sel",cur.has(x.dataset.letter)));
    $$(".qmap b")[E.i].classList.toggle("done",cur.size>0);
  });
}
function goExam(d){E.i=Math.max(0,Math.min(E.qs.length-1,E.i+d));drawExam();}
function toggleMark(){const id=E.qs[E.i].id;E.marks.has(id)?E.marks.delete(id):E.marks.add(id);drawExam();}
function confirmSubmit(){
  const un=E.qs.filter(q=>!(E.ans[q.id]||{picked:[]}).picked.length).length;
  if(un&&!confirm(`还有 ${un} 题未作答，确认交卷？`)) return;
  submitExam();
}
async function submitExam(){
  if(E.timer) clearInterval(E.timer);
  const r=await api("/api/exam/submit",{exam_id:E.id,answers:E.ans,
    elapsed_sec:Math.round((Date.now()-E.t0)/1000)});
  if(r.error) return;
  const rec=r.record; E.id=null;
  const doms=Object.entries(rec.by_domain).map(([k,v])=>
    `<tr><td>${DOM_CN[k]||k}</td><td>${v.c}/${v.n}</td><td>${Math.round(100*v.c/v.n)}%</td></tr>`).join("");
  const rev=r.review.map(x=>{
    const q=x.question;
    return `<div class="card"><div class="row small muted">
      <span class="chip">#${x.id}</span>
      <span class="chip" style="color:${x.correct?"var(--ok)":"var(--bad)"}">${x.correct?"正确":"错误"}</span>
      <span class="chip">正确答案 ${q.answer.join(" ")}</span></div>
      ${stemHTML(q)}
      <div>${q.options.map(o=>optHTML(o,q.answer.includes(o.letter)?"ok":"")).join("")}</div>
      ${explHTML(q.explanation_en,q.explanation_zh,q.explanation_quality)}</div>`;}).join("");
  $("#v-exam").innerHTML=`<div class="card">
    <h2>${rec.passed?'<span style="color:var(--ok)">通过</span>':'<span style="color:var(--bad)">未通过</span>'}
      　${rec.score} 分 <span class="muted small">/ 及格线 ${720}</span></h2>
    <div class="grid">
      <div class="kpi"><b>${rec.correct}/${rec.size}</b><span>答对</span></div>
      <div class="kpi"><b>${Math.round(100*rec.correct/rec.size)}%</b><span>正确率</span></div>
      <div class="kpi"><b>${fmt(rec.elapsed_sec)}</b><span>用时</span></div>
      <div class="kpi"><b>${rec.scoring_mode}</b><span>计分方式</span></div>
    </div>
    <h3>按考纲领域</h3><table><tr><th>领域</th><th>得分</th><th>正确率</th></tr>${doms}</table>
    <p class="muted small">记录已保存到 data/exams/${r.file}；错题已进错题本。</p>
    <div class="row"><button class="pri" onclick="renderExamHome()">再考一次</button></div>
  </div><h3>逐题回顾</h3>${rev}`;
  applyLang();
}

/* ---------- 错题本 ---------- */
async function renderWrong(){
  const r=await api("/api/wrongbook");
  $("#v-wrong").innerHTML=`<div class="card"><h2>错题本 <span class="muted small">${r.rows.length} 题</span></h2>
    <div class="row"><button class="pri" onclick="startSession(true)">只刷错题</button></div></div>`+
    (r.rows.length?r.rows.map(x=>`<div class="card">
      <div class="row small muted"><span class="chip">#${x.id}</span><span class="chip">box ${x.box}</span>
      <span class="chip">错 ${x.wrong} / 对 ${x.correct}</span>
      ${x.false_mastery?'<span class="chip warn">假掌握</span>':""}
      ${x.domain?`<span class="chip">${DOM_CN[x.domain]}</span>`:""}
      <span style="flex:1"></span>
      <button onclick="rmWrong(${x.id})">移出</button></div>
      <div class="j-en small">${esc(x.stem_en)}…</div>
      <div class="j-zh small muted">${esc(x.stem_zh)||"暂无中文译文"}…</div></div>`).join("")
     :`<div class="card muted">还没有错题。</div>`);
  applyLang();
}
async function rmWrong(id){await api("/api/wrongbook/remove",{id});renderWrong();}

/* ---------- 统计 ---------- */
async function renderStats(){
  const r=await api("/api/stats");
  const tot=Object.values(r.boxes).reduce((a,b)=>a+b,0)+r.mastered+r.untouched||1;
  const seg=(n,c,l)=>n?`<i style="width:${100*n/tot}%;background:${c}" title="${l} ${n}"></i>`:"";
  const mx=Math.max(1,...r.forecast.map(f=>f.count));
  $("#v-stats").innerHTML=`
  <div class="card"><h2>各 box 分布</h2>
    <div class="stack">
      ${seg(r.untouched,"var(--chip)","未接触")}
      ${[1,2,3,4,5].map((b,k)=>seg(r.boxes[b],`color-mix(in srgb,var(--pri) ${20+k*18}%,var(--card))`,"box"+b)).join("")}
      ${seg(r.mastered,"var(--ok)","已掌握")}
    </div>
    <div class="row small muted" style="margin-top:8px">
      <span class="chip">未接触 ${r.untouched}</span>
      ${[1,2,3,4,5].map(b=>`<span class="chip">box${b} ${r.boxes[b]}</span>`).join("")}
      <span class="chip">已掌握 ${r.mastered}</span></div></div>
  <div class="card"><h2>未来 7 天到期预测</h2>
    <div class="spark">${r.forecast.map(f=>
      `<div style="height:${Math.round(100*f.count/mx)}%"><span>${f.count}</span></div>`).join("")}</div>
    <div class="row small muted">${r.forecast.map(f=>
      `<span style="flex:1;text-align:center">${f.date.slice(5)}</span>`).join("")}</div></div>
  <div class="card"><h2>假掌握清单 <span class="muted small">（对 + 蒙的，考场最易翻车）</span></h2>
    ${r.false_mastery.length?`<table><tr><th>题号</th><th>box</th><th>题干</th></tr>
      ${r.false_mastery.map(x=>`<tr><td>#${x.id}</td><td>${x.box}</td><td>${esc(x.stem)}…</td></tr>`).join("")}
      </table>`:`<p class="muted">暂无。</p>`}</div>
  <div class="card"><h2>各领域正确率 <span class="muted small">（最弱在最上）</span></h2>
    ${r.domains.length?`<table><tr><th>领域</th><th>作答</th><th>正确</th><th>正确率</th></tr>
      ${r.domains.map(d=>`<tr><td>${DOM_CN[d.domain]||d.domain}</td><td>${d.attempts}</td>
        <td>${d.correct}</td><td><b>${d.pct}%</b></td></tr>`).join("")}</table>`
      :`<p class="muted">还没有足够数据。</p>`}</div>
  <div class="card small muted">译文覆盖：题干 ${r.coverage.stem_zh}/${r.coverage.total}，
    选项 ${r.coverage.options_zh}/${r.coverage.options_total}（${r.coverage.options_pct}%）</div>`;
}

/* ---------- 题库浏览 ---------- */
let BP=1,BR=0;
async function renderBrowse(page){
  BP=page||BP;
  const r=await api(`/api/browse?page=${BP}&review=${BR}`);
  $("#v-browse").innerHTML=`<div class="card"><h2>题库 <span class="muted small">${r.total} 题</span></h2>
    <div class="row"><button class="${BR?"":"pri"}" onclick="BR=0;renderBrowse(1)">全部</button>
    <button class="${BR?"pri":""}" onclick="BR=1;renderBrowse(1)">仅看待核对</button>
    <span style="flex:1"></span>
    <button onclick="renderBrowse(${Math.max(1,BP-1)})" ${BP<=1?"disabled":""}>上一页</button>
    <span class="small muted">${r.page}/${r.pages}</span>
    <button onclick="renderBrowse(${BP+1})" ${BP>=r.pages?"disabled":""}>下一页</button></div></div>`+
    r.rows.map(x=>`<div class="card">
      <div class="row small muted"><span class="chip">#${x.id}</span>
        <span class="chip">${x.type==="multi"?"多选":"单选"}</span>
        <span class="chip">答案 ${(x.answer||[]).join(" ")||"—"}</span>
        ${x.domain?`<span class="chip">${DOM_CN[x.domain]}</span>`:""}
        ${x.needs_review?`<span class="chip warn">待核对：${esc(x.review_reason)}</span>`:""}</div>
      <div class="j-en small">${esc(x.stem_en)}…</div>
      <div class="j-zh small muted">${esc(x.stem_zh)||"暂无中文译文"}…</div></div>`).join("");
  applyLang();
}

/* ---------- 设置 ---------- */
async function renderSettings(){
  const s=ST.settings;
  $("#v-settings").innerHTML=`<div class="card"><h2>设置</h2>
  <table>
   <tr><td>考试日期</td><td><input type="date" id="s_exam" value="${s.exam_date||""}">
     <span class="muted small">设定后自动压缩间隔、显示倒排计划</span></td></tr>
   <tr><td>每次会话题量</td><td><input type="number" id="s_size" min="5" max="100" value="${s.session_size}"></td></tr>
   <tr><td>出题顺序</td><td><select id="s_order">
     ${["sequential","random","review_first"].map(o=>`<option value="${o}" ${s.order===o?"selected":""}>${
       {sequential:"按题号顺序",random:"随机",review_first:"错题与到期优先"}[o]}</option>`).join("")}</select></td></tr>
   <tr><td>考试计时</td><td><select id="s_timer">
     <option value="1" ${s.timer_enabled?"selected":""}>开启（130 分钟）</option>
     <option value="0" ${!s.timer_enabled?"selected":""}>关闭</option></select></td></tr>
   <tr><td>计分方式</td><td><select id="s_score">
     <option value="linear" ${s.scoring_mode==="linear"?"selected":""}>linear：1000×正确率</option>
     <option value="aws_scaled" ${s.scoring_mode==="aws_scaled"?"selected":""}>aws_scaled：100+900×正确率</option>
     </select></td></tr>
  </table>
  <div class="row" style="margin-top:12px"><button class="pri" onclick="saveSettings()">保存</button></div>
  </div>
  <div class="card small muted">所有状态写在 <code>data/</code>：progress.json（进度+SRS）、wrong_book.json、
   settings.json、exams/。每答一题立即原子写盘。</div>`;
}
async function saveSettings(){
  const r=await api("/api/settings",{
    exam_date:$("#s_exam").value||null,
    session_size:Number($("#s_size").value)||25,
    order:$("#s_order").value,
    timer_enabled:$("#s_timer").value==="1",
    scoring_mode:$("#s_score").value});
  ST.settings=r.settings; toast("已保存"); renderHome(); nav("home");
}

/* ---------- 键盘 ---------- */
document.addEventListener("keydown",e=>{
  if(/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
  const k=e.key.toUpperCase();
  if(k==="T"){const o=["zh","en","both"];ST.settings.lang_mode=o[(o.indexOf(ST.settings.lang_mode)+1)%3];
    applyLang();api("/api/settings",{lang_mode:ST.settings.lang_mode});return;}
  if(VIEW==="learn"&&S.cur){
    const q=S.cur.question;
    if("123456".includes(k)){
      const el=$$("#opts .opt")[Number(k)-1]; if(el) el.click(); return;
    }
    if(k==="J") return $("#ctrl [data-conf=sure]")?.click();
    if(k==="K") return $("#ctrl [data-conf=unsure]")?.click();
    if(k==="L") return $("#ctrl [data-conf=guess]")?.click();
    if(e.key==="Enter"){const b=$("#ctrl button.pri"); if(b) b.click(); return;}
  }
  if(VIEW==="exam"&&E.id){
    if("123456".includes(k)){const el=$$("#opts .opt")[Number(k)-1];if(el)el.click();return;}
    if(k==="M") return toggleMark();
    if(e.key==="ArrowLeft") return goExam(-1);
    if(e.key==="ArrowRight"||e.key==="Enter") return goExam(1);
  }
});

/* ---------- 启动：支持 ?go=learn 直接进入学习状态 ---------- */
async function boot(){
  await renderHome();                       // 先 bootstrap，拿 settings / 主题 / 语言
  const go=new URLSearchParams(location.search).get("go")||"home";
  $$("main section").forEach(s=>s.classList.add("hide"));
  if(go==="learn"){ VIEW="learn"; $("#v-learn").classList.remove("hide"); return startSession(); }
  if(go!=="home"&&$("#v-"+go)) return nav(go);
  VIEW="home"; $("#v-home").classList.remove("hide");
}
boot();
</script></body></html>
"""


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
