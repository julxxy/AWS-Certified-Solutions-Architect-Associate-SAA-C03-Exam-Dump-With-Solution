# AWS SAA-C03 刷题程序 · 产品规格

> **这是规格，不是待执行的任务。** 程序已经建成并在跑（`build_bank.py` / `app.py` /
> `verify_bank.py` / `i18n_next.py` / `start.sh`）。本文件的作用变成三件事：
>
> 1. **记录设计意图** —— §4 那套 SRS 设计（三段式会话、R1–R4 四档、信心度规则、
>    Leitner 间隔、假掌握判定、考试日期倒排）只有这里写着「为什么这么设计」。
>    改行为之前先看它，别从代码反推。
> 2. **验收基准** —— §6。其中数据侧的检查已由 `python3 verify_bank.py` 自动化，
>    行为侧的仍需人工过一遍。
> 3. **历史存档** —— 它最初是交给模型施工的提示词（旧名 `PROMPT-刷题程序.md`），
>    保留原貌便于回看当初的取舍。所以 §0/§5 仍是「对着施工方说话」的口吻，属正常。
>
> **数据源的实况细节不在这里，在 `.claude/skills/saa-bank/reference.md`。**
> 那份是唯一权威，本文件 §2 只留摘要。两边冲突时以 reference.md 为准。
>
> 日常维护走 skill：`saa-bank`。

---

## 0. 角色与总目标

你是一名资深 Python 工程师。在仓库
`/Users/julian/Development/IdeaProjects/AWS-Certified-Solutions-Architect-Associate-SAA-C03-Exam-Dump-With-Solution`
中，构建一个**本地运行、离线可用、中英对照**的 AWS SAA-C03 刷题系统。

交付两样东西：

1. **题库构建脚本**（两阶段，见 §3）—— 先从 PDF 抽出英文题库并生成待译清单，再把 PDF + txt + 两路中文译文合并成结构化 JSON 题库。
2. **本地网页版刷题程序** —— `python3 app.py` 启动，浏览器做题，支持「模拟考试」与「滚动学习」两种模式，进度落盘到 `data/`。

**最高优先级：程序必须能真正跑起来。** 数据脏、题目缺失是既定事实（见 §2），遇到脏数据要标记并跳过，绝不能因此让程序报错或停止交付。

**边界说明：翻译本身不是你的活。** 选项中文由另一个进程产出。你要做的是：① 抽取 PDF 生成待译清单 `data/i18n_todo.jsonl`；② 读取翻译进程回填的 `data/i18n_zh.jsonl` 并合并。**任何情况下都不得自己编造或"顺手翻一下"中文** —— 缺译文就留 `null`，由 UI 显示角标。

---

## 1. 环境与硬约束

| 项 | 要求 |
|---|---|
| Python | 3.9+，**仅标准库**（`http.server`、`json`、`re`、`zlib`、`difflib`、`random`、`argparse`、`webbrowser`、`datetime`、`hashlib`、`os`）|
| 依赖安装 | **禁止** `pip install` 任何第三方包 |
| 前端 | 单文件 HTML，CSS/JS 全部内联，**禁止引用任何 CDN**（必须离线可用）|
| 网络 | 运行期与构建期**均禁止联网** |
| PDF 解析 | **`pdftotext` 本机未安装，且不得安装**。必须用 `zlib` + `re` 纯 Python 解析，解码方法见 `.claude/skills/saa-bank/reference.md`（已实测可用，不要再找其他工具）|
| 启动 | `python3 app.py` → 监听 `127.0.0.1:8765` → 自动打开浏览器 |
| 存储 | 所有状态写入 `data/`，**禁止把 localStorage 作为唯一存储** |
| 只读文件 | **不得修改、移动或删除** `AWS Certified Solutions Architect Associate SAA-C03.pdf`、`AWS SAA-03 Solution.txt`、`AWS SAA-03 Solution.zh-CN.txt`、`README.md`、`.git/`、`.idea/` |

---

## 2. 数据源实况（摘要）

> ⚠️ **完整版在 `.claude/skills/saa-bank/reference.md`，那份是唯一权威。**
> 这里只留一页纸的摘要，供读 §3/§4 时对照。两边不一致时以 reference.md 为准 ——
> 它随实现同步更新，本节不再逐条维护。

