---
name: saa-bank
description: 维护本仓库的 AWS SAA-C03 刷题系统。当 PDF、Solution.txt 或中文译文有更新需要重建题库时；当要核对存疑题目、补答案或解析（data/manual_fixes.json）时；当要补译选项/题干（data/i18n_zh.jsonl）时；当题目解析出错、答案判定不对、选项错位、译文不显示时；当要新增题目或调整 SRS/考试逻辑时使用。关键词：题库、重建、build_bank、刷题、核对、manual_fixes、补译、i18n、questions.json。
---

# AWS SAA-C03 题库维护

## 先看这里：这套数据的坑都是实测出来的

改任何解析逻辑前先读 `reference.md`。里面记的每一条都对应一次真实的翻车， 凭直觉「优化」正则或阈值大概率会让指标掉几百题，而且不报错、只是静默劣化。

## 系统组成

```
仓库根/
├── AWS Certified ... SAA-C03.pdf   题干 + A–F 选项（唯一权威来源，只读）
├── AWS SAA-03 Solution.txt         答案 + 解析（只读）
├── AWS SAA-03 Solution.zh-CN.txt   路 A 中文译文（只读）
├── SPEC-刷题程序.md                产品规格与验收基准
├── TODO-待核对题目.md              人工核对工作台（存疑题目的原题与结论）
├── start.sh                        一键入口（留在根目录）
├── scripts/                        所有 Python 脚本都在这里
│   ├── build_bank.py               题库构建（两阶段）
│   ├── app.py                      本地 web 刷题程序（只有后端）
│   ├── verify_bank.py              自检 + 基线回归对比
│   └── i18n_next.py                补译工作台
├── web/index.html                  前端全部（markup + 内联 CSS/JS），按 mtime 热加载
└── data/                           全部产物与状态

数据流：
  三个源文件
        │
        ├─ scripts/build_bank.py --extract → data/questions_en.json + data/i18n_todo.jsonl
        │                                                                ↓ 翻译进程补
        │                                                           data/i18n_zh.jsonl
        └─ scripts/build_bank.py           → data/questions.json + data/build_report.md
                                                    ↓
                                          scripts/app.py（本地 web，热加载译文）
```

三个源文件 **只读**，任何情况下不要改动它们。

`i18n_todo.jsonl` 两个阶段都会重算 —— 只跑阶段二也会收敛，不会停在上次 `--extract` 的快照。

⚠️ **脚本在 `scripts/` 下，但所有路径都相对仓库根解析**（脚本内 `ROOT` 多退一层）。
所以从任何目录调用都能跑，`data/` 永远指向仓库根下那个。改 `ROOT` 时别"简化"掉那层
`dirname` —— 会让 `data/` 跑到 `scripts/data/`，而且不报错。

## 常见任务

### 1. 数据源更新后重建

```bash
python3 scripts/build_bank.py --extract # PDF 变了才需要；只有 txt 变了可跳过
python3 scripts/build_bank.py
python3 scripts/verify_bank.py # 必跑：和上次基线对比，防止静默劣化
```

`verify_bank.py` 会把关键指标写进 `data/verify_baseline.json`。题数、选项总数、选项数分布、
切分段数是 **双向** 基线，变大变小都算回归（多切出一个选项和少切一个同样是抽取坏了）；其余是
单向的， **低于基线就是回归**。先查清原因再决定要不要接受新基线（有检查项失败时基线不会被覆盖）。

它还硬性校验几处已知脏数据的修复状态：#477 的图片转录、191–200 的人工答案、315/215 误编号、
191–200 确实不在 txt 里。 **这些期望值写死在 `verify_bank.py` 里** —— 改对应的 `manual_fixes.json`
必须同步改它，否则自检直接红。

`./start.sh` 的重建判断是 **两段的**，别记成「比 `questions.json` 新就重建」：

| 阶段              | 输入                                                                | 比对产物                 |
|-------------------|---------------------------------------------------------------------|--------------------------|
| 一（`--extract`） | PDF、`build_bank.py`                                                | `data/questions_en.json` |
| 二                | `questions_en.json`、两份 txt、`manual_fixes.json`、`build_bank.py` | `data/questions.json`    |

阶段一只能跟 `questions_en.json` 比：跟 `questions.json` 比的话，改完抽取逻辑跑 `start.sh` 只会跑
阶段二，而阶段二只读 `questions_en.json` —— 抽取修复一个字都不生效，脚本还照打「重新构建题库」。
`manual_fixes.json` 必须在阶段二的输入里，否则改了手工修正不重建完全不生效，而且一声不吭。
`i18n_zh.jsonl` 故意不参与：`app.py` 热加载译文，重建只是把它固化进 `questions.json`。

### 2. 核对存疑题目（当前主要待办）

工作台是根目录的 `TODO-待核对题目.md`：每道待核题保留完整英文原题、核对结论与来源链接。
当前只剩 **#543** 一道（Savings Plan 折扣共享，选项重叠、无唯一判分答案）。

流程固定：查 PDF 原题 + AWS 官方文档核验 → 结论写进 `TODO` → 用 `data/manual_fixes.json` 入库
（见下节）→ `build_bank.py` + `verify_bank.py` → 从 `TODO` 里移除该题并更新剩余题数。

三条实测出来的规矩：

- **答案字母以 PDF / `questions_en.json` 的原始字母为准**。练习页面每次出题都会打乱选项并重新
  分配字母，照屏幕抄字母必错。
