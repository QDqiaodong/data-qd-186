# 数字印刷缓存换版 v5（离线清单，schema_version 5）

`--cache-rollout` 与既有四个模式互斥。它只计算一份可照单执行的换版清单：不下载文件、
不操作设备，也不改变既有拼版、切帖或实物领用结果。每个快照内嵌完整的 v2 `proof_batch`，
按现有 v2 逻辑得到固定工单；一次切换的单位是整个批次，批内两册不能只切一册。

## 输入契约

严格 JSON，顶层恰好三个字段：

```json
{
  "capacity_bytes": 1000,
  "batches": [
    {
      "batch_id": "A",
      "current": {"proof_batch": { ... v2 输入 ... }, "page_assets": [ ... ]},
      "next":    {"proof_batch": { ... v2 输入 ... }, "page_assets": [ ... ]}
    }
  ],
  "assets": [
    {"asset_id": "p", "size_bytes": 200, "full_available": true,
     "patches": [{"base_asset_id": "b", "wire_bytes": 50}]}
  ]
}
```

- `capacity_bytes`：1..10¹² 的整数。
- `batches`：1..12 个待换版批次，`batch_id` 用仓库标识符规则且全局唯一。
- 每个快照的 `proof_batch` 是完整 v2 输入，沿用全部 v2 校验；`page_assets` 每行恰好是
  `{section_id, page, revision, asset_id}` 四个字段。
- 快照清单必须对该工单**所有新印帖**（`source == "fresh_print"`）中出现的非空逻辑页身份
  `(section_id, page, revision)` 各登记一次；同一身份在两册出现仍只登记一次。空白页位
  （`logical_page == null`）与仅出现在领用旧帖（`source == "retained"`）里的页不得登记；
  多登记、漏登记、重复身份映射一律是输入错误（退出 2）。
- 页身份只在本快照内有效：不得跨快照用页码或修订号推断同一文件。
- `assets` 是素材目录，`asset_id` 唯一；`size_bytes` 为 1..10¹² 整数，
  `full_available` 为布尔值。`patches` 每条为 `{base_asset_id, wire_bytes}`：
  - 基底必须在目录中存在且不能是目标自身；同一目标的同一基底最多登记一次；
  - `wire_bytes` 为正整数且严格小于该目标整包大小；
  - 允许没有任何可用来源（整包与差分皆空），历史素材可以只存在于控制机上。
- 所有 `page_assets.asset_id` 必须在目录中存在，否则退出 2。
- 开始时控制机恰好驻留所有当前工单依赖素材的并集；相同 `asset_id` 是同一份不可变文件，
  跨批次、跨页共用只占一份空间。初始占用超过容量属于输入与设备状态矛盾，退出 2。

## 换版步骤的精确语义

每一步选一个尚未切换的批次：

1. 保留所有仍在线工单引用的素材，**包括正在换版批次的旧工单**。
2. 新版需要但尚未驻留的每个素材各取一次，来源可以是：
   - 整包（`full_available` 为真时），下载字节等于 `size_bytes`；
   - 差分：基底在**这一步开始时**已经驻留，下载 `wire_bytes` 后生成目标。
   - 已驻留的目标零下载。这一步内刚生成的文件不能作为同一步任何差分的基底。
3. 传输是流式的：差分包不占磁盘配额；暂存期间所有原驻留素材与新增目标共存，峰值按
   **去重后的整文件总大小**检查，不按下载量检查；峰值必须 `<= capacity_bytes`。
4. 新版文件全部就位后整批切换，然后删除不被任何在线工单引用的文件。回收在下一步开始前
   完成；不允许抢先下载未来批次素材，也不允许特意保留无人引用的差分基底。

### 单目标来源选择

同一目标的可行来源取下载字节最小者；整包与差分不可能同价（差分包严格小于整包）。
多个同价差分按基底 ID 的 ASCII 字典序取最小者。

### 全局优化

枚举全部批次排列（子集动态规划，最多 12 批，4096 个状态），最小化**总下载字节**；
总下载打平时按批次 ID 序列的 ASCII 字典序取最小方案，而不是逐步贪心。

## 输出契约（成功，`"status": "complete"`）

原子新建、严格 JSON、拒绝覆盖任何已存在目标；非法输入或写入失败退出 2。顶层：