| 数据源 | 角色 | 关键事实 |
|---|---|---|
| `AWS Certified ... SAA-C03.pdf` | 题干 + A–F 选项（唯一权威） | 684 题连续无缺；选项 **2831** 个（ABCD 599 / ABCDE 75 / ABCDEF 10，其中 477 题 4 个是图片无文字）；多选 86 题 |
| `AWS SAA-03 Solution.txt` | 答案 + 解析 | 切分命中 **674/684**；36 题需相似度兜底取答案；**254 题源文件本就没有解析** |
| `AWS SAA-03 Solution.zh-CN.txt` | 路 A 中文（已完成） | `stem_zh` + `explanation_zh`；题干与 PDF **逐字相同**（中位相似度 1.000），可直接复用 |
| `data/i18n_zh.jsonl` | 路 B 中文（补译中） | `options[].text_zh` + 失真题干的 `stem_zh`；JSONL 增量追加，热加载 |

**四条绕不过去的硬事实**（细节见 reference.md）：

1. **PDF 要自己解码** —— `pdftotext` 本机没装也不装。Type3 子集字体 + 十六进制字形码，
   统一 `+0x1C` 偏移回 ASCII，另有 6 个连字码位要查表还原。
2. **切分正则不能"优化"** —— 主通道必须要求题号单调递增（否则解析里的编号列表会
   把后续几十题整体错位）；补漏只能用定点修复通道。
3. **第 315 题在 txt 里被误编号为 `215]`**，必须特判。
4. **191–200 在 txt 里根本不存在**，这 10 题永远没有答案和解析。

## 3. 交付物一：`build_bank.py`（题库构建脚本，**两阶段**）

选项中文的源头是 PDF 抽取结果，**翻译进程是本脚本的下游**，所以构建必须拆成两阶段：

```
阶段一   python3 build_bank.py --extract
         ├→ data/questions_en.json   纯英文题库（题干 + A–F 全部选项，权威）
         └→ data/i18n_todo.jsonl     待译清单（2827 条可译选项 + 约 42 条失真题干）
                     ↓
         翻译进程消费 todo，增量追加 data/i18n_zh.jsonl（用 i18n_next.py 驱动，可边译边刷）
                     ↓
阶段二   python3 build_bank.py
         ├→ data/questions.json      合并 EN + 路A + 路B + manual_fixes
         └→ data/build_report.md     数据质量报告
```

- 阶段二若发现 `data/questions_en.json` 不存在，**自动先跑一次阶段一**，不要求用户手动两步
- `i18n_todo.jsonl` 每次重新生成；**已在 `i18n_zh.jsonl` 中译好的条目不再列入**，这样清单会随翻译进度自然收敛到空
- todo 行格式与 `i18n_zh.jsonl` 一致，只是没有 `zh` 字段，改带英文原文：
  `{"id": 12, "letter": "A", "en": "Turn on S3 Transfer Acceleration on the destination S3 bucket…"}`

### 3.1 合并算法

对每个题号 `n ∈ [1, 684]`：

1. 从 PDF 取题干 + 选项列表（权威）。
2. 从 txt 取答案与解析。
3. **确定正确答案字母**，按以下优先级：
   - a. 显式字母：`Answer: B`、`ans- C`、`Correct answer A`、行首 `B. `
   - b. 多选：抓取全部字母（`Answers: A) ... + C) ...` → `["A","C"]`）
   - c. **文本相似度兜底**（关键，覆盖 §2 提到的那 36 题）：把 txt 中的答案句与 PDF 的每个选项做 `difflib.SequenceMatcher` 比对，取最高分；**≥ 0.65 才采纳**，否则判为未确定
   - d. 校验：多选题（`Choose two/three`）提取到的答案个数必须等于 2/3，不符则判为未确定
4. **挂载中文**：
   - `stem_zh` / `explanation_zh` 取自路 A；`stem_zh_source` 相应记为 `solution_paraphrase`
   - 若该题题干相似度 < 0.9，改取 `i18n_zh.jsonl` 中 `field=stem` 的补译，`stem_zh_source` 记为 `pdf_translation`
   - `options[].text_zh` 按 `(id, 原始字母)` 从 `i18n_zh.jsonl` 取；取不到填 `null`
