#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_bank.py — 题库自检。数据源更新后跑这个，确认没有静默劣化。

    python3 verify_bank.py            # 全量自检
    python3 verify_bank.py --strict   # 任一项不达标则退出码非 0（给 CI / 钩子用）

检查项分三类：
  硬性  —— 不通过说明构建逻辑坏了，必须修
  基线  —— 与上次记录的基线比较，掉了要查（基线存在 data/verify_baseline.json）
  提示  —— 只报告，不判定
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
F_BANK = os.path.join(DATA, "questions.json")
F_EN = os.path.join(DATA, "questions_en.json")
F_TODO = os.path.join(DATA, "i18n_todo.jsonl")
F_I18N = os.path.join(DATA, "i18n_zh.jsonl")
F_BASE = os.path.join(DATA, "verify_baseline.json")

OK, BAD, INFO = "✅", "❌", "·"
_fail = []


def check(cond, label, detail=""):
    print("  %s %s%s" % (OK if cond else BAD, label, ("  " + detail) if detail else ""))
    if not cond:
        _fail.append(label)
    return cond


def info(label, detail=""):
    print("  %s %s  %s" % (INFO, label, detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(F_BANK):
        print("找不到 data/questions.json，先跑 python3 build_bank.py")
        sys.exit(2)

    bank = {q["id"]: q for q in json.load(open(F_BANK, encoding="utf-8"))}
    ids = sorted(bank)
    total = len(bank)

    print("\n【硬性】结构完整性")
    check(total > 0, "题库非空", "%d 题" % total)
    check(ids == list(range(ids[0], ids[-1] + 1)), "题号连续无缺口",
          "%d–%d" % (ids[0], ids[-1]))
    check(all(q.get("stem_en") for q in bank.values()), "每题都有英文题干")
    check(all(q.get("options") for q in bank.values()), "每题都有选项")
    letters_ok = all([o["letter"] for o in q["options"]] ==
                     [chr(65 + i) for i in range(len(q["options"]))]
                     for q in bank.values())
    check(letters_ok, "选项字母连续且从 A 起")
    check(all(len(q["answer"]) == q["select_count"]
              for q in bank.values() if q["answer"]),
          "有答案的题，答案个数与 Choose N 一致")
    check(all(set(q["answer"]) <= {o["letter"] for o in q["options"]}
              for q in bank.values()), "答案字母都在选项范围内")

    print("\n【硬性】PDF 抽取质量")
    blob = " ".join(q["stem_en"] + " " + " ".join(o["text_en"] for o in q["options"])
                    for q in bank.values())
    lig = [c for c in "ÈÊÉË" if c in blob]
    check(not lig, "无未还原的连字字形", "残留 %s" % lig if lig else "")
    broken = re.findall(r"\b(?:les|ow|ows|traÊc|conÈg\w*)\b", blob)
    check(len(broken) < 5, "无明显断字", "疑似 %d 处" % len(broken))
    check("traffic" in blob and "configure" in blob.lower() and "files" in blob,
          "常见连字词拼写完整（traffic/configure/files）")

    print("\n【硬性】题型与分布")
    n_opts = sum(len(q["options"]) for q in bank.values())
    dist = Counter("".join(o["letter"] for o in q["options"]) for q in bank.values())
    info("选项总数", str(n_opts))
    info("选项数分布", "，".join("%s %d" % kv for kv in dist.most_common()))
    multi = [q for q in bank.values() if q["type"] == "multi"]
    check(all(q["select_count"] in (2, 3) for q in multi),
          "多选题 select_count 只能是 2 或 3", "%d 道多选" % len(multi))
    check(all(re.search(r"\(Choose (two|three)", q["stem_en"], re.I) for q in multi),
          "多选题题干确实含 (Choose two/three)")
    single_with_choose = [q["id"] for q in bank.values()
                          if q["type"] == "single"
                          and re.search(r"\(Choose (two|three)", q["stem_en"], re.I)]
    check(not single_with_choose, "没有「题干说多选但被判成单选」的题",
          str(single_with_choose[:5]))

    print("\n【基线】可用题量与答案解析率")
    usable = sum(1 for q in bank.values() if not q["needs_review"] and q["answer"])
    src = Counter(q["answer_source"] for q in bank.values())
    eq = Counter(q.get("explanation_quality") for q in bank.values())
    base = json.load(open(F_BASE, encoding="utf-8")) if os.path.exists(F_BASE) else {}

    def vs_base(key, cur, label, tol=0):
        old = base.get(key)
        if old is None:
            info(label, "%d（首次记录，将写入基线）" % cur)
            return True
        return check(cur >= old - tol, label,
                     "当前 %d / 基线 %d%s" % (cur, old, "（↓ 掉了）" if cur < old - tol else ""))

    vs_base("usable", usable, "可出题数不低于基线")
    vs_base("regex_letter", src.get("regex_letter", 0), "regex 直接命中答案数不低于基线")
    vs_base("expl_ok", eq.get("ok", 0), "完整解析题数不低于基线")
    check(usable >= 620, "可出题数 ≥ 620（§6 门槛）", "%d" % usable)
    info("answer_source", "，".join("%s %d" % kv for kv in src.most_common()))
    info("解析质量", "完整 %d / 偏短 %d / 无 %d"
         % (eq.get("ok", 0), eq.get("thin", 0), eq.get("none", 0)))

    print("\n【硬性】已知脏数据仍被正确标记")
    known = {
        315: "误编号为 215]，需从错号处取到",
        477: "选项为图片，PDF 无文字",
    }
    for qid, why in known.items():
        if qid in bank:
            q = bank[qid]
            check(q["needs_review"] or q.get("explanation_quality") != "ok",
                  "#%d 已标记（%s）" % (qid, why), q.get("review_reason") or "")
    gap = [i for i in range(191, 201) if i in bank]
    if gap:
        check(all(bank[i]["needs_review"] and bank[i]["options"] for i in gap),
              "191–200 保留题干选项但排除出题池")
    if 215 in bank:
        check(bool(bank[215]["answer"]), "真正的 215 未被误编号覆盖",
              "answer=%s" % bank[215]["answer"])

    print("\n【基线】译文覆盖")
    zh_o = sum(1 for q in bank.values() for o in q["options"] if o.get("text_zh"))
    trans_o = sum(1 for q in bank.values() for o in q["options"] if o["text_en"])
    zh_s = sum(1 for q in bank.values() if q.get("stem_zh"))
    pct = 100.0 * zh_o / trans_o if trans_o else 0
    info("选项中文", "%d / %d 可译（%.1f%%）" % (zh_o, trans_o, pct))
    info("题干中文", "%d / %d" % (zh_s, total))
    vs_base("zh_options", zh_o, "选项译文数不低于基线（不能倒退）")
    if pct >= 95:
        check(True, "选项中文覆盖率 ≥ 95%%（§6 门槛）", "%.1f%%" % pct)
    else:
        info("选项中文覆盖率未达 95% 门槛", "%.1f%%，缺译处显示英文 + 角标" % pct)

    if os.path.exists(F_TODO):
        n_todo = sum(1 for _ in open(F_TODO, encoding="utf-8"))
        info("待译清单", "%d 条（data/i18n_todo.jsonl）" % n_todo)
    if os.path.exists(F_I18N):
        bad = 0
        seen = set()
        for line in open(F_I18N, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                k = (int(r["id"]), r.get("letter") or "stem")
                if not r.get("zh"):
                    raise ValueError
                seen.add(k)
            except Exception:
                bad += 1
        check(bad == 0, "i18n_zh.jsonl 无坏行", "%d 行无法解析" % bad if bad else "")
        info("译文条目（去重后）", str(len(seen)))

    print("\n【提示】领域分布（影响考试分层抽样）")
    dom = Counter(q.get("domain") or "null" for q in bank.values())
    info("domain", "，".join("%s %d" % kv for kv in dom.most_common()))
    if dom.get("null", 0) > total * 0.5:
        info("提示", "超过一半题未打领域标签，考试抽样会退化为纯随机")

    # 写基线
    newbase = {"usable": usable, "regex_letter": src.get("regex_letter", 0),
               "expl_ok": eq.get("ok", 0), "zh_options": zh_o, "total": total}
    if not _fail:
        tmp = F_BASE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(newbase, f, ensure_ascii=False, indent=1)
        os.replace(tmp, F_BASE)

    print("\n" + "=" * 56)
    if _fail:
        print("❌ %d 项未通过：" % len(_fail))
        for f in _fail:
            print("   - " + f)
        print("\n基线未更新。修好后重跑。")
    else:
        print("✅ 全部通过，基线已更新 → data/verify_baseline.json")
    print("=" * 56)
    if args.strict and _fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
