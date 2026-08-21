#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_bank.py — AWS SAA-C03 题库构建脚本（两阶段）

阶段一  python3 scripts/build_bank.py --extract
        → data/questions_en.json   纯英文题库（PDF 抽取，权威）
        → data/i18n_todo.jsonl     待译清单

阶段二  python3 scripts/build_bank.py
        → data/questions.json      合并 EN + 解析译文 + 选项译文 + manual_fixes
        → data/build_report.md     数据质量报告

仅使用标准库。不联网。不修改任何源文件。
"""

import argparse
import difflib
import json
import os
import re
import sys
import zlib
from collections import Counter, defaultdict

# 脚本在 scripts/ 下，仓库根目录要再往上退一层。
# 别"简化"成 dirname(__file__) —— 那样 data/ 会解析到 scripts/data/，
# 题库读不到、进度写错地方，而且不报错。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

PDF_FILE = os.path.join(ROOT, "AWS Certified Solutions Architect Associate SAA-C03.pdf")
SOL_EN = os.path.join(ROOT, "AWS SAA-03 Solution.txt")
SOL_ZH = os.path.join(ROOT, "AWS SAA-03 Solution.zh-CN.txt")

QUESTIONS_EN = os.path.join(DATA, "questions_en.json")
QUESTIONS = os.path.join(DATA, "questions.json")
I18N_TODO = os.path.join(DATA, "i18n_todo.jsonl")
I18N_ZH = os.path.join(DATA, "i18n_zh.jsonl")
MANUAL_FIXES = os.path.join(DATA, "manual_fixes.json")
BUILD_REPORT = os.path.join(DATA, "build_report.md")

TOTAL_Q = 684
# txt 中根本不存在的题号（源文件从 190] 直接跳到 201]）
MISSING_IN_TXT = set(range(191, 201))
# txt 中第 315 题被误编号为 215]
TYPO_RENUMBER = {"dup_of": 215, "real": 315}


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def atomic_write(path, text):
    """先写 .tmp 再 os.replace，避免半截文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def norm_ws(s):
    return re.sub(r"[ \t]+", " ", s).strip()


def flat(s):
    """折成单行并压缩所有空白，用于相似度比对。"""
    return re.sub(r"\s+", " ", s).strip()


def stem_similarity(stem_en, seg):
    """PDF 题干 vs 解析文档开头的相似度。<0.9 视为「转述失真」，需按 PDF 补译。

    autojunk 必须关掉。difflib 默认对长度 ≥200 的序列启用启发式：出现频率超过 1%
    的元素被当「垃圾」踢出索引。逐字符比对时空格、e、t 这些全中招，匹配块无法成锚，
    分数直接崩掉。实测 680 题只差 6 处 fi 连字，autojunk=True 算出 0.145，
    关掉后是 0.970。开着的时候 <0.9 的题从 31 虚增到 42，多出来的 11 题
    （164/301/653/655/657/658/668/671/674/676/680）全是假警报。
    """
    if not seg:
        return 0.0
    head = flat(seg)[: len(stem_en)]
    return difflib.SequenceMatcher(
        None, flat(stem_en).lower(), head.lower(), autojunk=False).ratio()


# --------------------------------------------------------------------------
# 阶段一：PDF 抽取
# --------------------------------------------------------------------------

# 该 PDF 用 Type3 子集字体 + 十六进制单字节字形码，几乎没有 /ToUnicode 表。
# 字形编码是统一 +0x1C 偏移到 ASCII，偏移后剩 6 个码位需要固定表替换。
LIGATURES = {
    "È": "fi",  # 0xAC
    "Ê": "ffi",  # 0xAE
    "É": "fl",  # 0xAD
    "Ë": "ffl",  # 0xAF
    "º": "’",  # 0x9E  右单引号
    "¿": "•",  # 0xA3  项目符号
    # 下面 4 个是后来补的：出现次数少（全库 6 处 / 4 道题），一开始没被发现，
    # 于是 211 的题干读作 `¼application½`、271 读作 `the ¹same`、
    # 516 少了一个字母。逐个拿 Solution.txt 里的同一句话对照确认过。
    "¼": "“",  # 0xA0  左双引号
    "½": "”",  # 0xA1  右双引号
    "¹": "‘",  # 0x9D  左单引号
    "\x87": "Е",  # 0x6B  西里尔大写 IE（U+0415）—— 源文档本身就是同形字 typo，
    #                     txt 里写的就是它，抽取只负责如实还原，不做"纠正"
}

_RE_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_RE_BT = re.compile(rb"BT(.*?)ET", re.S)
_RE_HEXTJ = re.compile(rb"<([0-9A-Fa-f]{2})>\s*Tj")


def _decode_seg(seg):
    text = "".join(chr(int(h, 16) + 0x1C) for h in _RE_HEXTJ.findall(seg))
    for k, v in LIGATURES.items():
        text = text.replace(k, v)
    return text