5. 生成记录，打上质量标记。

**191–200 的处理**：这 10 题 PDF 里有完整题干与选项（翻译后也会有完整中文），但永远没有答案和解析。一律 `needs_review: true`、`answer: []`、`answer_source: "unresolved"`，排除出考试池与滚动学习出题池，但**保留在题库里**，允许在「仅浏览」入口下阅读。不要直接丢弃。

### 3.2 题目 JSON Schema

```json
{
  "id": 684,
  "topic": "Topic 1",
  "type": "single",
  "select_count": 1,
  "stem_en": "A company wants to migrate its web applications ...",
  "stem_zh": "某公司希望将其 Web 应用程序从本地迁移到 AWS ...",
  "stem_zh_source": "solution_paraphrase",
  "options": [
    {"letter": "A", "text_en": "Deploy the applications in eu-central-1...", "text_zh": "在 eu-central-1 部署应用程序..."},
    {"letter": "B", "text_en": "Deploy the applications in AWS Local Zones...", "text_zh": "通过将公司的 VPC 从 eu-central-1 扩展到所选的 Local Zone..."}
  ],
  "answer": ["B"],
  "answer_source": "regex_letter",
  "answer_confidence": 1.0,
  "explanation_en": "Local Zones provide single-digit latency ...",
  "explanation_zh": "Local Zones 在靠近 eu-central-1 的位置提供个位数毫秒级延迟 ...",
  "domain": "resilient",
  "needs_review": false,
  "review_reason": null,
  "pdf_page": 249
}
```

- `type`: `single` | `multi`
- `answer_source`: `regex_letter` | `fuzzy_match` | `manual` | `unresolved`
- `stem_zh_source`: `solution_paraphrase`（取自路 A，实测 639 题）| `pdf_translation`（题干失真已补译，实测 42 题）| `null`（无中文）
- `text_zh` / `stem_zh` / `explanation_zh` 缺失时一律填 `null`，**不要填空字符串**，前端靠 `null` 判断是否显示「暂无中文译文」角标
- `needs_review: true` 的条件：**只看答案可不可信** —— 答案未确定 / 答案个数与 Choose N 不符 /
  txt 缺该题 / 选项是图片无文字。置位后该题被排除出考试池与学习出题池。
  - ⚠️ **原始规格曾把「解析长度 < 120 字符」也列为条件，实现时推翻了。** 照那样做
    `needs_review:false` 只剩 **430** 题，远低于 §6 的 620 门槛 —— 因为源文件里 51–99 等
    整段区间本就只有题目和答案、没有解析（实测 254 题）。一道答案确凿、只是没解析的题
    照样能考，不该踢出题库。
- `explanation_quality`: `ok`（≥120 字符）| `thin`（40–120）| `none`（<40）。
  **非阻塞**，只进报告供人工按需补充，不影响能否出题。
- **中文缺失同样不算 `needs_review`**，只影响译文覆盖率统计。
- `domain`（可选加分项）：用关键词启发式打标到 SAA-C03 四大领域，无把握则填 `null`
  - `secure`（安全架构，考纲 30%）：IAM, KMS, WAF, Shield, GuardDuty, Secrets Manager, Cognito…
  - `resilient`（弹性架构，26%）：Multi-AZ, Auto Scaling, Route 53 failover, SQS, backup, DR…
  - `performant`（高性能架构，24%）：CloudFront, ElastiCache, Global Accelerator, read replica, Athena…
  - `cost`（成本优化，20%）：Spot, Savings Plans, S3 Lifecycle, Glacier, Intelligent-Tiering…

### 3.3 `data/build_report.md`（必须生成）

人工修数据的作业清单，包含：

- 总题数 / 成功解析数 / `needs_review` 数
- **逐条列出所有 `needs_review` 的题号与原因**，按原因分组
- `answer_source` 分布统计
- `stem_zh_source` 分布统计
- 单选/多选分布、选项数分布
- **译文覆盖率，拆成三项分别统计**（不要只给一个总数）：
  - 题干：`已译题数 / 684`
  - **选项：`已译选项数 / 2827`　← 这是本次的主要缺口，验收门槛见 §6**
    （题库共 2831 个选项，其中 477 题的 4 个选项在 PDF 里是图片、无文字可译，不计入分母）
  - 解析：`已译题数 / 有解析的题数`
