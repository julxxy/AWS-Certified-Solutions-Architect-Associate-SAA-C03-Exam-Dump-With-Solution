#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_bank.py — 题库自检。数据源更新后跑这个，确认没有静默劣化。

    python3 scripts/verify_bank.py            # 全量自检
    python3 scripts/verify_bank.py --strict   # 任一项不达标则退出码非 0（给 CI / 钩子用）

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

# 复用 build_bank 的常量与切分逻辑：题号上限、315 误编号的还原通道，都得对着
# 同一份实现验，否则「验证脚本自己抄了一份会漂」的问题迟早出现。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_bank  # noqa: E402

# 脚本在 scripts/ 下，仓库根目录要再往上退一层。
# 别"简化"成 dirname(__file__) —— 那样 data/ 会解析到 scripts/data/，
# 题库读不到、进度写错地方，而且不报错。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
F_BANK = os.path.join(DATA, "questions.json")
F_EN = os.path.join(DATA, "questions_en.json")
F_TODO = os.path.join(DATA, "i18n_todo.jsonl")
F_I18N = os.path.join(DATA, "i18n_zh.jsonl")
F_REPORT = os.path.join(DATA, "build_report.md")
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
        print("找不到 data/questions.json，先跑 python3 scripts/build_bank.py")
        sys.exit(2)

    bank = {q["id"]: q for q in json.load(open(F_BANK, encoding="utf-8"))}
    ids = sorted(bank)
    base = json.load(open(F_BASE, encoding="utf-8")) if os.path.exists(F_BASE) else {}

    def vs_base(key, cur, label, tol=0):
        """单向基线：只防「掉下去」。用于会随补译/修数据单调变好的指标。"""
        old = base.get(key)
        if old is None:
            info(label, "%s（首次记录，将写入基线）" % cur)
            return True
        return check(cur >= old - tol, label,
                     "当前 %d / 基线 %d%s" % (cur, old, "（↓ 掉了）" if cur < old - tol else ""))

    def vs_base_exact(key, cur, label):
        """双向基线：变大变小都是回归。

        题数、选项总数、选项数分布这类结构性指标不该「变好」——多切出一个选项
        和少切一个同样是抽取逻辑坏了。原先它们只走 info()，reference.md
        §PDF 抽取「变了就要查」的那组基准数字实际上没有任何人在守。
        """
        old = base.get(key)
        if old is None:
            info(label, "%s（首次记录，将写入基线）" % cur)
            return True
        return check(cur == old, label,
                     "当前 %s / 基线 %s%s" % (cur, old, "（≠ 变了）" if cur != old else ""))
    total = len(bank)

    print("\n【硬性】结构完整性")
    check(total > 0, "题库非空", "%d 题" % total)
    # 对 range(1, TOTAL_Q+1) 比，不能拿实际首尾比 —— 后者在题库只剩 1–600 时照样 ✅。
    check(ids == list(range(1, build_bank.TOTAL_Q + 1)),
          "题号 1–%d 连续无缺口" % build_bank.TOTAL_Q,
          "实际 %d–%d 共 %d 题" % (ids[0], ids[-1], total))
    # §6 第一项：阶段一产物也要在。verify 只看 questions.json 的话，
    # questions_en.json 被删/被截断都发现不了，而阶段二只读它。
    if os.path.exists(F_EN):
        en_list = json.load(open(F_EN, encoding="utf-8"))
        check(len(en_list) == build_bank.TOTAL_Q, "questions_en.json 含 %d 条" % build_bank.TOTAL_Q,
              "%d 条" % len(en_list))
    else:
        check(False, "questions_en.json 存在", "缺失 —— 阶段二只读它，先跑 --extract")
    check(os.path.exists(F_REPORT), "build_report.md 已生成")
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
    # 只查这 4 个字形是不够的：偏移后落在 0x80–0xFF 的码位还有一批，出现次数少
    # （¼ ½ ¹ 和一个 U+0087 一共才 6 处 / 4 道题），当年就是这么漏过去的。
    # 改成「白名单之外的非 ASCII 一律报」，LIGATURES 再漏码位就会当场暴露。
    ALLOWED = set("’‘“”•–—…×°Е")  # 正常会出现的排印字符 + 源文档自带的同形字 typo
    stray = sorted({c for c in blob if ord(c) > 127 and c not in ALLOWED})
    check(not stray, "无白名单之外的非 ASCII 残留",
          "残留 %s" % [(c, "U+%04X" % ord(c)) for c in stray[:6]] if stray else "")
    broken = re.findall(r"\b(?:les|ow|ows|traÊc|conÈg\w*)\b", blob)
    check(len(broken) < 5, "无明显断字", "疑似 %d 处" % len(broken))
    check("traffic" in blob and "configure" in blob.lower() and "files" in blob,
          "常见连字词拼写完整（traffic/configure/files）")

    print("\n【硬性】题型与分布")
    n_opts = sum(len(q["options"]) for q in bank.values())
    dist = Counter("".join(o["letter"] for o in q["options"]) for q in bank.values())
    dist_str = "，".join("%s %d" % kv for kv in sorted(dist.items()))
    vs_base_exact("total", total, "总题数与基线一致")
    vs_base_exact("n_opts", n_opts, "选项总数与基线一致")
    vs_base_exact("opt_dist", dist_str, "选项数分布与基线一致")
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

    vs_base("usable", usable, "可出题数不低于基线")
    vs_base("regex_letter", src.get("regex_letter", 0), "regex 直接命中答案数不低于基线")
    vs_base("expl_ok", eq.get("ok", 0), "完整解析题数不低于基线")
    check(usable >= 620, "可出题数 ≥ 620（§6 门槛）", "%d" % usable)
    info("answer_source", "，".join("%s %d" % kv for kv in src.most_common()))
    info("解析质量", "完整 %d / 偏短 %d / 无 %d"
         % (eq.get("ok", 0), eq.get("thin", 0), eq.get("none", 0)))

    print("\n【硬性】已知脏数据仍被正确标记")
    check(477 in bank and bank[477]["needs_review"],
          "#477 已标记（选项为图片，PDF 无文字）",
          bank[477].get("review_reason") or "" if 477 in bank else "题号不存在")
    check(all(i in bank for i in range(191, 201)) and
          all(bank[i]["needs_review"] and bank[i]["options"] for i in range(191, 201)),
          "191–200 保留题干选项但排除出题池")

    segs = None
    # §6 要求验的是「315 从误编号的 215] 正确取到，且真正的 215 没被覆盖」。
    # 光看 questions.json 验不了这件事：题干来自 PDF，段落来自 txt，成品里看不出
    # 段落是从哪儿切的。原先那句 `needs_review or explanation_quality != "ok"` 是
    # 恒真式 —— 315 那段源文件本就只有题干没答案，两个条件必成立，位置推断修复通道
    # （build_bank 的修复通道 B）整个坏掉也照样 ✅。所以这里重跑一次切分，直接对着
    # 切出来的段落验。
    if os.path.exists(build_bank.SOL_EN):
        text = open(build_bank.SOL_EN, encoding="utf-8", errors="replace").read()
        segs = build_bank.split_solution(text, build_bank._RE_SEG_EN)
        vs_base_exact("sol_segs", len(segs), "解析文档切出段数与基线一致")

        def seg_matches(qid, n=60):
            """切出来的这一段，开头是不是这道题的题干。"""
            seg = segs.get(qid)
            if not seg or qid not in bank:
                return False
            head = re.sub(r"\s+", " ", seg).strip().lower()
            stem = re.sub(r"\s+", " ", bank[qid]["stem_en"]).strip().lower()
            return head.startswith(stem[:n])

        check(seg_matches(315), "#315 已从误编号的 215] 正确取到（修复通道 B 生效）",
              "切出 %d 字符" % len(segs.get(315) or ""))
        check(seg_matches(215), "真正的 215 未被误编号覆盖",
              "answer=%s" % (bank[215]["answer"] if 215 in bank else "?"))
        check(not any(i in segs for i in range(191, 201)),
              "191–200 在 txt 里确实不存在（没有被别的段落顶替）")
    else:
        info("跳过切分复验", "找不到 %s" % os.path.basename(build_bank.SOL_EN))

    # 题面与解析段开头对不上的题。多数只是解析文档改写了题干，答案照样对，
    # 所以不判失败、也不踢出出题池 —— 但要点名，人工抽查时从这份名单开始。
    # 651 当初就藏在这里：解析文档在它段尾多粘了一份 649 的解析。
    mism = sorted(i for i, q in bank.items() if q.get("stem_mismatch"))
    live = [i for i in mism if not bank[i]["needs_review"] and bank[i].get("answer")]
    info("题面与解析开头不符", "%d 题，其中 %d 题仍在出题池" % (len(mism), len(live)))
    if live:
        info("  └ 建议人工抽查", "%s" % live[:30])

    print("\n【基线】译文覆盖")
    zh_o = sum(1 for q in bank.values() for o in q["options"] if o.get("text_zh"))
    trans_o = sum(1 for q in bank.values() for o in q["options"] if o["text_en"])
    zh_s = sum(1 for q in bank.values() if q.get("stem_zh"))
    pct = 100.0 * zh_o / trans_o if trans_o else 0
    info("选项中文", "%d / %d 可译（%.1f%%）" % (zh_o, trans_o, pct))
    info("题干中文", "%d / %d" % (zh_s, total))
    vs_base("zh_options", zh_o, "选项译文数不低于基线（不能倒退）")
    # 这里原先写的是 `if pct >= 95: check(True, ...) else: info(...)` —— 分支一拆，
    # 这项检查就结构上永远不可能失败：覆盖率跌到 3% 也照打「全部通过」、退出码 0、
    # --strict 不报错、基线照写。§6 明确把它列成门槛，就得是真断言。
    check(pct >= 95, "选项中文覆盖率 ≥ 95%（§6 门槛）",
          "%.1f%%%s" % (pct, "" if pct >= 95 else "，缺译处显示英文 + 角标"))

    if os.path.exists(F_TODO):
        n_todo = sum(1 for _ in open(F_TODO, encoding="utf-8"))
        info("待译清单", "%d 条（data/i18n_todo.jsonl）" % n_todo)
    if os.path.exists(F_I18N):
        # 直接复用 build_bank 的加载器，不再自己抄一份判据。原先这里的内联解析
        # 与 build_bank / app.py 有两处不一致，方向还相反：
        #   · `//` 注释行不跳 → 一行合法注释就让本项失败、基线从此不再更新；
        #   · `r.get("letter") or "stem"` 把漏写 letter 的行当题干条目收下 ——
        #     而 build_bank 判它是坏行直接丢弃，app.py 取 rec["letter"] 会 KeyError
        #     也算坏行。于是同一份文件：译文静默丢失，验证却全绿。
        zh_opts, zh_stems, bad = build_bank.load_i18n_jsonl(F_I18N)
        check(bad == 0, "i18n_zh.jsonl 无坏行（判据与 build_bank/app 一致）",
              "%d 行无法解析" % bad if bad else "")
        info("译文条目（去重后）", "%d（选项 %d + 题干 %d）"
             % (len(zh_opts) + len(zh_stems), len(zh_opts), len(zh_stems)))

    print("\n【提示】领域分布（影响考试分层抽样）")
    dom = Counter(q.get("domain") or "null" for q in bank.values())
    info("domain", "，".join("%s %d" % kv for kv in dom.most_common()))
    if dom.get("null", 0) > total * 0.5:
        info("提示", "超过一半题未打领域标签，考试抽样会退化为纯随机")

    # 写基线
    newbase = {"usable": usable, "regex_letter": src.get("regex_letter", 0),
               "expl_ok": eq.get("ok", 0), "zh_options": zh_o, "total": total,
               "n_opts": n_opts, "opt_dist": dist_str}
    if segs is not None:
        newbase["sol_segs"] = len(segs)
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
