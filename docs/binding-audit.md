# 书帖工单 v3 · 装订后离线抽检（装订审计）

v3 在同一 CLI 中以显式 `--binding-audit` 开关启用，与 `--proof-batch` 互斥（同时给出两个开关
退出 2）；不带开关时 v1 行为不变，v2 规划行为不变（见 [v1 契约](contract.md) 与
[打样批契约](proof-batch.md)）。本模式只做离线质检：不接触打印机、外网，不改切帖、不补印、
不换纸、不重新领用旧帖，也绝不改变工单。

## 输入

源文件是严格 JSON，顶层恰好两个字段：

```json
{
  "proof_batch": { ... v2 规划输入 ... },
  "observed_sheets": [
    {
      "physical_id": "P",
      "possible_targets": [{"edition_id": "jia", "signature": 1, "sheet": 1}],
      "front_from": {"edition_id": "jia", "signature": 1, "sheet": 1},
      "back_from": {"edition_id": "jia", "signature": 1, "sheet": 1}
    }
  ]
}
```

- `proof_batch`：现有 v2 规划输入。库内以当前 `process_batch` 的结果作为**本次已执行的
  工单**；审计数据不能改变工单。工单本身非法（v2 规则）即为输入错误。
- **目标位置**：工单中每个 `fresh_print` 帖的每张纸都是一个目标位置，引用格式为
  `{"edition_id", "signature", "sheet"}`，两个编号取工单的当前帖号与帖内张号（均从 1 起）。
  每个版次对应一册。
- **旧帖**：`retained` 帖按整帖已确认且已入册处理，不出现在新印实物、候选位置或版面引用
  中；任何指向旧帖的引用都是输入错误（不是"未知引用"）。
- `observed_sheets`：每一项代表实际进入这批样书的一张纸（不含废纸或待用库存）。
  - `physical_id`：沿用仓库标识符规则（`[A-Za-z0-9_:-]{1,64}`），批内唯一。
  - `possible_targets`：1–16 个不重复的目标引用，输送线确实不能排除的去向；不同实物可
    申报同一位置。
  - `front_from` / `back_from`：各引用工单中任意一个新印位置，分别表示实物正面确实用了
    那个位置的正面版单、背面确实用了那个位置的背面版单。两者不必来自同一位置，同一版单
    也可以重复印在不同实物上；引用版面不表示纸张送到了那个位置。
- 记录总数必须恰好等于工单新印张数；全批只用旧帖时必须为空数组。输入已统一正背面与左右
  方向，不从图像推断翻转，也不审计纸料或油墨。

## 判定模型

**完整解释**：每张实物去到它的候选位置之一，且每个新印位置恰好收到一张。仅用实物流向证据
决定哪些解释成立；即使某张纸印错，它也可能真的装进了书，不能因版面不合而删掉它的去向。

**位置合格**：在某种完整解释里，一个位置合格，当且仅当拿到的实物在正背两面、各面从左到右
的页位上，带修订号的逻辑页（包括 `null` 空位）和 `placement` 页框逐项等于该位置的工单。
两面分别用各自的版单核对；不同的版单引用也可以印出相同的一面。来源帖号、张号和用于折后
排序的 `folio` 编号不是印面身份，不作相等条件。

**一册合格**：它的全部新印位置在**同一个**完整解释里都合格；不是每个位置各自找得到好纸
就算通过。没有新印位置的版次在物流可解释时恒为合格。

逐版结论：

- `verified`：每种完整解释里该册都合格；
- `defective`：没有一种完整解释让该册合格；
- `uncertain`：既可能合格也可能有错。

## 输出（`schema_version: 3`）

原子新建、严格 JSON、拒绝覆盖任何已存在的目标（含符号链接）；与 v1/v2 相同的发布约定。
存在完整解释时顶层为：