- **未译选项清单**：逐条列出 `(题号, 字母)`，这份清单就是翻译进程的作业依据，需与 `data/i18n_todo.jsonl` 内容一致

### 3.4 人工修正通道

支持 `data/manual_fixes.json`，构建时**最后覆盖**自动结果：

```json
{ "253": { "answer": ["C"], "explanation_en": "手工补充的解析" } }
```

文件不存在则跳过。这样我修数据不用改代码。

---

## 4. 交付物二：`app.py`（本地网页版刷题程序）

### 4.1 模式 A：模拟考试

- **从题库随机抽 65 题**，抽样池**排除 `needs_review: true` 的题**
- 若已打好 `domain` 标签，按考纲配比分层抽样（30/26/24/20），否则纯随机
- 同一场考试内不重复；抽样使用可选 `--seed` 以便复现
- **计分（默认）**：`score = round(1000 * 答对题数 / 65)`，**≥ 720 分通过**（≈ 47/65）
  - 配置项 `scoring_mode`：`linear`（默认，如上）| `aws_scaled`（`100 + 900 * 正确率`，更贴近真实换算）
- **多选计分**：全对才得分，错一个即 0 分（与真实考试一致）。配置项 `partial_credit: false`
- 计时 **130 分钟**倒计时，可在设置里关闭
- 答题过程中可「标记待回看」，交卷前有题号总览面板（已答/未答/已标记 三色）
- 交卷后结果页：总分、是否通过、正确率、用时、**按 domain 的得分分布**、逐题回顾（你的作答 / 正确答案 / 中英对照解析）
- 整场记录落盘 `data/exams/exam_YYYYMMDD_HHMMSS.json`；错题自动进错题本

### 4.2 模式 B：滚动学习（重点需求）

核心诉求：**记住我上次学到哪里了，并且科学地复习学过的内容。**

基础能力：

- **断点续学**：启动首屏显示「上次学到第 N 题 / 已练 X 题 / 已掌握 Y 题 / 错题 Z 题 / 今日到期 W 题」，一个按钮「继续上次」
- 三种出题顺序：`sequential`（按题号顺序，默认）| `random` | `review_first`（错题与到期题优先）
- **每答完一题立即写盘**，进程被杀也不丢进度
- **原子写入**：先写 `xxx.json.tmp` 再 `os.replace()`，避免半截 JSON 损坏存档
- 即时反馈：提交后立刻显示对错 + 中英对照解析
- 错题本页面：可浏览、可手动移出、可只刷错题

---

### 4.2.1 会话结构：三段式（复习的载体）

一次滚动学习会话默认 25 题（可配置），**固定按此结构编排**：

```
├─ 热身复习 Warm-up   ~30% (8 题)  ← 上次错题 + 今日到期题，优先级见 4.2.3
├─ 新题推进 New       ~55% (14 题) ← 从 cursor 继续
└─ 收尾重测 Cool-down ~15% (3 题)  ← 本次会话内答错的题，间隔 ≥10 题后当场重考一次
```

- 每段之间有明确的分隔提示（「热身复习 3/8」这样的进度标识）
- 若无到期题，热身段自动缩短，把配额让给新题
- Cool-down 的重测结果**不计入 box 升降**，只用于当场巩固与提示

### 4.2.2 四档复习强度（核心设计）

同一道复习题，按掌握程度用**不同强度**呈现，而不是一律重做一遍：

| 档 | 名称 | 呈现方式 | 适用 |
|---|---|---|---|
| **R1** | 完整重做 | 正常答题界面，**选项乱序** | box 1–2（新错/不熟）|
| **R2** | 干扰项狙击 | 只呈现「你上次选错的那个选项」vs「正确选项」，二选一；选完展示解析中针对该干扰项的辨析 | 有错误记录的题，优先于 R1 |
| **R3** | 闪卡自评 | 只显示题干，**选项全部遮蔽**；让我心里回想答案 → 点「翻开」对照 → 自评「记得 / 模糊 / 忘了」 | box 3–4 |
| **R4** | 速览确认 | 题干 + 正确答案 + 解析首句，一屏一题，快速翻页，不作答 | box 5 已掌握；考前 48 小时冲刺 |

