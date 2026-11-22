# 书帖工单 v7 · 供纸计数班单（离线续算，schema_version 7）

`--press-meter` 与前六个模式互斥。印厂的供纸计数器只导出**间歇读数**，报的是会回卷的
累计值；机长的上机记录又常比读数晚到。本模式把这两路增量证据离线归集成**每班一张**的
用纸单：按班次、机台和 `{edition_id, signature}`（新印帖号）汇总**实际送入机台的纸张**
（含试印和废张）。跨过换单或交班的读数差额**不按时间比例摊**；证据不能唯一确定用量时
一律暂缓出单（待核验），绝不用未封存的行给出正式用量区间。

一条账链**一次初始化、多次接续**：首包给初始化参数，后续每包只带上一份产物的续算状态
和本次新增数据，不再提交初始化参数或以前的导出包；已归档的原始流水不用重取，已出过的
班单不会重开。运行只用标准库。

## 包格式（首包与接续包）

每次调用的源文件是严格 JSON，顶层只允许 `init` / `state` / `watermarks` / `readings` /
`mountings` 五个字段，其中 `init` 与 `state` **恰好出现一个**，其余三个可缺省（视为空）。

### 首包：`init` + 新增数据

```json
{
  "init": {
    "proof_batch": {"editions": [ ... ], "legacy_lots": [ ... ]},
    "shift_boundaries": [0, 10, 20],
    "machines": [
      {"id": "press-1", "max_sheets_per_second": 1, "modulus": 1000,
       "initial_reading": 100}
    ]
  },
  "watermarks": [{"machine": "press-1", "readings": 5, "mountings": 20}],
  "readings": [{"machine": "press-1", "second": 15, "value": 111}],
  "mountings": [{"machine": "press-1", "start": 0, "end": 8,
                 "edition_id": "edition-ji", "signature": 1}]
}
```

- `proof_batch`：完整 v2 输入，先走现有 v2 规划器（`batch.process_batch`）得到**执行工单**。
  归集键只认工单里的 `{edition_id, signature}` 且 `source == "fresh_print"` 的当前帖号；
  旧帖（`retained`）与不存在的帖号一律拒绝。工单不被修改，计划张数不用于截断实耗。
- `shift_boundaries`：2–65 个整数秒，从 0 开始严格递增，末界为 T（≤ 10⁹）。相邻两界构成
  1–64 个首尾相接的半开班次 `[s, e)`。
- `machines`：1–8 台值班机台。`id` 沿用仓库标识符规则；`max_sheets_per_second` 为每秒最多
  送入的整数张数（1–10000）；`modulus` 为计数器模数（2–10⁹）；`initial_reading` 是 0 秒的
  初始读数（0 到模数减一，不计入流水条数）。

### 接续包：`state` + 新增数据

```json
{
  "state": { ... 上一份产物的 continuation，可经 JSON 存取 ... },
  "watermarks": [{"machine": "press-1", "readings": 16}],
  "readings": [{"machine": "press-1", "second": 5, "value": 103}]
}
```

`state` 就是上一份产物里的 `continuation` 字段（可单独取用、另存）；不允许再提交
`init` 或以前的导出包。状态本身按未知引用、越界、重复、交叠、非法倒灌同样严格校验。

## 两路数据与水位

每台机的两路数据各有自己的**完整水位**，初值 0，每包可以不变或上升，没更新的机台和
数据路沿用原值：

- `readings`（读数水位，最大 T+1）：保证所有时间戳**严格小于**该值的读数已经到齐。
- `mountings`（上机水位，最大 T）：保证起点**严格小于**该值的上机记录已经到齐。

水位**不是**已见最大时间：不能因收到较晚的一行便提前结班。新增行可无序、可先越过水位
（以后水位再追上），但不得落到本路**上一次**水位之前（非法倒灌）；记录不可覆盖或撤回。

