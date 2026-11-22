# 书帖工单 v6 · 纸张批号召回（离线清单，schema_version 6）

`--lot-recall` 与既有五个模式互斥。它只从同一份离线台账生成三份操作清单和可追溯依据：
不修改 v2 工单、不改拼版/用料/出库记录，也不向外发送任何通知。四类数组各自最多
20,000 行，输入沿用 8 MB 上限。

## 输入契约

严格 JSON，顶层恰好七个字段：

```json
{
  "proof_batch": { ... 完整 v2 输入（editions / legacy_lots）... },
  "copies":      [{"edition_id": "A", "count": 16}],
  "print_lots":  [{"lot_id": "L", "edition_id": "A", "signature": 1, "count": 18}],
  "bind_spans":  [{"lot_id": "L", "first_piece": 8, "first_copy": 1,
                   "count": 8, "piece_step": -1}],
  "pack_spans":  [{"edition_id": "A", "first_copy": 16, "copy_step": -1,
                   "carton_series": "C", "first_carton": 100, "carton_count": 4,
                   "books_per_carton": 4}],
  "dispatches":  [{"carton_series": "C", "first_carton": 103, "last_carton": 103,
                   "consignee_id": "D"}],
  "alerts":      [{"lot_id": "L", "first_piece": 6, "last_piece": 7}]
}
```

- `proof_batch` 先走现有 v2 规划器（`batch.process_batch`）得到**执行工单**。版次 ID、
  当前帖号与 `fresh_print` / `retained` 来源都以该工单为准；召回数据不得改变工单。
- 全部流水码与数量（`count`、`first_piece`、`first_copy`、`first_carton`、
  `carton_count`、`books_per_carton`、`last_piece`、`last_carton` 等）都是 1–10⁹ 的整数；
  由起点、步长与数量推导出的每个码也必须落在合法域内。字符串 ID 沿用仓库规则
  `[A-Za-z0-9_:-]{1,64}`。

### `copies`

每个执行工单版次恰好一行 `{edition_id, count}`；册号是该版次内的 1 到 `count`。
工单若为该版领用了任何一件旧帖，则这一版只能有**一册**（旧帖已经装入），且旧帖不得在
`print_lots` 或 `bind_spans` 中登记。召回只涉及本次新印帖；旧帖与补白不产生额外待配或
待召回件。

### `print_lots`

每行 `{lot_id, edition_id, signature, count}`。`lot_id` 唯一；所引帖号必须是该版次的
`fresh_print` 帖号。实物流水码是该印刷批内的 1 到 `count`，一件指一份完整书帖。
可有多个印刷批给同一帖号供货（用多行不同 `lot_id` 表达），也可留下尚未领出的实物。

### `bind_spans`

每行 `{lot_id, first_piece, first_copy, count, piece_step}`。从 `first_copy` 起按册号
递增，每册从该 lot 取一件，实物码从 `first_piece` 起按 `piece_step`（只准 `+1`/`-1`）
递推；版次与帖号取自 lot。**完整校验所有装订记录**：

- 每版每册在每个新印帖号上必须恰好有一件来源；同一 lot 的同一实物码不得用两次；
- 两个不同帖号的来源可以同时影响同一册；
- 任一跨度越过 lot 实际流水码或版次册号范围都是输入错误。

装订是倒着从一摞书帖末端取料的，所以 `piece_step` 常为 `-1`，册号越大实物码越小；
这与方向无关，仅决定映射。

### `pack_spans`

每行 `{edition_id, first_copy, copy_step, carton_series, first_carton, carton_count,
books_per_carton}`。按箱号从 `first_carton` 递增，每只箱放 `books_per_carton` 册；
整行装入的册号从 `first_copy` 起，每放一册走 `copy_step`（仅 `+1`/`-1`），下一只箱
接着走、不重置。要求：

- 每版册号必须恰好全量装箱一次；
- 同一 `carton_series` 内箱号全批唯一，允许有间隔；
- 不同包装行可用同一箱号系列，但任何一只箱只能由一行定义。

装箱也可能倒序（`copy_step: -1`），同样只改变映射。

### `dispatches`

每行 `{carton_series, first_carton, last_carton, consignee_id}`，表示这个连续区间的箱
已**整箱**发给该收货方。每个箱必须实际存在、不能跨不存在的箱号；可横跨相邻包装行。
一只箱不可出现在两行出库记录里，即使收货方相同。其余箱在厂。

### `alerts`

每行 `{lot_id, first_piece, last_piece}`，是同一张通知中的受影响实物范围。范围可重复、
相邻或相交，按**集合**处理；区间下界不可大于上界，不能越过该批实际流水码。允许空通知，
但仍须校验整份台账。