- 默认由 box 自动选档，界面上允许手动覆盖
- R3 的自评映射：记得 → 等同答对；模糊 → box 不动；忘了 → 等同答错，降到 box 1
- **R4 不改变任何 box 状态**（它只是维持性重看，不能当作复习凭据）

### 4.2.3 信心度打分（必做，专治「背题库」）

每题提交答案时**同时选一个信心档**：`有把握` / `不确定` / `蒙的`（键盘 `J/K/L`）。

判定规则：

| 结果 | 处理 |
|---|---|
| 对 + 有把握 | 正常升 box |
| 对 + 不确定 | box 升一级但 `next_due` 折半 |
| **对 + 蒙的** | **按答错处理**，box 不升，记为「假掌握」 |
| 错 + 不确定/蒙的 | 降到 box 1 |
| **错 + 有把握** | 降到 box 1，并**置顶到下次会话热身段第一位**（高信心错误一旦被纠正，记忆留存最强）|

### 4.2.4 选项乱序（必做）

每次出题都**打乱选项顺序并重新分配字母**。这是对抗「记住答案是 B」的唯一有效手段——固定题库刷到第三轮时，人记住的是选项位置而不是知识点。

**硬性约束（中英对照下最容易出的 bug）**：乱序**只允许对 `options` 数组整体做一次 `random.shuffle`**，让 `text_en` 与 `text_zh` 作为同一个对象一起搬运。**禁止**对任何单语字段单独排序或分别洗牌——否则会出现「英文显示 A 的内容、中文显示 C 的内容」，而双语并排时两边看起来都像正常选项，极难发现。

实现注意：解析文本里常引用字母（如「Option C 是错的」「（选项 A）」）。做法二选一：

- a. 渲染解析时按本次乱序映射把字母替换掉（推荐）
- b. 解析区额外显示一份「原始字母 → 本次字母」对照表

选 a 的话，**中英两份解析都要替换**。中文解析里的字母引用写法不统一，实测出现过 `选项 A`、`（选项 A）`、`Option C`、`A.` 等多种形式，替换正则要一并覆盖。

### 4.2.5 间隔重复调度

- Leitner 5 个 box，间隔天数 `[1, 2, 4, 9, 21]`
- **注意 box 1 的间隔是 1 天而不是 0 天**：同一天内重复刷同一题收益很低，隔夜的第一次复习性价比最高。当次会话内的巩固由 Cool-down 段负责，不占用 box 调度
- 到期题按此优先级排序进入热身段：
  1. 错 + 有把握（高信心错误）
  2. 「假掌握」标记（对 + 蒙的）
  3. 逾期天数最多的
  4. 连续错误次数最多的
- **交错（interleaving）**：热身段禁止同一 `domain` 连续超过 2 题，强制打散。混合出题比按主题集中练更接近真实考场的辨别负荷

### 4.2.6 掌握判定

**不要「答对一次就算掌握」。** 一道题标记为 `mastered` 需同时满足：

- 累计答对 ≥ 3 次
- 这 3 次分布在 **≥ 3 个不同日期**
- 最近一次为「对 + 有把握」

已 `mastered` 的题退出常规轮转，只在 R4 速览和考前冲刺中出现。

### 4.2.7 考试日期倒排（可选但强烈建议）

`settings.json` 支持 `exam_date`（默认 `null`）。设定后：

- 自动压缩间隔序列，保证**每道未掌握的题在考前至少还能再过一遍**
- 首页显示「距考试 X 天 / 未掌握 Y 题 / 建议每天 Z 题」
- 考前 7 天自动进入冲刺模式：只出 `box ≤ 3` 与「假掌握」的题
- 考前 48 小时自动切 R4 速览为主

### 4.2.8 复习效果面板

新增一个「学习统计」页：

- 各 box 的题目数量分布（堆叠柱状图，纯 CSS/内联 SVG 实现，不引外部图表库）
- 未来 7 天每日到期题数预测（让我知道明天要花多少时间）
- **「假掌握」清单**：`对+蒙的` 比例高的题单独列出，这是最容易在考场翻车的部分
- 各 `domain` 的正确率对比，指出最弱的一块

### 4.3 前端交互