- `readings[]`：`{machine, second, value}`。按机台与整数秒唯一；`second` 为 1…T，
  `value` 为 0 到模数减一。相邻读数 `(a,x),(b,y)` 只说明 `[a,b)` 内实耗非负且
  `≡ y−x (mod modulus)`；允许零张、一次或多次回卷。
- `mountings[]`：`{machine, start, end, edition_id, signature}`。半开区间 `[start,end)`
  非空且在 0…T 内；同机不得交叠，允许首尾相接；同一帖可重复上机或在不同机台同时印。
  上机记录未覆盖的秒，在上机水位以内才确定为空机（空机实耗为零）；上机时每秒可送 0 到
  机台上限的任意整数张。读数不提供秒内或逐张定位。
- 每包 `readings` + `mountings` 合计最多 2000 条；整条链合计最多 20000 条（不含机器
  初始读数）。整数不能用布尔值代替。

完整检查未知引用、越界、重复、交叠与非法倒灌，**包括暂未用于结班的行**。对两路证据均
已完整的任一相邻读数段（右端读数严格小于读数水位、且上机水位至少到该右端），若段余数
超过上机限速下的最大可送张数（含回卷条件后仍不存在合理实耗），属于**输入矛盾**，整包
拒绝、退出 2——即使该段不在当前待结班。

## 结班规则

只出从上次截止点开始**连续**的一段班单，按班序逐个判定；遇到首个尚不能出单的班就停，
不跳过，也不妨碍接收本次合法数据。对当前班 `[s, e)`，每台机：

1. 至少要有一条落在班末时刻或之后的读数；用**实际相邻读数段**覆盖整班（含跨出班末的
   段），不在班界虚造读数。首个段从 ≤ s 的最晚读数（或跨截止点段的左端）起，末个段的
   右端 ≥ e。
2. 每个涉及的读数段都已完整：右端读数严格小于读数水位，上机水位至少到该右端。

没满足就报告该班尚缺哪台机的哪路完整性（`incomplete` 诊断，缺右端读数与水位未封分开
表达）。证据齐了之后，在所有满足这些完整读数段的可行逐秒用纸情形中，求班内各
`(机台, 版次, 当前帖号)` 汇总实耗的最小、最大值。读数段之间互相独立，逐段求界后相加；
段内某键的班内张数 `x` 满足

```
min_in(K) = max(0, lo − (cap − inside_cap(K)))
max_in(K) = min(inside_cap(K), hi)
```

其中 `lo`/`hi` 是该段可行总量的最小、最大值（`lo = (y−x) mod modulus`，
`hi = lo + modulus·⌊(cap − lo)/modulus⌋`，`cap = 每秒上限 × 段内上机秒数`），
`inside_cap(K) = 每秒上限 × K 在班内的上机秒数`。总量约束把段内班内班外耦合在一起，
所以同一笔纸不会又全算给这一班、又全算给另一班；同一帖反复上机仍是一个归集键，不要求
每个上机小段的耗量都能唯一定位。

- **所有键的两个端点都相等**才出单。
- 完整但不能唯一归集则报告**待核验**（`ambiguous` 诊断）：给该班各键的精确上下界，并
  注明导致差异的机台、读数段端点和受影响的归集键。
- 已经归档的那部分实耗如果曾与当前班共用一个读数段，仍必须纳入该段的约束（续算状态里
  的 `archived`），不能重新拆给当前班。

## 产物（artifact）

每次调用生成一个有版本号的产物，原子新建、拒绝覆盖；班单、诊断与续算状态**同一份
文件**交付，不会只发布班单或只更新续算状态：

```json
{
  "schema_version": 7,
  "artifact_sequence": 2,
  "new_slips": [ ... 本次新出的班单，按班序 ... ],
  "diagnosis": { ... 首个待结班的诊断；无则为 null ... },
  "continuation": { ... 可单独取用的续算状态 ... },
  "summary": {"artifact_sequence": 2, "new_slips": 1, "issued_shifts_total": 1,
              "shifts_total": 2, "diagnosis": "none", "sheets_this_call": 6}
}
```

