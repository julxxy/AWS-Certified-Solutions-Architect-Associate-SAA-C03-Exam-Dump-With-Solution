# 待核对题目 TODO

创建日期：2026-09-05。依据当前 `data/questions.json` 与构建逻辑整理。

当前共 684 题，683 题可用于学习和模拟考试。剩余 #543 已确认选项重叠，等待题面或选项修订，继续排除出学习和考试池。

本文件保留待办、完整英文原题和核对结论。已入库的修正及来源保存在 `data/manual_fixes.json`；完成入库和自检后，移除对应待办与临时记录。

## 怎么核对

1. 每次选 3–5 题，按题号查看原 PDF 的完整题干和选项。
2. 对照 AWS 官方文档核验，记录正确选项、理由、其他选项的问题及来源链接；注意题目条件和服务功能是否存在年代差异。
3. 核对多选题要求的选项数量。答案字母以原 PDF / 题库原始选项为准，练习页面会打乱选项，不能直接抄页面字母。
4. 在本文件末尾复制记录模板保存结论；确认题面完整且答案有依据后，再勾选对应题目。有争议的题保持未勾选，并记录疑点。
5. 勾选只表示人工核对完成。以后决定写回题库时，再执行文末的入库待办。
6. 入库并通过自检后，移除该题的待办和临时核对记录，并更新剩余题数。

以下保留 1 道已确认选项重叠的题目，完整英文题干和原始选项保持原样。后续需取得能消除重叠的修订题面或选项；页码取自题库 `pdf_page`，仅作 PDF 定位提示。

## 有歧义的文字题（1 题）

本题的技术规则及选项重叠已经核实，但没有唯一判分答案。A+E 仅保留为作答倾向，不代表官方考试答案。

- [ ] #543 · PDF 定位 199：已确认选项重叠：A+E、B+E、A+D 均可行；A+E 仅作答倾向，等待题面或选项修订。

### #543 · PDF 定位 199

核对结论：已确认选项重叠：A+E、B+E、A+D 均可行；A+E 仅作答倾向，等待题面或选项修订。

英文原题：

```text
Question #543

A company runs Amazon EC2 instances in multiple AWS accounts that are individually bled. The company recently purchased a Savings Pian. Because of changes in the company’s business requirements, the company has decommissioned a large number of EC2 instances. The company wants to use its Savings Plan discounts on its other AWS accounts. Which combination of steps will meet these requirements? (Choose two.)

A. From the AWS Account Management Console of the management account, turn on discount sharing from the billing preferences section.

B. From the AWS Account Management Console of the account that purchased the existing Savings Plan, turn on discount sharing from the billing preferences section. Include all accounts.

C. From the AWS Organizations management account, use AWS Resource Access Manager (AWS RAM) to share the Savings Plan with other accounts.

D. Create an organization in AWS Organizations in a new payer account. Invite the other AWS accounts to join the organization from the management account.

E. Create an organization in AWS Organizations in the existing AWS account with the existing EC2 instances and Savings Plan. Invite the other AWS accounts to join the organization from the management account.

```

核对结论（2026-09-05，结合用户提供的 Claude 分析）：

- 已确认：共享由组织管理账户控制；购买方与接收方都需启用共享。E 使购买账户成为管理账户，因此 A、B 在该组合中重叠。
- 已确认：新管理账户可以为持有 Savings Plan 的成员账户配置共享。D 技术上成立，且符合管理账户不承载业务负载的建议；原题没有最少操作要求。
- 已确认：Savings Plans 在组织内默认启用共享，A/B 可理解为确认共享设置。实际仍应检查偏好和用量资格。

| 组合        | 技术判断                                                           |
|-------------|--------------------------------------------------------------------|
| A+E         | 可行：现有购买账户创建组织，再作为管理账户确认相关账户的共享设置。 |
| B+E         | 可行：E 下购买账户就是管理账户，B 明确包含所有账户。               |
| A+D         | 可行：新建管理账户，邀请购买账户和其他账户加入，再配置共享。       |
| 含 C 的组合 | 不成立：Savings Plans 折扣不通过 AWS RAM 共享。                    |

作答倾向：用户提供的 Claude 分析倾向 A+E，但没有证据证明这是命题者的唯一预期，暂不设置判分答案。当前待办是取得修订题面或选项，以消除已确认的重叠；不再保留重复询问机器人的追问。

AWS 官方依据：

- [共享设置的管理账户权限及购买方、接收方条件](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/ri-turn-off.html)
- [创建组织的账户成为管理账户](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_org_create.html)
- [Savings Plans 在合并账单家庭中的应用顺序](https://docs.aws.amazon.com/savingsplans/latest/userguide/sp-applying.html)
- [Savings Plans 默认启用共享](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/savings-plans.html)
- [管理账户不承载业务负载的建议](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_best-practices_mgmt-acct.html)
- [AWS RAM 支持的资源类型](https://docs.aws.amazon.com/ram/latest/userguide/shareable.html)

## 核对记录模板

核对时可在本文件末尾复制以下模板，保存尚未入库的结论或疑点。入库并通过自检后移除该记录。

```text
题号：#
核对日期：
结论：已确认 / 有争议 / 题面仍缺内容
正确选项（原始字母）：
正确选项原文或关键内容：
题干、选项或策略需要补充的内容：
选择理由：
其他选项为什么不合适：
AWS 官方文档链接及支持的结论：
题目年代与当前服务行为差异（如有）：
待解决疑点：
是否已写回题库：否
```

## 核对完成后的入库待办（以后处理）

项目已有 `data/manual_fixes.json` 人工修正入口。核对结果可以通过该文件持久保存，再重新构建；直接编辑生成的 `data/questions.json` 会在下次构建时被覆盖。

- [ ] 将已确认答案、解析与核对依据写入人工修正记录；仍有争议或题面不完整的题继续保留待核对状态。
- [ ] 完善人工修正的放行校验：先验证题面完整、答案字母合法及数量正确，再决定是否清除待核对标记；避免仅补解析就被视为已确认答案。
- [ ] 正式入库时重新构建并自检，更新质量报告，确认已修复题进入学习与考试池，未解决题继续排除。注意构建会写产物，自检通过后也会更新
  `data/verify_baseline.json`。

## 页面功能待办（可选，以后决定是否实现）

目前“仅看待核对”只显示题干摘要和核对原因，没有完整题目详情或核对操作入口。

- [ ] 支持查看完整中英题干、选项、策略内容及原 PDF 定位信息。
- [ ] 支持填写答案、解析、来源链接和核对备注。
- [ ] 区分“答案未确认”“题面缺内容”“有争议”“已确认”等状态，并支持确认通过或保留待核对。
