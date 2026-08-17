---
name: saa-bank
description: 维护本仓库的 AWS SAA-C03 刷题系统。当 PDF、Solution.txt 或中文译文有更新需要重建题库时；当要补译选项/题干（data/i18n_zh.jsonl）时；当题目解析出错、答案判定不对、选项错位、译文不显示时；当要新增题目或调整 SRS/考试逻辑时使用。关键词：题库、重建、build_bank、刷题、补译、i18n、questions.json。
---

# AWS SAA-C03 题库维护

## 先看这里：这套数据的坑都是实测出来的

改任何解析逻辑前先读 `reference.md`。里面记的每一条都对应一次真实的翻车， 凭直觉「优化」正则或阈值大概率会让指标掉几百题，而且不报错、只是静默劣化。

## 系统组成

```
AWS Certified ... SAA-C03.pdf   题干 + A–F 选项（唯一权威来源，只读）
AWS SAA-03 Solution.txt         答案 + 解析（只读）
AWS SAA-03 Solution.zh-CN.txt   上一份的中文译文（只读）
        │
        ├─ build_bank.py --extract → data/questions_en.json + data/i18n_todo.jsonl
        │                                                          ↓ 翻译进程补
        │                                                     data/i18n_zh.jsonl
        └─ build_bank.py           → data/questions.json + data/build_report.md
                                              ↓
                                    app.py（本地 web，热加载译文）
```

三个源文件 **只读**，任何情况下不要改动它们。

## 常见任务

### 1. 数据源更新后重建

```bash
python3 build_bank.py --extract # PDF 变了才需要；只有 txt 变了可跳过
python3 build_bank.py
python3 verify_bank.py # 必跑：和上次基线对比，防止静默劣化
```

`verify_bank.py` 会把关键指标写进 `data/verify_baseline.json`。 **任一指标低于基线就是回归**， 先查清楚原因再决定要不要接受新基线（有检查项失败时基线不会被覆盖）。

`./start.sh` 会自动检测源文件是否比 `data/questions.json` 新，需要时自动重建。

### 2. 补译（当前主要待办）

用 `i18n_next.py` 驱动，不用手工翻清单：

```bash
python3 i18n_next.py            # 看进度
python3 i18n_next.py 100        # 取下一批 100 条（已译的自动跳过）
python3 i18n_next.py 100 --ctx  # 同上，附带题干作为上下文
python3 i18n_next.py --check    # 校验有没有坏行/重复/漏项
```

翻完一批 → 追加到 `data/i18n_zh.jsonl` → 再取下一批。**断点续译天然支持**：
已译条目按 `(id, letter)` 去重跳过，中断后再跑 `i18n_next.py` 就接着上次。

待译清单是 `data/i18n_todo.jsonl`，每行：

```json
{
  "id": 12,
  "letter": "A",
  "en": "Turn on S3 Transfer Acceleration…"
}
{
  "id": 563,
  "field": "stem",
  "en": "A company runs its applications on both…"
}
```

翻译结果 **追加**到 `data/i18n_zh.jsonl`（不要整体重写，追加才安全）：

```json
{
  "id": 12,
  "letter": "A",
  "zh": "在目标 S3 存储桶上开启 S3 Transfer Acceleration…"
}
```

规矩：

- AWS 服务名保留英文（Amazon S3 / EC2 / Lambda / Aurora…），其余译成中文
- 同一 `(id, letter)` 可以重复出现， **以最后一行为准**，所以修订直接追加新行即可
- 坏行会被跳过并计数，不会中断加载 —— 但 `verify_bank.py` 会报出来，别放着不管
- **绝不允许编造翻译**。没把握就留着不译，UI 会显示英文原文 + 灰色角标
- 译完不必重跑 build：`app.py` 每次取题会检查 mtime 热加载。重跑 build 只是把译文固化进
  `questions.json`，顺便让 `i18n_todo.jsonl` 收敛

补完一批后：

```bash
python3 build_bank.py && python3 verify_bank.py
```

### 3. 启动

```bash
./start.sh # 构建（按需）+ 启动 + 直接进入滚动学习
./start.sh exam # 直接进模拟考试
./start.sh --rebuild # 强制重跑两阶段
./start.sh --stop
```

### 4. 修个别题的数据

不要改代码，用 `data/manual_fixes.json`（构建时最后覆盖，并强制清掉 needs_review）：

```json
{
  "253": {
    "answer": [
      "C"
    ],
    "explanation_en": "手工补充的解析"
  }
}
```

## 出问题时先查什么

| 症状                                 | 大概率原因                            | 看 reference.md 哪节 |
|--------------------------------------|---------------------------------------|----------------------|
| 某段题号的题干/答案整体错位          | 解析正文里的编号列表被当成题号        | §切分                |
| 题目数对但选项数不对                 | Topic 行归属、图片型选项被丢弃        | §PDF 抽取            |
| 抽出来的文本有 `traÊc`、`log les`    | 连字表没用对                          | §PDF 抽取            |
| 大量题变成 needs_review              | 解析长度阈值语义被改回阻塞式          | §质量标记            |
| 解析里的「Option C」与屏幕选项对不上 | 乱序字母重映射漏了某种写法            | §乱序                |
| 中文不显示                           | JSONL 坏行 / letter 大小写 / 没热加载 | §译文                |

## 新增题目时

`build_bank.py` 里 `TOTAL_Q = 684` 是硬编码上限，题目变多必须同步改；
`MISSING_IN_TXT`（191–200）和 315 误编号这两条特判是针对当前源文件的， 换了数据源要重新核对是否还成立 —— `verify_bank.py` 的「已知脏数据」段会告诉你。