### 班单（仓管另存）

```json
{
  "shift_index": 0, "shift_start": 0, "shift_end": 10,
  "machines": [
    {"machine": "press-1", "max_sheets_per_second": 1, "modulus": 1000,
     "keys": [{"edition_id": "edition-ji", "signature": 1, "sheets": 6}],
     "machine_total": 6,
     "segments": [
       {"left_reading": [5, 103], "right_reading": [15, 111],
        "residue": 8, "capacity": 8, "total_min": 8, "total_max": 8,
        "archived_before": 0,
        "contributions": [{"edition_id": "edition-ji", "signature": 1, "sheets": 3}],
        "in_shift_sheets": 3,
        "carried_forward": {"min_sheets": 5, "max_sheets": 5}}
     ]}
  ],
  "total_sheets": 6,
  "verification_note": "..."
}
```

- 班界、各键实耗、机台合计与全班合计都在；`segments` 逐条相邻读数段给出它给每个键
  贡献了多少张，跨班段用**原始端点**作依据。
- `archived_before`：该段已在更早班单归档的张数；`carried_forward`：该段留给更晚班段
  的可行区间。`archived_before + in_shift_sheets + carried_forward` 分解该段总量。
- 空机台（班内无上机）也出现在班单里，`keys` 为空、合计为 0。

**如何用原始记录核对**（不需要逐秒出证）：对每条读数段，取计数器原始导出里的
`left_reading`/`right_reading`，`(右值 − 左值) mod modulus == residue`；
`capacity == max_sheets_per_second × 段内上机秒数`（上机记录与读数段求交，空机秒不
计）；段总量落在 `[total_min, total_max]` 且与 `residue` 同余。键实耗等于各段
`contributions` 中该键之和；跨班段的 `archived_before` 可在更早班单上找到对应
`carried_forward` 与 `in_shift_sheets`，三段合起来与段总量守恒。

### 诊断

- `{"kind": "incomplete", ...}`：`missing` 逐条给出机台、数据路
  （`readings`/`mountings`）与原因：`missing_right_reading`（缺班末或之后的读数，附
  当前最晚读数）、`readings_not_sealed`（读数水位须超过 `segment_right`）、
  `mountings_not_sealed`（上机水位须至少到 `segment_right`）。
- `{"kind": "ambiguous", ...}`：`keys` 给该班全部键的精确上下界；`causes` 按机台与
  读数段端点列出仍未定的键及其段内上下界。

合法待核验与缺数据都退出 0；非法输入（含矛盾读数、非法旧帖号、倒灌、坏状态）或写入
失败退出 2，且不留下半份文件。

## 续算状态与接续边界

`continuation` 是自包含的：链配置（班界、机台、工单认定的新印帖号）、`next_shift_index`、
累计行数、每机的两路水位、边界锚点（≤ 截止点的最晚读数）、跨截止点读数段（原端点、
整段上机秒数、已归档张数 `archived`），以及截止点之后的未结流水。已结区间里的读数与
上机原行不累积在状态里；过界的上机记录只保留仍有效的尾部与身份。已出的班单不保存在
状态中、也不会再次开出。

拿走旧导出包、旧产物和已结班单，仅凭最新状态继续，结果与用同一完整证据从头核算相同
（`artifact_sequence` 是各链自己的版本计数，随包数递增）。一个合法增量包内调换记录行
次序不改变输出。全部计算都是读数段与上机区间的区间运算，不按秒、张或可能的计数值展开：
T = 10⁹ 的长班与十秒班开销同阶。

## 用法

```sh
# 首包：初始化 + 第一批数据
signature-press --press-meter --source fixtures/press-meter.json --output run/meter-1.json
# 接续包：把 run/meter-1.json 的 continuation 作为 state，加上本次新增数据
signature-press --press-meter --source pkg-2.json --output run/meter-2.json
```

库入口：

```python
from signature_press.meter import process_meter
artifact = process_meter(package_dict)   # schema_version == 7
```
