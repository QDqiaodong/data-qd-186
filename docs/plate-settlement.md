# 书帖工单 v4 · 离线制版费结算（账项重放）

v4 在同一 CLI 中以显式 `--plate-settlement` 开关启用，与 `--proof-batch`、`--binding-audit`
互斥（同时给出任意两个开关由 argparse 拒绝并退出 2）；不带开关时 v1 行为不变，v2/v3 的
入口与输出也完全不变（见 [v1 契约](contract.md)、[打样批契约](proof-batch.md) 与
[装订审计契约](binding-audit.md)）。本模式只做离线核算：不接触打印机、外网，不核算纸钱、
印数或装订质量，也不会为降低费用而改变任何拼版工单。

## 输入

源文件是严格 JSON，顶层恰好两个字段：

```json
{
  "orders": [
    {"order_id": "A", "proof_batch": { ... 完整 v2 规划输入（editions / legacy_lots）... }}
  ],
  "events": [ ... 0–2048 条按数组顺序入账的记录 ... ]
}
```

- `orders`：1–32 项，每项 `{order_id, proof_batch}`。`order_id` 沿用仓库标识符规则
  （`[A-Za-z0-9_:-]{1,64}`）且批内唯一。每项独立调用现有 v2 规划器
  （`batch.process_batch`）得到**执行工单**；不同工单各自使用自己的 v2 输入，旧帖身份
  不跨工单共享。任一工单本身非法即整体输入错误。
- `events`：0–2048 条记录，**严格按数组顺序入账，不按 ID 排序**。每条有唯一 `event_id`
  （同一标识符命名空间）和 `type`。中途任何一条非法都退出 2，不发布任何结算结果，后续
  事件不得掩盖中途错误。

### 计费面

一个计费面用 `{order_id, edition_id, signature, sheet, side}` 引用，后三项取**当前工单**
的帖号、帖内张号（均从 1 起）与 `front`/`back`。

- 只有 `fresh_print` 新印帖中的一面、且该面**至少有一个非空逻辑页**，才可计制版费。
- 指向 `retained` 旧帖的引用、指向不存在位置的引用、指向**全白面**（两面所有页位皆为
  `null` 中的那一面）的引用都是输入错误，而不是“未知面”。
- 计费面可以尚未出现在供应商来账中；任何当前计费面在同一时刻至多被一个账项认领一次。

### 共同账项与同版核对

供应商在同一账项（pool）的 `uses` 里声明多个计费面共用一张物理版。系统对同一账项下的
各面做同版核对，全部满足才合法：

1. 纸张宽高（`sheet_width_um` / `sheet_height_um`）相同；
2. 该面从左到右的每个页位上，带修订号的逻辑页（含 `null` 空位）与完整 `placement`
   页框（x/y/宽/高/旋转角）逐项相同。

面名（front/back）、帖号、纸张号与 `folio` 都**不**参与同版判断。只是印面内容相同而
没有供应商的共同账项，系统绝不擅自合并收费；把不能共版的面放进同一账项是输入错误。

### `revise` 事件（来账与更正）

```json
{"event_id": "ev-1", "type": "revise",
 "pools": [{"pool_id": "X", "total_fen": 101, "uses": [ ... 计费面引用 ... ]}]}
```

- `pools` 含一个或多个**互不相同**的账项全量记录；同一事件内 `pool_id` 不得重复。
- `total_fen` 是 0 至 10¹² 的整数分；`uses` 是该账项当前承担制版费的计费面引用列表。
- 同一账项首次出现为**第 1 版**，之后每次在 `revise` 中出现版本号加一；本事件未提及的
  账项保持不变。
- 非空 `uses` 允许零费用（新开立零费用账项合法）。已存在账项可以用 `total_fen: 0` 与
  空 `uses` **撤销**：身份与版本号保留（输出中 `revoked: true` 可辨认），之后可以用同一
  `pool_id` 重新开立，版本号继续递增。其他空列表（新账项即空、或空列表带非零费用）非法。
- 同一事件可以把面从一个账项转入另一个账项，或互换两个账项里的面。占用校验针对
  **整批替换完成后**的状态，而不是按 `pools` 数组顺序逐条转移：替换结束后任一计费面仍被
  重复认领，即使后续事件会纠正，也拒绝整个输入。

### `close` 事件（财务结账）

```json
{"event_id": "ev-2", "type": "close", "statement_id": "st-a1", "order_id": "A"}
```

- 只给指定工单出一张结算单；同一工单可以多次结算，`statement_id` 全局不得重用。
- 按该事件**所在时点**的完整来账核算，看不见后面的更正。
- 结算单逐账项列出“该工单此刻的应付”减“此前已为该工单在该账项上结过的带符号金额总和”：
  正数补收、负数冲回。只省略差额为零的账项行；**不能把不同账项轧差后再决定是否出行**；
  没有差额也要留下一张空结算单（`lines: []`，带符号合计为 0）。