def pdf_pages(path):
    """返回按文档顺序排列的页文本列表（只保留含文字的内容流）。"""
    with open(path, "rb") as f:
        raw = f.read()
    pages = []
    for chunk in _RE_STREAM.findall(raw):
        try:
            d = zlib.decompress(chunk)
        except Exception:
            continue
        if b"BT" not in d or b"Tj" not in d:
            continue
        lines = [_decode_seg(m.group(1)) for m in _RE_BT.finditer(d)]
        page = "\n".join(l for l in lines if l.strip())
        if page.strip():
            pages.append(page)
    return pages


_RE_OPT = re.compile(r"(?m)^([A-F])\.[ \t]*$")
_RE_CHOOSE = re.compile(r"\(Choose\s+(two|three)\.?\)", re.I)
_WORDNUM = {"two": 2, "three": 3}


def parse_pdf(path):
    """抽取 684 题的题干与选项。返回 {id: record}。"""
    pages = pdf_pages(path)
    log("  PDF: 抽出 %d 个页面流" % len(pages))

    # `Topic N` 行出现在 `Question #N` 之前，属于紧随其后的那道题
    page_of, topic_of = {}, {}
    blocks = []  # (qid, body)
    buf_id, buf_lines, pending_topic = None, [], None
    for pno, page in enumerate(pages, start=1):
        for line in page.split("\n"):
            s = line.strip()
            m = re.match(r"^Question #(\d+)$", s)
            if m:
                if buf_id is not None:
                    blocks.append((buf_id, "\n".join(buf_lines)))
                buf_id = int(m.group(1))
                buf_lines = []
                page_of[buf_id] = pno
                if pending_topic:
                    topic_of[buf_id] = pending_topic
                    pending_topic = None
                continue
            mt = re.match(r"^Topic (\d+)$", s)
            if mt:
                pending_topic = "Topic " + mt.group(1)
                continue
            if s == "Topic 1 - Exam A":  # 页眉
                continue
            buf_lines.append(line)
    if buf_id is not None:
        blocks.append((buf_id, "\n".join(buf_lines)))

    out = {}
    for qid, body in blocks:
        # 题干与选项之间以独占一行的 "A." / "B." … 分隔
        marks = list(_RE_OPT.finditer(body))
        stem_raw = body[: marks[0].start()] if marks else body
        stem = norm_ws(" ".join(l.strip() for l in stem_raw.strip().split("\n") if l.strip()))

        options = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
            txt = body[m.end():end]
            txt = norm_ws(" ".join(l.strip() for l in txt.strip().split("\n") if l.strip()))
            # 少数题的选项是图片（如 477 的 IAM 策略截图），PDF 里没有文字。
            # 保留占位，由阶段二标 needs_review，不要静默丢弃导致选项数错乱。
            options.append({"letter": m.group(1), "text_en": txt, "text_zh": None})

        mc = _RE_CHOOSE.search(stem)
        sel = _WORDNUM[mc.group(1).lower()] if mc else 1

        out[qid] = {
            "id": qid,
            "topic": topic_of.get(qid),
            "type": "multi" if sel > 1 else "single",
            "select_count": sel,
            "stem_en": stem,
            "options": options,
            "pdf_page": page_of.get(qid),
        }
    return out


_RE_TOPIC_LINE = re.compile(r"(?m)^Topic \d+\s*$\n?")

# --------------------------------------------------------------------------
# 解析文档（英文 / 中文）切分
# --------------------------------------------------------------------------

# §2.2 实测：这条正则命中 658/684，不要"优化"（加前缀兼容会掉到 509）

# 题号位数跟着 TOTAL_Q 走。写死 \d{1,3} 的话，题库一旦超过 999 题，主通道对
# 1000 以后的题号全线失效 —— 而 `^` 禁止中途重试，修复通道 A/B 也救不回连片缺失，
# 脚本还不报错。当前 TOTAL_Q=684，算出来仍是 {1,3}，行为与原先完全一致。
_NUM = r"\d{1,%d}" % len(str(TOTAL_Q))

_RE_SEG_EN = re.compile(r"(?m)^\s*(" + _NUM + r")\s*[\].)]\s*")
# 中文译文里题号后统一带空格，且重点标记已归一为「重点>>>」
_RE_SEG_ZH = re.compile(r"(?m)^[^\S\n]*(?:重点\s*>*)?\s*(" + _NUM + r")\s*[\].]\s")