```json
{
  "schema_version": 5,
  "status": "complete",
  "capacity_bytes": 1100,
  "initial_occupancy_bytes": 700,
  "final_occupancy_bytes": 500,
  "total_download_bytes": 350,
  "steps": [
    {
      "step": 1,
      "batch_id": "A",
      "retained_assets": [{"asset_id": "b", "size_bytes": 300}],
      "acquisitions": [
        {"asset_id": "p", "size_bytes": 200, "acquisition": "patch",
         "base_asset_id": "b", "download_bytes": 50}
      ],
      "download_bytes": 50,
      "pre_switch_peak_bytes": 900,
      "reclaimed_assets": [{"asset_id": "a", "size_bytes": 100}],
      "post_reclaim_occupancy_bytes": 800
    }
  ],
  "snapshots": [
    {
      "batch_id": "A",
      "current": {"work_order": { ... 完整 v2 工单 ... },
                  "requirements": [
                    {"asset_id": "x", "size_bytes": 300,
                     "logical_pages": [
                       {"section_id": "SA", "page": 1, "revision": "r1",
                        "locations": [
                          {"edition_id": "edition-A-cur", "signature": 1,
                           "sheet": 1, "side": "front", "folio": 1}]}]}]},
      "next": {"work_order": { ... }, "requirements": [ ... ]}
    }
  ],
  "summary": {"status": "complete", "batches": 2, "steps": 2,
              "total_download_bytes": 350,
              "initial_occupancy_bytes": 700,
              "final_occupancy_bytes": 500}
}
```

- `retained_assets`：新版需要且本步开始时已驻留、零下载沿用的素材。
- `acquisitions`：本步新取素材，按 asset_id 排序。整包记录
  `"acquisition": "full"`；差分记录 `"acquisition": "patch"` 并带 `base_asset_id`。
- `pre_switch_peak_bytes`：切换前去重文件总大小的峰值；`reclaimed_assets` 是切换回收清单，
  `post_reclaim_occupancy_bytes` 是回收后占用。
- `snapshots` 按 `batch_id` 字典序排列，与输入顺序无关。每个 `requirements` 条目给出素材
  及其每个逻辑页身份在 v2 工单新印帖中的具体位置（edition / signature / sheet / side /
  folio），用于核对需求确实来自工单页位而不是直接把目录当驻留清单。

## 输出契约（业务阻塞，`"status": "blocked"`，退出 0）

合法输入但不存在完整路线时不发布任何可执行步骤，改为给出可达的最长切换前缀：

```json
{
  "schema_version": 5,
  "status": "blocked",
  "capacity_bytes": 1000,
  "reachable_prefix": ["A"],
  "remaining_blockers": [
    {"batch_id": "B",
     "peak_bytes": 1100,
     "over_capacity": true,
     "unavailable_targets": ["q"],
     "snapshot_requirements": [ ... 同成功报告的 next requirements ... ]}
  ],
  "summary": {"status": "blocked", "switched_batches": 1,
              "remaining_batches": 1, "initial_occupancy_bytes": 700}
}
```

- 前缀先按可达状态中已切换批次数最多选取，再按批次 ID 序列字典序最小决胜；仅列批次 ID。
- 对该状态下每个剩余批次：
  - `peak_bytes` 是**假如该批所有新版目标都到齐**后的去重峰值（无来源目标也按整文件大小
    计入），`over_capacity` 表示是否超容量；
  - `unavailable_targets` 列出在该步开始驻留集合下既无整包、又无合法差分基底的目标。
  - 两个阻碍相互独立，可以同时出现。

## 顺序无关性与失败约定

- 交换批次、素材目录、页映射行或差分列表的记录顺序不改变报告：快照按 batch_id 排序输出，
  素材与清单按 asset_id、身份按三元组、位置按工单坐标规范化排序；v2 版次顺序作为工单语义
  保持不变。
- 成功与业务阻塞都退出 0；非法结构、无效工单、重复或缺失映射、未知引用、初始超容量或
  写出失败退出 2，且不留下半份输出文件。

## 用法

```sh
# v5 缓存换版
signature-press --cache-rollout \
  --source fixtures/cache-rollout.json --output run/rollout.json
```

库入口：

```python
from signature_press.rollout import process_rollout  # 输入 dict，返回 schema_version=5 的 dict
```