## 结果（`schema_version: 6`）

原子新建、拒绝覆盖任何已存在目标（含符号链接）；任何非法输入、坏引用、覆盖冲突、
缺配、缺箱或写入失败均退出 2 且不留下半份文件。报告全程用**区间集合**表示，不按册
或按箱展开，因此 10⁹ 册 / 10⁸ 箱与十几个册的开销与报告体积同阶。顶层：

```json
{
  "schema_version": 6,
  "editions": [
    {
      "edition_id": "A",
      "copy_count": 16,
      "affected_copies": [{"first": 2, "last": 4}, {"first": 15, "last": 16}],
      "in_factory_affected": [{"first": 15, "last": 16}],
      "dispatched_affected": [{"first": 2, "last": 4}],
      "held_good_copies": [{"first": 13, "last": 14}],
      "incoming_evidence": [
        {"edition_id": "A", "signature": 1, "lot_id": "L",
         "copy_first": 2, "copy_last": 3, "piece_first": 6, "piece_last": 7,
         "piece_step": -1},
        {"edition_id": "A", "signature": 1, "lot_id": "L",
         "copy_first": 15, "copy_last": 16, "piece_first": 15, "piece_last": 16,
         "piece_step": 1},
        {"edition_id": "A", "signature": 2, "lot_id": "M",
         "copy_first": 3, "copy_last": 4, "piece_first": 3, "piece_last": 4,
         "piece_step": 1}
      ]
    }
  ],
  "unbound_pieces": [
    {"lot_id": "L", "edition_id": "A", "signature": 1,
     "pieces": [{"first_piece": 17, "last_piece": 18}]}
  ],
  "held_cartons": [
    {"carton_series": "C", "ranges": [{"first_carton": 100, "last_carton": 100}]}
  ],
  "consignees": [
    {"consignee_id": "D",
     "cartons": [{"carton_series": "C",
                  "ranges": [{"first_carton": 103, "last_carton": 103}]}],
     "editions": [{"edition_id": "A",
                   "recall_copies": [{"first": 2, "last": 4}]}]}
  ],
  "summary": {
    "affected_copies": 5,
    "in_factory_affected_copies": 2,
    "dispatched_affected_copies": 3,
    "held_good_copies": 2,
    "held_cartons": 1,
    "recalled_copies": 3,
    "unbound_pieces": 2,
    "consignees": 1
  }
}
```

字段说明：

- `editions[].affected_copies`：**真正受影响**的册号集合（多帖影响同一册只计一次）。
  与 `in_factory_affected` 和 `dispatched_affected` 的并集完全相同，二者互不相交。
- `editions[].incoming_evidence`：足以核对来料溯源的区间记录，每条指明版次、当前帖号、
  印刷批、册号区间及对应的实物区间和方向（`piece_step`）。某册若受两个帖号影响，册号只
  计一次，但两条来料依据都保留。
- `unbound_pieces`：未装订（尚未进入装订线）的受影响书帖，按印刷批给出实物码范围；
  它们是**扣件**，不计入成书受影响的册数。完全未启用的批只要通知覆盖也会出现。
- `held_cartons`：在厂箱中，只要一册受影响便整箱扣留的箱号集合，按箱号系列给出。
- `editions[].held_good_copies`：在这些被扣箱里因整箱扣留而**搭扣**的好书册号；它们既不
  写入 `affected_copies`，也不会导致别的箱被冻结。
- `consignees`：按收货方给出用于定位的**发出箱号**，以及按版次列出的真正应召回册号；
  同箱好书不列入召回。箱号只出现在实际发给该收货方的记录里。
- `summary`：受影响册、在厂/已发拆分、搭扣册、在厂扣箱、各收货方召回册及未装订扣件的
  去重计数。校验 `affected = in_factory + dispatched` 与
  `recalled = dispatched`；不成立即视为内部错误并拒绝发布。

集合按各自标识和连续区间输出，同一集合内相邻区间合并，不跨版次、批号、箱号系列或
收货方合并。台账行重排（不改 `proof_batch` 版次顺序）或把通知改为等价的重叠区间，
结果都不变。

## 大用例

单帖版 10⁹ 册、同一印刷批倒着配、从册 1 正向装箱、箱 75000001–100000000 已发同一
收货方时，报告体积约 1 KB，运行时间为毫秒级；新增模式的 CLI 可在 Linux 上 256 MiB
进程地址空间限制（`ulimit -v 262144`）下跑完。

## 用法

```sh
signature-press --lot-recall --source fixtures/lot-recall.json --output run/recall.json
```

库入口：

```python
from signature_press.recall import process_recall
report = process_recall(raw_dict)   # schema_version == 6
```