def split_solution(text, regex):
    """按题号切分解析文档，三段式：

    1. 主通道 —— 行首题号，且**必须单调递增**。单调性这一条很关键：解析正文里
       常有 `1. Real-time Data Stream:` 这种编号列表，不加单调过滤会被误当成题号，
       把后续几十题的内容整体错位。
    2. 修复通道 A —— 主通道漏掉的题号，放宽到允许 `IMP>>>>>>98.` 这类行内前缀
       （只对缺失题号做定点搜索，不动主通道，避免整体命中率下降）。
    3. 修复通道 B —— 位置推断，专治源文件把第 315 题误编号为 `215]`：
       该匹配位置落在 314 与 316 之间，据此还原。
    """
    anchors = {}  # qid -> (start, content_start)

    # 1. 主通道
    prev = 0
    for m in regex.finditer(text):
        n = int(m.group(1))
        if 1 <= n <= TOTAL_Q and n > prev:
            anchors[n] = (m.start(), m.end())
            prev = n

    # 2. 修复通道 A：宽松前缀定点搜索
    for n in [x for x in range(1, TOTAL_Q + 1) if x not in anchors]:
        pat = r"(?m)^[^\n\d]{0,24}(?<![\d.])%d\s*[\].)]" % n
        hits = list(re.finditer(pat, text))
        if len(hits) == 1:
            anchors[n] = (hits[0].start(), hits[0].end())

    # 3. 修复通道 B：位置推断（315 误编号为 215）
    for n in [x for x in range(2, TOTAL_Q) if x not in anchors]:
        lo = anchors.get(n - 1)
        hi = anchors.get(n + 1)
        if not lo or not hi:
            continue
        taken = {v[0] for v in anchors.values()}
        cands = [m for m in regex.finditer(text)
                 if lo[0] < m.start() < hi[0] and m.start() not in taken]
        if len(cands) > 1 and n == TYPO_RENUMBER["real"]:
            # 候选不唯一时，用已知的误编号收窄。TYPO_RENUMBER 以前是个纯注释常量
            # （全仓零引用），315 全靠"恰好只有一个候选"这个零余量的巧合救回来 ——
            # 只要 314 与 316 之间的解析正文里多一行编号列表（源文件里到处都是），
            # 315 就会被静默丢弃。现在把那条已知事实真正用上。
            narrowed = [m for m in cands if int(m.group(1)) == TYPO_RENUMBER["dup_of"]]
            if narrowed:
                cands = narrowed
        if len(cands) == 1:
            anchors[n] = (cands[0].start(), cands[0].end())

    # 依位置切片
    ordered = sorted(anchors.items(), key=lambda kv: kv[1][0])
    segs = {}
    for i, (n, (start, cstart)) in enumerate(ordered):
        end = ordered[i + 1][1][0] if i + 1 < len(ordered) else len(text)
        segs[n] = text[cstart:end]
    return segs


# --------------------------------------------------------------------------
# 答案判定
# --------------------------------------------------------------------------

# 「答案行」：含 ans- / Answer(s): / Correct answer 标记的那一整行
_RE_ANS_MARK_LINE = re.compile(
    r"(?im)^.*?(?:\bans\s*-|\bAnswers?\s*:|\bCorrect answers?\b).*$")
# 答案行里的字母 token：`B.` / `C)` / `(A)`
_RE_LETTER_TOKEN = re.compile(r"(?i)(?:^|[\s(+,])([A-F])\s*[\).]")
_RE_LINE_LETTER = re.compile(r"(?m)^\s*([A-F])[\).]\s+\S")


def _answer_text_candidates(seg):
    """从解析段落里挑出「疑似答案句」，用于相似度兜底。"""
    cands = []
    m = re.search(r"(?im)^\s*ans\s*-\s*(.+)$", seg)
    if m:
        cands.append(m.group(1))
    for m in re.finditer(r"(?im)^\s*(?:Correct answer|Answers?)\s*[:\-]?\s*(.+)$", seg):
        cands.append(m.group(1))
    for m in _RE_LINE_LETTER.finditer(seg):
        # 行尾必须从正文起点找，不能从 m.start() 找：_RE_LINE_LETTER 的 `^\s*` 会
        # 跨空行，选项前有空行时（源文件绝大多数是这种排版）m.start() 落在上一个
        # 换行符上，seg.find("\n", m.start()) 返回的是**匹配内部**那个换行符，
        # 切出来是空串，再被下面的 len>12 静默滤掉。实测 721 处匹配里 539 处这样丢了。
        line_end = seg.find("\n", m.end())
        cands.append(seg[m.end() - 1: line_end if line_end > 0 else len(seg)])
    # 兜底：段首若干行
    head = [l for l in seg.strip().split("\n") if l.strip()][:6]
    cands.extend(head)
    return [c.strip() for c in cands if c and len(c.strip()) > 12]