- 顶部工具条：**中/英/双语** 三态切换（默认双语并排，窄屏则上下堆叠）
  - 「中文」单语模式下，题干、**选项**、解析都要显示中文；某字段无中文则**该字段单独回退英文**并挂灰色角标，不要整题退回英文
  - 「双语」模式下选项区为「英文一行 + 中文一行」成对显示，中文缺失时该选项只显示英文
- 键盘快捷键：`1-6` 选择选项、`Enter` 提交/下一题、`T` 切换语言、`M` 标记、`←/→` 翻题
- 多选题渲染为 checkbox 并提示「本题需选 N 项」
- 深色/浅色模式切换，跟随系统
- 移动端可用的响应式布局
- 长题干不截断、代码/服务名用等宽字体

### 4.4 `data/` 目录结构

```
data/
├── questions_en.json     # 阶段一产物：纯英文题库（PDF 抽取，权威）
├── i18n_todo.jsonl       # 阶段一产物：待译清单，每次重新生成
├── i18n_zh.jsonl         # 翻译进程产物：选项/题干中文，增量追加，热加载
├── questions.json        # 阶段二产物：合并后的题库
├── build_report.md       # 阶段二产物：数据质量报告
├── manual_fixes.json     # 可选：人工修正（手写）
├── progress.json         # 滚动学习进度（cursor + 每题 SRS 状态）
├── wrong_book.json       # 错题本
├── settings.json         # 用户设置（语言模式、计时、计分模式、主题、exam_date、每次会话题量）
└── exams/
    └── exam_20260817_193000.json
```

`progress.json` 结构：

```json
{
  "version": 1,
  "updated_at": "2026-08-17T19:30:00+08:00",
  "cursor": {"mode": "sequential", "last_question_id": 137, "position": 136},
  "stats": {"answered": 240, "correct": 191, "mastered": 88, "false_mastery": 12},
  "last_session": {
    "ended_at": "2026-08-17T19:30:00+08:00",
    "counts": {"warmup": 8, "new": 14, "cooldown": 3},
    "wrong_ids": [212, 388, 501]
  },
  "questions": {
    "137": {
      "attempts": 3, "correct": 2, "wrong": 1,
      "last_result": "correct",
      "last_confidence": "sure",
      "correct_dates": ["2026-08-11", "2026-08-14", "2026-08-17"],
      "streak": 2,
      "box": 3,
      "mastered": false,
      "false_mastery": false,
      "high_conf_error": false,
      "wrong_picks": {"C": 1},
      "last_seen_at": "2026-08-17T19:29:41+08:00",
      "next_due_at": "2026-08-21T19:29:41+08:00"
    }
  }
}
```

- `last_confidence`: `sure` | `unsure` | `guess`
- `correct_dates`: 去重后的日期列表，用于 §4.2.6 的「跨 3 个不同日期」判定
- `wrong_picks`: 记录我选错时选了哪个选项（**记录选项的原始字母**，因为渲染时会乱序），R2 干扰项狙击靠它取干扰项

**向前兼容**：读取时若 `version` 不匹配或字段缺失，走迁移/补默认值，**不要直接抛异常**。

---

## 5. 歧义处理原则

遇到本文件没写清楚的情况，按此优先级自行决策，**不要停下来问，做完后在总结里说明你的选择**：

1. 程序能跑 > 功能完整 > 数据完美
2. 数据缺失 → 标记 `needs_review` 并排除出考试池，不阻塞
3. 格式无法识别 → 降级 + 打警告日志，不崩溃
4. 需要第三方库才能做的功能 → 换标准库方案或砍掉该功能

---

## 6. 验收标准

> **数据侧的检查已经自动化**：`python3 verify_bank.py`（结构完整性、抽取质量、题型分布、
> 已知脏数据标记、与上次基线的回归对比）。数据源更新后跑它就够，不用手工过下面的清单。
>
> 下面标 🤖 的项目由 `verify_bank.py` 覆盖，标 👤 的仍需人工在浏览器里过一遍。


