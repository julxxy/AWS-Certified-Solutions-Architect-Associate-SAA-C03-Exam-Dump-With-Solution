#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i18n_next.py — 补译工作台。

    python3 i18n_next.py            # 看进度
    python3 i18n_next.py 100        # 打印下一批 100 条待译（已译的自动跳过）
    python3 i18n_next.py 100 --ctx  # 同上，附带题干作为翻译上下文
    python3 i18n_next.py --check    # 校验 i18n_zh.jsonl 有没有坏行/重复/漏项

翻译结果**追加**到 data/i18n_zh.jsonl，一行一条：
    {"id": 12, "letter": "A", "zh": "…"}
    {"id": 563, "field": "stem", "zh": "…"}
同一 (id, letter) 可以重复出现，以最后一行为准，所以修订直接追加新行即可。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
F_TODO = os.path.join(DATA, "i18n_todo.jsonl")
F_ZH = os.path.join(DATA, "i18n_zh.jsonl")
F_BANK = os.path.join(DATA, "questions.json")


def key(r):
    return (int(r["id"]), r.get("letter") or "STEM")


def load_done():
    done, bad = {}, 0
    if not os.path.exists(F_ZH):
        return done, bad
    for line in open(F_ZH, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            r = json.loads(line)
            if not r.get("zh"):
                raise ValueError("empty zh")
            done[key(r)] = r["zh"]      # 后写覆盖先写
        except Exception:
            bad += 1
    return done, bad


def load_todo():
    if not os.path.exists(F_TODO):
        sys.exit("找不到 data/i18n_todo.jsonl，先跑 python3 build_bank.py --extract")
    return [json.loads(l) for l in open(F_TODO, encoding="utf-8") if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=0, help="本批条数")
    ap.add_argument("--ctx", action="store_true", help="附带题干上下文")
    ap.add_argument("--check", action="store_true", help="校验 i18n_zh.jsonl")
    args = ap.parse_args()

    todo = load_todo()
    done, bad = load_done()
    rest = [r for r in todo if key(r) not in done]

    if args.check:
        print("i18n_zh.jsonl 校验")
        print("  条目（去重后）：%d" % len(done))
        print("  坏行：%d %s" % (bad, "← 需要修" if bad else ""))
        todo_keys = {key(r) for r in todo}
        orphan = [k for k in done if k not in todo_keys]
        print("  不在待译清单中的条目：%d %s" % (
            len(orphan), orphan[:8] if orphan else ""))
        print("  仍待译：%d" % len(rest))
        sys.exit(1 if bad else 0)

    n_opt = sum(1 for r in rest if "letter" in r)
    n_stem = len(rest) - n_opt
    print("进度 %d / %d = %.1f%%    剩余 %d（选项 %d + 题干 %d）%s"
          % (len(done), len(todo), 100.0 * len(done) / len(todo) if todo else 0,
             len(rest), n_opt, n_stem,
             "    ⚠ 有 %d 行坏数据" % bad if bad else ""))
    if not args.n:
        if rest:
            print("\n取下一批：python3 i18n_next.py 100")
        else:
            print("\n✅ 全部译完。跑 python3 build_bank.py && python3 verify_bank.py 固化。")
        return

    batch = rest[: args.n]
    stems = {}
    if args.ctx and os.path.exists(F_BANK):
        stems = {q["id"]: q["stem_en"] for q in json.load(open(F_BANK, encoding="utf-8"))}

    print("\n--- 本批 %d 条（格式：id|letter|英文原文）---" % len(batch))
    last = None
    for r in batch:
        if args.ctx and r["id"] != last:
            last = r["id"]
            s = stems.get(r["id"], "")
            if s:
                print("\n### 题 %d 题干：%s" % (r["id"], s[:220]))
        print("%s|%s|%s" % (r["id"], r.get("letter") or "STEM", r["en"]))


if __name__ == "__main__":
    main()