def resolve_answer(seg, options, select_count):
    """返回 (letters, source, confidence)。"""
    valid = {o["letter"] for o in options}
    if seg is None:
        return [], "unresolved", 0.0

    # a. 先定位「答案行」，再只在这一行里抽字母。
    #    不能在整段里乱抓：解析正文常写「Option D 不可扩展」，会污染结果；
    #    也不能只看开头 N 个字符——答案行往往在长题干之后（如 655 题）。
    explicit = []
    ans_line = None
    for m in _RE_ANS_MARK_LINE.finditer(seg):
        ans_line = m.group(0)
        break
    if ans_line:
        for m in _RE_LETTER_TOKEN.finditer(ans_line):
            L = m.group(1).upper()
            if L in valid and L not in explicit:
                explicit.append(L)
    if explicit and len(explicit) == select_count:
        return sorted(explicit), "regex_letter", 1.0

    # 行首字母（`B. 选项原文` 是最常见的格式）
    line_letters = []
    for m in _RE_LINE_LETTER.finditer(seg):
        L = m.group(1).upper()
        if L in valid and L not in line_letters:
            line_letters.append(L)
    if len(line_letters) == select_count:
        return sorted(line_letters), "regex_letter", 0.95

    # c. 相似度兜底
    best = {}
    for cand in _answer_text_candidates(seg):
        c = cand.lower()
        for o in options:
            r = difflib.SequenceMatcher(None, c, o["text_en"].lower()).ratio()
            if r > best.get(o["letter"], 0.0):
                best[o["letter"]] = r
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    picked = [L for L, r in ranked[:select_count] if r >= 0.65]
    if len(picked) == select_count:
        # 并列不采纳。源文件里有整段只重印题干 + 全部选项、压根没写答案的题
        # （151/158/159/160/178/179/184/232 实测如此）。这种段落里每个选项与
        # 自己的相似度都是 1.0，ranked 里一堆并列，ranked[:select_count] 取到
        # 的纯粹是排序稳定性的产物 —— 却会带着 confidence 1.0、needs_review
        # false 进出题池，用户选对反被判错。
        #
        # 判据：与被取中的最低分并列的选项数超过 select_count，就说明相似度
        # 区分不出来，判未确定。真有答案的题区分度很干净（6 题 0.993 vs 次高
        # 0.302，232 题 0.992 vs 0.989），不会被这条误伤。
        cutoff = ranked[select_count - 1][1]
        if sum(1 for _, r in ranked if r >= cutoff - 1e-9) <= select_count:
            return sorted(picked), "fuzzy_match", round(cutoff, 3)

    # d. 显式字母数量不符也先留着（标低置信），否则未确定
    if explicit and select_count == 1:
        return sorted(explicit[:1]), "regex_letter", 0.6
    return [], "unresolved", 0.0


# --------------------------------------------------------------------------
# 领域启发式打标
# --------------------------------------------------------------------------

DOMAIN_KEYWORDS = {
    "secure": ["iam", "kms", "waf", "shield", "guardduty", "secrets manager", "cognito",
               "encrypt", "macie", "security group", "acl", "certificate", "tls", "ssl",
               "service control policy", "scp", "cloudtrail", "inspector", "least privilege",
               "root user", "mfa", "object lock", "principalorgid", "sts", "assume role"],
    "resilient": ["multi-az", "auto scaling", "failover", "sqs", "backup", "disaster recovery",
                  " dr ", "rpo", "rto", "route 53", "highly available", "high availability",
                  "resilien", "replica", "snapshot", "fault toler", "decoupl", "sns", "standby",
                  "availability zone"],
    "performant": ["cloudfront", "elasticache", "global accelerator", "read replica", "athena",
                   "latency", "throughput", "cache", "caching", "dax", "fsx for lustre",
                   "performance", "iops", "kinesis", "redshift", "placement group", "edge"],
    "cost": ["spot", "savings plan", "reserved instance", "lifecycle", "glacier",
             "intelligent-tiering", "cost-effective", "cost effective", "minimize cost",
             "reduce cost", "cheapest", "budget", "cost explorer", "lowest cost",
             "infrequent access", "on-demand instance"],
}