```json
{
  "schema_version": 3,
  "logistics": "explainable",
  "positions": [
    {"edition_id": "jia", "signature": 1, "sheet": 1,
     "arrivals": [{"physical_id": "P", "qualified": true},
                  {"physical_id": "R", "qualified": false}]}
  ],
  "editions": [
    {"id": "jia", "status": "uncertain",
     "qualified_evidence":   {"assignment": [ ... ]},
     "unqualified_evidence": {"assignment": [ ... ], "mismatch": { ... }}},
    {"id": "yi", "status": "defective",
     "unqualified_evidence": {"assignment": [ ... ], "mismatch": { ... }},
     "deficiency": {"targets": [ ... ], "sheets": [ ... ]}}
  ],
  "summary": {"logistics": "explainable", "physical_sheets": 4, "target_positions": 4,
              "verified": 0, "uncertain": 1, "defective": 1}
}
```

- `positions`：按工单顺序逐位置列出 `arrivals` —— **至少在一种完整解释里真正可能到达**
  该位置的实物及其合格与否（按 `physical_id` 排序）。输送线给的原始候选不会照抄进来
  （被其他位置的占用排除的去向不会出现）。
- `editions`：按工单顺序逐版给出 `status`，并按存在的情形附证据：
  - 存在合格解释（`verified` 或 `uncertain`）→ `qualified_evidence`；
  - 存在不合格解释（`defective` 或 `uncertain`）→ `unqualified_evidence`；
  - `defective` 额外附 `deficiency`。
- `assignment`：证据的核心是**全批所有实物到目标的一对一去向**（每项
  `{"physical_id", "target"}`，按 `physical_id` 排序），不是孤立的局部配对；全批无新印纸
  时为空数组。合格证据里的去向让该册全部新印位置合格。
- `mismatch`：不合格证据再标出一处具体不符：`physical_id`、`target`、`side`
  （`front`/`back`）、`slot_index`（该面从左到右的页位序号，0 起），以及该页位的
  `expected` / `actual`（各为 `{"logical_page", "placement"}`，`logical_page` 含修订号或
  为 `null`）。
- `deficiency`（霍尔缺口证据）：在**仅剔除该册不合格配对、保留别册全部候选**的条件下，
  给出一组目标 `targets` 及仍可去往这组目标的全部实物 `sheets`；实物严格少于目标，因此
  任何调换都凑不出该册的好书。

没有完整解释时，输出单独的日志冲突诊断，不发布任何版次的质量结论：

```json
{
  "schema_version": 3,
  "logistics": "conflict",
  "conflict": {"deficiency": {"targets": [ ... ], "sheets": [ ... ]}},
  "summary": {"logistics": "conflict", "physical_sheets": 4, "target_positions": 4}
}
```

缺口证据形式相同，但这次使用**未经质量过滤**的物流候选。合法但互相矛盾的物流是正常审计
结果：生成诊断并退出 0。

## 确定性与算法

同一输入交换实物行或候选列表的顺序后，报告内容、列表和选出的证据完全稳定：实物按
`physical_id` 排序、候选按工单位置序规范化后，所有匹配与证据选择都是确定的（证据取字典
序最小的一对一去向；多个合法证据本就无需固定某种最优者）。判定只用增广路匹配与交错图强
连通分量（可达配对 = 出现在某种完美匹配中的边），复杂度为多项式；**从不枚举**全部装订
解释，也不截取前若干种。

## 错误与退出码

未知引用、引用到旧帖、数量不符、非法结构（字段缺失/多余、候选为空或超过 16、候选重复、
实物 ID 重复或不合规则、引用编号非正整数等）都是输入错误，退出 2 且不发布报告；写入失败
（含目标已存在）同样退出 2 且不留半份文件。合法但互相矛盾的物流退出 0 并给出冲突诊断。

## 用法

```sh
# v3 装订审计
signature-press --binding-audit --source fixtures/binding-audit.json --output run/audit.json
```

库入口：

```python
from signature_press.audit import process_audit  # v3，输入 dict，返回 schema_version=3 的 dict
```