- 先数清 `(Choose two/three)` 要几个 —— 答案个数与 `select_count` 不符会被判 `needs_review`。
- 核不出唯一答案就 **不要硬填**：显式 `needs_review: true` + `review_reason` 留在池外。塞一个
  编造的答案会让用户选对反被判错，比这题不出更糟（坑 8b 就是这么来的）。

### 3. 用 `manual_fixes.json` 修个别题

不要改代码。这个文件在构建的最后一步覆盖字段，键是题号字符串：

```json
{
  "_comment": "顶层注释",
  "253": {
    "_why": [
      "2026-09-05：核对结论与判断依据"
    ],
    "_sources": [
      "https://docs.aws.amazon.com/..."
    ],
    "answer": [
      "C"
    ],
    "explanation_en": "…",
    "explanation_zh": "…"
  }
}
```

- **下划线开头的键是注释**，构建时跳过。当前 53 条修正每条都带 `_why`（判断依据）、多数带
  `_sources`（官方文档链接）—— 这是本仓库的约定，新增修正照做。
- 可覆盖：`answer`、`explanation_en/zh`、`stem_en`、`stem_zh`（要同时写 `stem_zh_source`）、
  `options`、`needs_review`/`review_reason`。
- 带了 `answer` 会自动置 `answer_source: "manual"`、`answer_confidence: 1.0`，不用手写。
- `explanation_quality` 写了没用 —— 人工修正之后按解析长度重算（有意为之，见 reference §人工修正）。

⚠️ 三条会咬人的，细节在 reference.md §人工修正：

1. **`needs_review` 是无条件重写的**。只要题号出现在这个文件里，哪怕只补了 `explanation_zh`，
   标记也会被重置为 `patch.get("needs_review", False)`。给「选项是图片 / 答案个数不符」的题只补
   解析，会把它悄悄放回出题池。要保留就显式写 `needs_review: true` + `review_reason`。
2. **替换 `options` 要整组齐**：`letter` 保持原始字母，`text_en` 与 `text_zh` 成对给；并且 **`i18n_zh.jsonl` 里要补同样的 `(id, letter)` 行**
   ，否则浏览器显示的是 jsonl 里的旧译文。
3. **`verify_bank.py` 盯着其中几题**：#477 的四个策略选项（含 C/D 斜杠差异）、191–200 的十个
   答案都写死在自检里，改这些题必须两边一起改。

### 4. 补译（选项译文已 100%，只在题库变动后需要）

`data/i18n_todo.jsonl` 当前是空的：选项 **2832/2832**、题干 **684/684** 都有中文。只有新增题目、
`manual_fixes` 换了选项文本、或按 PDF 重译失真题干时才会重新出现待译条目。

用 `i18n_next.py` 驱动，不用手工翻清单：

```bash
python3 scripts/i18n_next.py # 看进度
python3 scripts/i18n_next.py 100 # 取下一批 100 条（已译的自动跳过）
python3 scripts/i18n_next.py 100 --ctx # 同上，附带题干作为上下文
python3 scripts/i18n_next.py --check # 校验有没有坏行/重复/漏项
```

翻完一批 → 追加到 `data/i18n_zh.jsonl` → 再取下一批。 **断点续译天然支持**：
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
python3 scripts/build_bank.py && python3 scripts/verify_bank.py
```

### 5. 启动

```bash
./start.sh # 构建（按需）+ 启动 + 直接进入滚动学习
./start.sh exam # 直接进模拟考试；另有 home / wrong / stats / browse
./start.sh --rebuild # 强制重跑两阶段，可与页面连用：./start.sh --rebuild home
./start.sh stop # 停服务；stop 与 --stop 等价，放在任意位置都认
./start.sh --help # 用法的唯一事实来源（usage()），别在别处再抄一份
SAA_PORT=9000 ./start.sh # 换端口，默认 8765
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
| 只补了解析，题却回到出题池           | manual_fixes 无条件重写 needs_review  | §人工修正            |
| 浏览器中文与 questions.json 不一致   | jsonl 热加载覆盖手工修正的译文        | §人工修正            |

## 要改前端时

页面在 `web/index.html`，`app.py` 只负责读它并原样返回。改完刷新浏览器就生效，
不用重启服务（按 mtime 热加载）。

SPEC §技术选型 那条「单文件 HTML，CSS/JS 全部内联，禁止引用任何 CDN」约束的是 **发出去的页面**，不是源码怎么放。所以别把 CSS/JS 拆成 `/static/*`
之类的额外
请求路由 —— 断网可用是验收项（§6 有一条 👤 要人工过）。

## 要改程序行为（不是改数据）时

本 skill 只管数据侧。 **产品行为的设计意图在仓库根目录的 `SPEC-刷题程序.md`**：
三段式会话编排、R1–R4 四档复习强度、信心度打分规则、Leitner 间隔序列、
掌握判定条件、考试计分与 720 分线、`exam_date` 倒排、交错出题约束 —— 这些
「为什么这么设计」只有那份文件写着。改 SRS 策略或重做 UI 之前先读它，
别直接从 `app.py` 反推。

该文件 §6 是验收清单，标 🤖 的项已由 `verify_bank.py` 自动化，标 👤 的需人工过。

## 新增题目时

`build_bank.py` 里 `TOTAL_Q = 684` 是硬编码上限，题目变多必须同步改；
`MISSING_IN_TXT`（191–200）和 315 误编号这两条特判是针对当前源文件的， 换了数据源要重新核对是否
还成立 —— `verify_bank.py` 的「已知脏数据」段会告诉你，那一段是 **会失败的断言**，不是提示：
它写死了 191–200 的十个人工答案和 #477 转录后的四条策略。源文件换了，先改那一段，别去松断言。