- [ ] 🤖 `python3 build_bank.py --extract` 跑通，`data/questions_en.json` 含 684 条，`data/i18n_todo.jsonl` 已生成
- [ ] 🤖 抽取出的选项总数为 **2831**（其中 477 题 4 个为图片型无文字），选项数分布为 ABCD 599 / ABCDE 75 / ABCDEF 10
- [ ] 🤖 抽取文本中**没有断字**（全文搜 `les`、`traÊc`、`Éow` 等应无命中，`configure`/`traffic`/`flow` 拼写完整）
- [ ] 🤖 `python3 build_bank.py` 一次跑通，`data/questions.json` 含 684 条记录
- [ ] 🤖 `needs_review: false` 的题数 **≥ 620**（低于此数说明匹配算法有问题，需改进）
- [ ] 🤖 `data/build_report.md` 已生成，逐条列出待人工核对的题号
- [ ] 🤖 抽查题号 **1 / 51 / 253 / 315 / 684**：题干、选项数、正确答案、解析是否正确对应（253 与 315 属已知脏数据，应被正确标记；**315 需从 txt 中误编号的 `215]` 正确取到**，且真正的 215 未被覆盖）
- [ ] 🤖 191–200 这 10 题存在于 `questions.json`、有题干与选项、`needs_review: true`、且不出现在考试与滚动学习的出题池中
- [ ] 🤖 多选题（如 18、44、51）`type == "multi"` 且 `answer` 长度与 `Choose two/three` 一致
- [ ] 👤 `python3 app.py` 启动无报错，浏览器自动打开
- [ ] 👤 断网状态下页面完整可用（无任何外链资源）
- [ ] 👤 模拟考试抽满 65 题、无重复、计分与 720 分及格线正确
- [ ] 👤 滚动学习答 3 题后**直接 kill 进程**，重启能准确恢复到第 4 题且进度不丢
- [ ] 👤 同一道题连续进入两次，**选项顺序不同**，且解析中的字母引用与本次乱序一致（无错位）
- [ ] 👤 选「对 + 蒙的」后，该题 box **未升级**且被标记为「假掌握」，出现在统计页清单里
- [ ] 👤 选「错 + 有把握」后，该题出现在**下次会话热身段的第一位**
- [ ] 👤 会话编排符合三段式：热身题全部来自错题/到期题，Cool-down 只含本次答错的题
- [ ] 👤 R3 闪卡模式下选项确实被遮蔽，「翻开」后才可见；自评三档能正确改写 box
- [ ] 👤 设置 `exam_date` 为 5 天后，间隔序列被压缩且首页显示「距考试 5 天 / 建议每天 N 题」
- [ ] 👤 一道题在 3 个不同日期各答对一次后才变 `mastered`；同一天答对 3 次**不算**
- [ ] 🤖 **选项中文覆盖率 ≥ 95%**（即 `text_zh` 非 null 的选项 ≥ 2686 / 2827），且该数字出现在 `build_report.md` 中
- [ ] 👤 切到**「中文」单语模式**，选项区显示的是中文而不是英文
- [ ] 👤 缺中文的选项**单独回退英文**并显示灰色角标，不会导致整题退回英文
- [ ] 👤 **乱序后中英不错位**：同一选项的 `text_en` 与 `text_zh` 始终成对；连续刷同一题 5 次，每次中英内容都对得上
- [ ] 👤 中英**两份**解析里的字母引用都按本次乱序正确替换（中文的 `（选项 A）`、英文的 `Option A` 都要变）
- [ ] 🤖 `stem_zh_source` 中约 26 题为 `pdf_translation`，其余为 `solution_paraphrase`
- [ ] 👤 两个译文文件都不存在时程序正常运行（纯英文模式，仅警告）
- [ ] 👤 `data/i18n_zh.jsonl` 中途追加新行后，翻新题时能自动加载到新译文（热加载生效）
- [ ] 👤 `i18n_zh.jsonl` 中混入一行非法 JSON，加载时**跳过该行并计数**，不影响其余译文
- [ ] 🤖 原始 PDF / `AWS SAA-03 Solution.txt` / `AWS SAA-03 Solution.zh-CN.txt` / README / .git 未被修改（用 `git status` 确认）

---

## 7. 交付格式

完成后给出一份简短总结，包含：

1. 新增文件清单及各自职责（一句话）
2. 启动与使用命令
3. `build_report.md` 的关键数字（成功率、待核对题数）
4. §6 验收清单的逐条结果
5. 你做过的取舍与已知限制
