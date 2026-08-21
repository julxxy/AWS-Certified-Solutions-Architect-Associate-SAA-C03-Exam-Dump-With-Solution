# AWS SAA-C03 Exam Dump + 中英对照刷题程序

AWS Certified Solutions Architect – Associate (SAA-C03) 备考题库，含 684 道多选题、 答案与解析，以及一个 **本地运行、离线可用、中英对照**的刷题程序。

## 上游项目 / Upstream

本仓库 fork 自
**[Iamrushabhshahh/AWS-Certified-Solutions-Architect-Associate-SAA-C03-Exam-Dump-With-Solution](https://github.com/Iamrushabhshahh/AWS-Certified-Solutions-Architect-Associate-SAA-C03-Exam-Dump-With-Solution)**
（作者 [@Iamrushabhshahh](https://github.com/Iamrushabhshahh)，初始提交 2024-02-01）。

原始题库（PDF + 解答 txt）与解答的社区修正均归功于上游作者及其贡献者。 本仓库在此基础上增加了中文译文与刷题工具，题库文件保持只读、未作改动。

> This repository is a fork of the upstream project linked above. All original
> exam questions and community-contributed answer corrections belong to the
> upstream authors. This fork adds a Chinese translation layer and a local
> offline quiz application on top; the original source files are kept read-only.

## 本仓库新增了什么

| 新增         | 说明                                                              |
|--------------|-------------------------------------------------------------------|
| **中文译文** | 解析全文已译（`AWS SAA-03 Solution.zh-CN.txt`）；选项译文补译中   |
| **刷题程序** | 本地网页版，模拟考试 + 间隔重复学习，仅用 Python 标准库、完全离线 |
| **题库构建** | 从 PDF/txt 解析合并成结构化 JSON，含数据质量报告与回归自检        |

## 快速开始

只需 Python 3.9+， **无需安装任何第三方包**：

```bash
./start.sh # 构建（按需）+ 启动 + 直接进入学习
./start.sh exam # 直接进模拟考试
./start.sh --stop
./start.sh --help # 全部入口与选项
```

浏览器会打开 `http://127.0.0.1:8765`（改端口用 `SAA_PORT=9000 ./start.sh`）。全程不联网，页面无任何外链资源。

除 `learn`（默认）和 `exam` 外，还可以直接开到 `home` / `wrong` / `stats` / `browse`。

## 功能

**模拟考试** —— 65 题 / 130 分钟倒计时 / ≥720 分通过，按考纲四大领域 （安全 30%、弹性 26%、高性能 24%、成本 20%）分层抽样，交卷后按领域给出得分分布
与逐题中英对照回顾。

**滚动学习** —— 每场会话按「热身复习 → 新题推进 → 收尾重测」三段编排，核心机制：

- **Leitner 间隔重复**：5 个 box，间隔 `[1, 2, 4, 9, 21]` 天
- **信心度打分**：每题提交时选「有把握 / 不确定 / 蒙的」。 **答对但选了「蒙的」按答错处理**
  并标记为「假掌握」—— 专治背题库
- **四档复习强度**：完整重做 / 干扰项狙击（只给你上次选错的那项 vs 正确项）/ 闪卡自评 / 速览确认，按掌握程度自动选档
- **选项乱序**：每次出题打乱选项并重新分配字母，解析里的字母引用同步改写。 固定题库刷到第三轮时，人记住的是选项位置而不是知识点
- **掌握判定**：累计答对 ≥3 次、分布在 ≥3 个不同日期、且最近一次为「对 + 有把握」
- **考试日期倒排**：设定考试日后自动压缩间隔，考前 7 天进入冲刺模式

进度每答一题即原子写盘，进程被杀也不丢。

## 目录结构

```
├── AWS Certified ... SAA-C03.pdf   题干与选项（上游原始文件，只读）
├── AWS SAA-03 Solution.txt         答案与解析（上游原始文件，只读）
├── AWS SAA-03 Solution.zh-CN.txt   解析的中文译文
├── SPEC-刷题程序.md                产品规格与验收基准
├── start.sh                        一键入口
├── scripts/
│   ├── build_bank.py               题库构建（两阶段）
│   ├── app.py                      刷题程序
│   ├── verify_bank.py              自检 + 基线回归对比
│   └── i18n_next.py                补译工作台
└── data/                           构建产物与本地学习状态
```

个人学习状态（进度、错题本、模考记录）只存本地，不入库。

## 数据质量

上游题库存在已知的脏数据，构建时会逐条标记并写进 `data/build_report.md`：

- **可出题 642 / 684** —— 其余 42 题源文件本身没写答案（含 191–200 这 10 题在解答 文档里完全缺失），已标记 `needs_review` 并排除出考试池
- **254 题没有解析** —— 源文件里 51–99 等整段区间只有题目和答案。这些题答案可信， 照常出题，只是答完没有解析可看
- 第 315 题在解答文档里被误编号为 `215]`，构建时特判还原

数据源更新后跑 `python3 scripts/verify_bank.py`，它会与上次基线比对，防止指标静默劣化。

## 贡献

题目答案有误 → 欢迎提到 **上游仓库**，让所有人受益。 刷题程序或中文译文的问题 → 提到本仓库。

## 免责声明

本仓库仅为备考辅助，不保证通过考试。AWS 服务与特性持续变化，答案请交叉验证， 并结合动手实践与官方文档使用。

祝备考顺利。