- 结算后这些差额计入该工单的已结金额，其他工单的已结金额不动。之后的事件可以改变应付
  与待结金额，但**不能修改已有结算单的任何字段**。

## 分摊（精确整数，无浮点）

对每个当前有效账项，按各工单在该账项中的计费面数占总面数的比例分摊 `total_fen`：

1. 每工单先取精确份额（`Fraction`）的整数部分；
2. 剩余分钱按精确余数从大到小各补一分；余数相同时按 `order_id` 的 ASCII 升序。

- 撤销后的账项每个工单当前应付均为 0；某工单退出仍有效的账项后，该账项对它的当前应付
  也为 0（历史已结不变，因而以负数待结差额表现为待冲回）。
- 更正后用完整的新参与关系**重算整个受影响账项**，不只算转出或转入的工单。
- 每个账项各参与工单应付之和恒等于其供应商账面金额，输出前内置守恒校验，失败则不发布。

## 输出（`schema_version: 4`）

原子新建、严格 JSON、拒绝覆盖任何已存在的目标（含符号链接）；任何非法输入或写入失败均
退出 2 且不留半份文件。顶层：

```json
{
  "schema_version": 4,
  "orders": ["A", "B"],
  "pools": [
    {
      "pool_id": "X",
      "version": 2,
      "total_fen": 101,
      "revoked": false,
      "uses": [ {"order_id": "A", "edition_id": "ea-a1", "signature": 1,
                 "sheet": 1, "side": "front"} ],
      "participants": [
        {"order_id": "A", "face_count": 1, "payable_fen": 51},
        {"order_id": "B", "face_count": 1, "payable_fen": 50}
      ]
    },
    {"pool_id": "Z", "version": 2, "total_fen": 0, "revoked": true,
     "uses": [], "participants": []}
  ],
  "statements": [
    {
      "statement_id": "st-a1", "order_id": "A", "event_id": "ev-2",
      "signed_total_fen": 67,
      "lines": [
        {"pool_id": "X", "pool_version": 1, "previously_settled_fen": 0,
         "payable_fen": 67, "difference_fen": 67}
      ]
    }
  ],
  "outstanding": [
    {"order_id": "A", "pools": [
      {"pool_id": "X", "payable_fen": 0, "settled_fen": 51,
       "open_difference_fen": -51}
    ]}
  ],
  "summary": {"orders": 2, "pools": 3, "active_pools": 2,
              "revoked_pools": 1, "statements": 8}
}
```

- `pools`：按 `pool_id` 排序，逐账项给出当前版本、金额、`revoked` 标志、有效计费面
  （按引用五元组排序）及各**参与**工单的面数与当前应付。撤销账项以 `revoked: true`、
  空 `uses` 与空 `participants` 辨认，但仍保留身份与版本号。
- `statements`：按结账发生顺序列出。每张含结算单 ID、工单、触发事件 ID、带符号合计，
  以及逐账项行（账项 ID、**结账时的账项版本**、此前已结、当时应付、本次差额）。空结算单
  保留为 `lines: []`。
- `outstanding`：逐工单（按 `orders` 输入顺序）逐账项（按 `pool_id` 排序）给出当前应付、
  历史已结与未结差额 `open_difference_fen = payable - settled`。已退出账项但仍有待冲回
  款项的关系不会消失；与该工单既无参与、又无历史、也无差额的账项省略。
- 守恒承诺：逐工单逐账项恒有 `settled_fen + open_difference_fen == payable_fen`；逐账项
  各工单应付之和恒等于 `total_fen`。

## 确定性与重放

- 固定 `events` 顺序时，交换 `orders` 清单、同一 `revise` 的账项清单或任一账项的计费面
  清单顺序，结果完全不变：所有输出列表按明确键排序，分摊决胜只看余数与 ASCII `order_id`。
- 在原日志之后补事件重跑时，原有结算单**逐张保持完全一致**（结账只快照当时的标量值）。
  把日志截断到某次结账单独重放，所得该张结算单与完整日志中的同一张逐字段相等。

## 用法

```sh
# v4 离线制版费结算
signature-press --plate-settlement \
  --source fixtures/plate-settlement.json --output run/plate-settlement.json

# 离线重放核对：把日志截断或在尾部追加更正后写到新源文件，换一个新输出路径再跑一次；
# 已有输出路径不可覆盖。对比两次报告的 statements，旧单必须逐字节一致。
```

库入口：

```python
from signature_press.settlement import process_settlement  # 输入 dict，返回 schema_version=4 的 dict
```