def guess_domain(stem, options):
    blob = (stem + " " + " ".join(o["text_en"] for o in options)).lower()
    score = {}
    for dom, kws in DOMAIN_KEYWORDS.items():
        score[dom] = sum(blob.count(k) for k in kws)
    best = max(score.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return None
    # 第二名过于接近则判为无把握
    ordered = sorted(score.values(), reverse=True)
    if len(ordered) > 1 and ordered[0] - ordered[1] < 2:
        return None
    return best[0]


# --------------------------------------------------------------------------
# 译文加载
# --------------------------------------------------------------------------

def load_i18n_jsonl(path):
    """返回 (options_map, stem_map, bad_lines)。同一 key 以最后一行为准。"""
    opts, stems, bad = {}, {}, 0
    if not os.path.exists(path):
        return opts, stems, bad
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                rec = json.loads(line)
                qid = int(rec["id"])
                zh = rec.get("zh")
                if not zh:
                    raise ValueError("empty zh")
                if rec.get("field") == "stem":
                    stems[qid] = zh
                elif rec.get("letter"):
                    opts[(qid, str(rec["letter"]).upper())] = zh
                else:
                    raise ValueError("missing letter/field")
            except Exception:
                bad += 1
    return opts, stems, bad


# 解析段落的结构是「题干 → 答案行 → 解析」。这些正则用来定位答案行。
_RE_ANS_LINE_EN = re.compile(
    r"^\s*(?:ans\s*-|Answers?\s*:|Correct answer\b|[A-F][\).]\s+\S|\(?[A-F]\)\s)", re.I)
_RE_ANS_LINE_ZH = re.compile(
    r"^\s*(?:答案|正确答案|参考答案|[A-F][\.、)]\s*\S)")
_RE_DIVIDER = re.compile(r"(?m)^[-=]{5,}\s*$")


def split_segment(seg, stem_en=None, zh=False):
    """把解析段落切成 (题干部分, 解析部分)。

    解析文档的段落结构是「题干 → 答案 → 解析」。题干与 PDF 题干几乎逐字相同，
    直接当解析展示会和题面重复，必须剥掉。分两步：

    1. 按题干文本逐行剥离开头（有些题的答案行没有字母前缀，光找答案行会漏）
    2. 再跳过答案行本身
    """
    if not seg:
        return None, None
    lines = [l for l in _RE_DIVIDER.sub("", seg).split("\n")]
    rgx = _RE_ANS_LINE_ZH if zh else _RE_ANS_LINE_EN

    # 1. 剥题干：逐行判断该行是否是 PDF 题干的一部分
    cut = 0
    if stem_en:
        fs = flat(stem_en).lower()
        for i, l in enumerate(lines):
            s = flat(l)
            if not s:
                continue
            probe = s[:40].lower()
            if len(probe) >= 12 and probe in fs:
                cut = i + 1
            elif cut:
                break
            elif i > 6:
                break
    else:
        # 中文段落没有可比对的原文，改用「题干以问句结尾」这一稳定特征：
        # 前若干行里最后一个以 ？/? 收尾的行即题干末尾。
        # 注意问号后常还跟一个括注，如「…满足这些要求？（选择两项。）」，必须一并允许，
        # 否则所有多选题的中文题干都会丢失。
        for i, l in enumerate(lines[:10]):
            if re.search(r"[？?]\s*(?:[（(][^）)]*[）)])?\s*$", l.strip()):
                cut = i + 1
    stem_part = "\n".join(lines[:cut]).strip() or None
    rest = lines[cut:]

    # 2. 跳过答案行（含多选题写成连续多行的情况）
    first = None
    for i, l in enumerate(rest):
        if l.strip() and rgx.match(l):
            first = i
            break
    if first is not None:
        # 答案块的收尾条件有两个，谁先到算谁：
        #
        #   a) 碰到空行 —— 老格式的答案块自成一段，段后空行才是解析。
        #      这一条必须留着：解析里常把答案字母原样重复一遍再展开（45 题的
        #      `B. …` / `E. …`），只按"像不像答案行"判会把解析本身吃掉。
        #   b) 碰到第一条不像答案行的正文 —— 解析文档尾部（652 起几乎整段）是
        #      `Answer: B) …` 紧跟解析、中间没有空行。只按 a) 判会把整段解析
        #      当成答案块跳过，42 道题的解析就是这么丢的（651 还因此串成了
        #      649 的内容）。
        #
        # 两条都是"提前停"，答案块只会比原来短，解析只会比原来多，不会倒退。
        j = first
        while j < len(rest) and j - first < 6:
            line = rest[j]
            if not line.strip():  # a) 吃掉答案块尾部这一个空行后收工
                j += 1
                break
            if j > first and not rgx.match(line):
                break  # b) 已经是解析正文了
            j += 1
        rest = rest[j:]
    else:
        # 没有字母标记时，答案句通常就是紧接题干的第一段，跳过它
        k = 0
        while k < len(rest) and not rest[k].strip():
            k += 1
        if k < len(rest):
            k += 1
        rest = rest[k:]

    expl = re.sub(r"\n{3,}", "\n\n", "\n".join(rest).strip()).strip()
    return stem_part, (expl or None)


# --------------------------------------------------------------------------
# 阶段一
# --------------------------------------------------------------------------

def stage_extract():
    log("阶段一：抽取 PDF")
    if not os.path.exists(PDF_FILE):
        log("  [错误] 找不到 PDF：%s" % PDF_FILE)
        sys.exit(1)

    qs = parse_pdf(PDF_FILE)
    log("  解析出 %d 题" % len(qs))

    miss = [n for n in range(1, TOTAL_Q + 1) if n not in qs]
    if miss:
        log("  [警告] 缺失题号 %d 个：%s" % (len(miss), miss[:20]))

    os.makedirs(DATA, exist_ok=True)
    payload = [qs[k] for k in sorted(qs)]
    atomic_write(QUESTIONS_EN, json.dumps(payload, ensure_ascii=False, indent=1))
    n_opt = sum(len(q["options"]) for q in payload)
    log("  → %s（%d 题 / %d 选项）" % (os.path.relpath(QUESTIONS_EN, ROOT), len(payload), n_opt))

    sol_segs = read_sol_segs()
    distorted = write_i18n_todo(payload, sol_segs)
    return payload, distorted


def read_sol_segs():
    if not os.path.exists(SOL_EN):
        return {}
    with open(SOL_EN, "r", encoding="utf-8", errors="replace") as f:
        return split_solution(f.read(), _RE_SEG_EN)


def write_i18n_todo(payload, sol_segs):
    """重算待译清单，返回题干失真（PDF 题干 vs 解析文档题干相似度 <0.9）的题号。

    两个阶段都要调。以前只有阶段一写这个文件，于是补完译文只跑 stage_build 时，
    i18n_todo.jsonl 会一直停在上次 --extract 的快照 —— 译文早已 100%，verify
    还在报"待译清单 2405 条"。i18n_next.py 因为会拿 i18n_zh.jsonl 过一遍才没受影响。
    """
    zh_opts, zh_stems, bad = load_i18n_jsonl(I18N_ZH)
    if bad:
        log("  [警告] i18n_zh.jsonl 有 %d 行无法解析，已跳过" % bad)

    todo, distorted = [], []
    for q in payload:
        for o in q["options"]:
            # 图片型选项（PDF 无文字）没有可译内容，不进清单
            if o["text_en"] and (q["id"], o["letter"]) not in zh_opts:
                todo.append({"id": q["id"], "letter": o["letter"], "en": o["text_en"]})
        ratio = stem_similarity(q["stem_en"], sol_segs.get(q["id"]))
        if ratio < 0.9:
            distorted.append(q["id"])
            if q["id"] not in zh_stems:
                todo.append({"id": q["id"], "field": "stem", "en": q["stem_en"]})

    lines = "\n".join(json.dumps(t, ensure_ascii=False) for t in todo)
    atomic_write(I18N_TODO, lines + ("\n" if lines else ""))
    log("  → %s（待译 %d 条：选项 %d + 题干 %d）" % (
        os.path.relpath(I18N_TODO, ROOT), len(todo),
        sum(1 for t in todo if "letter" in t), sum(1 for t in todo if t.get("field") == "stem")))
    log("  题干失真（相似度 <0.9）%d 题：%s" % (len(distorted), distorted[:30]))
    return distorted


# --------------------------------------------------------------------------
# 阶段二
# --------------------------------------------------------------------------

def stage_build():
    if not os.path.exists(QUESTIONS_EN):
        log("未发现 questions_en.json，自动先跑阶段一\n")
        stage_extract()
        log("")

    log("阶段二：合并题库")
    with open(QUESTIONS_EN, "r", encoding="utf-8") as f:
        en_list = json.load(f)
    en = {q["id"]: q for q in en_list}

    # 英文解析
    sol_segs = {}
    if os.path.exists(SOL_EN):
        with open(SOL_EN, "r", encoding="utf-8", errors="replace") as f:
            sol_segs = split_solution(f.read(), _RE_SEG_EN)
        log("  解析文档(英)：切出 %d 段" % len(sol_segs))
    else:
        log("  [警告] 找不到 %s，答案与解析将全部缺失" % os.path.basename(SOL_EN))

    # 中文解析（路 A）
    zh_segs = {}
    if os.path.exists(SOL_ZH):
        with open(SOL_ZH, "r", encoding="utf-8", errors="replace") as f:
            zh_segs = split_solution(f.read(), _RE_SEG_ZH)
        log("  解析文档(中)：切出 %d 段" % len(zh_segs))
    else:
        log("  [警告] 找不到 %s，中文解析缺失（程序仍可用，走英文模式）" % os.path.basename(SOL_ZH))

    # 选项译文（路 B）
    zh_opts, zh_stems, bad = load_i18n_jsonl(I18N_ZH)
    if bad:
        log("  [警告] i18n_zh.jsonl 有 %d 行无法解析，已跳过" % bad)
    log("  选项译文：%d 条；题干补译：%d 条" % (len(zh_opts), len(zh_stems)))

    # 译文补进来了，待译清单要跟着收敛；顺带拿到题干失真的题号
    distorted = set(write_i18n_todo(en_list, sol_segs))

    out = []
    for qid in sorted(en):
        q = dict(en[qid])
        q["options"] = [dict(o) for o in q["options"]]
        seg_en = sol_segs.get(qid)
        seg_zh = zh_segs.get(qid)

        # 答案
        letters, src, conf = resolve_answer(seg_en, q["options"], q["select_count"])
        q["answer"] = letters
        q["answer_source"] = src
        q["answer_confidence"] = conf

        # 题面与解析段开头对不上。多数只是解析文档改写/省略了题干（答案照样是对的），
        # 所以**不能**据此判 needs_review —— 31 题里有 10 题答案没问题，一刀切会
        # 把可出题数从 642 砍到 632，为抓 1 道错题赔掉 9 道好题。
        # 这里只做标记：verify 会把它和答案状态一起列出来，人工复核时有据可查。
        q["stem_mismatch"] = qid in distorted

        # 解析（剥掉与题面重复的题干部分）
        _, q["explanation_en"] = split_segment(seg_en, q["stem_en"], zh=False)
        zh_stem_part, q["explanation_zh"] = split_segment(seg_zh, None, zh=True)

        # 题干中文。
        # i18n_zh.jsonl 里的 field:"stem" 是**人工显式补的**，优先级最高，必须排在
        # 相似度分支之前。原先它排在「head_ratio>=0.9 就用路 A 题干」之后，于是对
        # 638 道路 A 题干可用的题，jsonl 里的修订会被静默忽略 —— 而 app.py 的
        # _overlay_i18n 是无条件覆盖的：浏览器显示修订稿、questions.json 存的是旧稿，
        # 重跑 build 也固化不进去，两边永远对不上。
        head_ratio = stem_similarity(q["stem_en"], seg_en)
        if qid in zh_stems:
            q["stem_zh"] = zh_stems[qid]
            q["stem_zh_source"] = "pdf_translation"
        elif head_ratio >= 0.9 and zh_stem_part:
            q["stem_zh"] = zh_stem_part
            q["stem_zh_source"] = "solution_paraphrase"
        else:
            q["stem_zh"] = None
            q["stem_zh_source"] = None

        # 选项中文
        for o in q["options"]:
            o["text_zh"] = zh_opts.get((qid, o["letter"]))

        q["domain"] = guess_domain(q["stem_en"], q["options"])

        # 质量标记分两类：
        #
        # needs_review —— 语义是「这题的答案不可信，别拿它考我」，会被排除出
        #   考试池与滚动学习出题池。只有答案层面的问题才置位。
        # explanation_quality —— 非阻塞的解析质量标记。源文件里 51–99 等整段
        #   区间本来就只有题目和答案、没有解析（实测 244 题），这些题答案可信、
        #   照常可考，只是答完没有解析可看，不该把它们踢出题库。
        expl_len = len(q["explanation_en"] or "")
        reasons = []
        if qid in MISSING_IN_TXT:
            reasons.append("txt 中不存在该题（源文件 190→201 跳号），永无答案与解析")
        elif seg_en is None:
            reasons.append("txt 缺该题")
        if not letters:
            reasons.append("答案未确定")
        elif len(letters) != q["select_count"]:
            reasons.append("答案个数(%d)与 Choose %d 不符" % (len(letters), q["select_count"]))
        if any(not o["text_en"] for o in q["options"]):
            reasons.append("选项为图片，PDF 中无文字")
        q["needs_review"] = bool(reasons)
        q["review_reason"] = "；".join(reasons) if reasons else None
        q["explanation_quality"] = ("none" if expl_len < 40
                                    else "thin" if expl_len < 120 else "ok")
        out.append(q)

    # 人工修正最后覆盖
    fixed = 0
    if os.path.exists(MANUAL_FIXES):
        try:
            with open(MANUAL_FIXES, "r", encoding="utf-8") as f:
                fixes = json.load(f)
            idx = {q["id"]: q for q in out}
            for k, patch in fixes.items():
                # 下划线开头的键是注释（顶层的 _comment、每题的 _why）。
                # 不跳过的话 int("_comment") 会抛到外层 except，整份修正被静默丢弃。
                if k.startswith("_"):
                    continue
                q = idx.get(int(k))
                if not q:
                    continue
                for field, val in patch.items():
                    if field.startswith("_"):
                        continue
                    q[field] = val
                if "answer" in patch:
                    q["answer_source"] = "manual"
                    q["answer_confidence"] = 1.0
                q["needs_review"] = False
                q["review_reason"] = None
                fixed += 1
            log("  人工修正：应用 %d 题" % fixed)
        except Exception as e:
            log("  [警告] manual_fixes.json 解析失败，已跳过：%s" % e)

    # explanation_quality 必须在人工修正之后重算。原先它在上面的循环里就定好了，
    # 于是手工补了两百多字解析的题仍然挂着 quality="none"，继续留在报告的
    # 「无解析题号」作业清单里 —— 而同一份报告的「解析（英）」计数是修正后重算的，
    # 两边对不上，照清单去补解析会反复补同一题。
    for q in out:
        n = len(q.get("explanation_en") or "")
        q["explanation_quality"] = "none" if n < 40 else "thin" if n < 120 else "ok"

    atomic_write(QUESTIONS, json.dumps(out, ensure_ascii=False, indent=1))
    log("  → %s（%d 题）" % (os.path.relpath(QUESTIONS, ROOT), len(out)))

    write_report(out, bad)
    return out


def write_report(qs, bad_i18n_lines):
    total = len(qs)
    nr = [q for q in qs if q["needs_review"]]
    by_reason = defaultdict(list)
    for q in nr:
        for r in (q["review_reason"] or "").split("；"):
            if r:
                by_reason[r].append(q["id"])

    src_dist = Counter(q["answer_source"] for q in qs)
    stem_src = Counter(q["stem_zh_source"] or "null" for q in qs)
    type_dist = Counter(q["type"] for q in qs)
    optn_dist = Counter("".join(o["letter"] for o in q["options"]) for q in qs)
    dom_dist = Counter(q["domain"] or "null" for q in qs)

    n_opts = sum(len(q["options"]) for q in qs)
    # 覆盖率的分母只能算「可译」的选项。477 题的选项在 PDF 里是图片、没有英文原文，
    # 算进去覆盖率永远到不了 100%，而 verify_bank.py 用的是可译分母 —— 两边报的数
    # 对不上，会让人一直以为还有 4 条没译。
    n_opts_tr = sum(1 for q in qs for o in q["options"] if o["text_en"])
    n_opts_zh = sum(1 for q in qs for o in q["options"] if o["text_zh"])
    n_stem_zh = sum(1 for q in qs if q["stem_zh"])
    n_expl = sum(1 for q in qs if q["explanation_en"])
    n_expl_zh = sum(1 for q in qs if q["explanation_zh"])

    L = []
    A = L.append
    A("# 题库构建报告\n")
    A("## 总览\n")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A("| 总题数 | %d |" % total)
    A("| 成功解析（`needs_review: false`）| **%d** |" % (total - len(nr)))
    A("| 待人工核对（`needs_review: true`）| %d |" % len(nr))
    A("| 选项总数 | %d |" % n_opts)
    A("")

    A("## 译文覆盖率\n")
    A("| 项 | 已译 | 总数 | 覆盖率 |")
    A("|---|---|---|---|")
    A("| 题干（中） | %d | %d | %.1f%% |" % (n_stem_zh, total, 100.0 * n_stem_zh / total))
    A("| **选项（中）** | **%d** | **%d** | **%.1f%%** |" % (
        n_opts_zh, n_opts_tr, 100.0 * n_opts_zh / n_opts_tr if n_opts_tr else 0))
    # 「解析（英）」问的是「684 题里有多少题有解析」，分母就是总题数。
    # 「解析（中）」问的是「有解析的题里译了多少」——SPEC §3.3 写明是
    # 「已译题数 / **有解析的题数**」。原先这一行也拿 total 当分母，把 470/475=98.9%
    # 报成了 470/684=68.7%，看报告的人会一直去追一个不存在的 30 个百分点缺口。
    A("| 解析（英） | %d | %d | %.1f%% |" % (n_expl, total, 100.0 * n_expl / total))
    A("| 解析（中） | %d | %d | %.1f%% |" % (
        n_expl_zh, n_expl, 100.0 * n_expl_zh / n_expl if n_expl else 0.0))
    A("")
    if bad_i18n_lines:
        A("> ⚠️ `i18n_zh.jsonl` 中有 %d 行无法解析，已跳过。\n" % bad_i18n_lines)

    A("## 分布统计\n")
    A("- `answer_source`：" + "，".join("%s %d" % kv for kv in src_dist.most_common()))
    A("- `stem_zh_source`：" + "，".join("%s %d" % kv for kv in stem_src.most_common()))
    A("- 题型：" + "，".join("%s %d" % kv for kv in type_dist.most_common()))
    A("- 选项数：" + "，".join("%s %d" % kv for kv in optn_dist.most_common()))
    A("- `domain`：" + "，".join("%s %d" % kv for kv in dom_dist.most_common()))
    A("")

    A("## 解析质量（非阻塞，不影响能否出题）\n")
    eq = Counter(q.get("explanation_quality") for q in qs)
    A("- 完整（≥120 字符）：%d 题" % eq.get("ok", 0))
    A("- 偏短（40–120 字符）：%d 题" % eq.get("thin", 0))
    A("- **无解析（<40 字符）：%d 题** —— 源文件 51–99 等整段区间本就只有题目与答案\n"
      % eq.get("none", 0))
    for tag, label in (("none", "无解析"), ("thin", "偏短")):
        ids = sorted(q["id"] for q in qs if q.get("explanation_quality") == tag)
        if ids:
            A("<details><summary>%s 题号（%d）</summary>\n" % (label, len(ids)))
            A("`" + ", ".join(str(i) for i in ids) + "`\n")
            A("</details>\n")

    A("## 待人工核对清单（`needs_review: true`，已排除出考试池）\n")
    for reason in sorted(by_reason, key=lambda r: -len(by_reason[r])):
        ids = sorted(by_reason[reason])
        A("### %s（%d 题）\n" % (reason, len(ids)))
        A("`" + ", ".join(str(i) for i in ids) + "`\n")

    A("## 未译选项清单\n")
    # 只算**可译**的选项，和 write_i18n_todo 用同一条判据。原先没排除图片型选项
    # （477 那 4 个 PDF 里就是截图、没有英文原文），于是报告永远写着「共 4 条待译，
    # 详见 data/i18n_todo.jsonl」而那个文件是 0 字节，上一节又刚写完「100%」——
    # 这 4 条成了永远清不掉的假待办，「全部选项均已翻译」那句从来走不到。
    missing = [(q["id"], o["letter"]) for q in qs for o in q["options"]
               if o["text_en"] and not o["text_zh"]]
    if not missing:
        A("全部选项均已翻译。\n")
    else:
        A("共 %d 条待译，详见 `data/i18n_todo.jsonl`。按题号聚合：\n" % len(missing))
        agg = defaultdict(list)
        for qid, letter in missing:
            agg[qid].append(letter)
        rows = ["%d:%s" % (qid, "".join(sorted(v))) for qid, v in sorted(agg.items())]
        for i in range(0, len(rows), 12):
            A("`" + "  ".join(rows[i:i + 12]) + "`")
        A("")

    atomic_write(BUILD_REPORT, "\n".join(L))
    log("  → %s" % os.path.relpath(BUILD_REPORT, ROOT))
    log("")
    log("  ✅ needs_review:false = %d / %d（门槛 ≥620）" % (total - len(nr), total))
    log("  选项中文覆盖率 = %.1f%%（门槛 ≥95%%，分母只算可译选项）"
        % (100.0 * n_opts_zh / n_opts_tr if n_opts_tr else 0))


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="AWS SAA-C03 题库构建（两阶段）")
    ap.add_argument("--extract", action="store_true",
                    help="仅执行阶段一：抽取 PDF，产出 questions_en.json 与 i18n_todo.jsonl")
    args = ap.parse_args()
    if args.extract:
        stage_extract()
    else:
        stage_build()


if __name__ == "__main__":
    main()
