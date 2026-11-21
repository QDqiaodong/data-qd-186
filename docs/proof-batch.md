# 书帖工单 v2 · 双版打样共用余帖（离线规划）

v2 在同一 CLI 中以显式 `--proof-batch` 开关启用；不带该开关时 `signature-press --source ... --output ...`
与库入口 `process(source)` 的 v1 行为完全不变（见 [v1 契约](contract.md)）。v2 只做离线计划，
不接触打印机、外网，也不修改实际库存。

## 输入

`--proof-batch` 的源文件是严格 JSON，顶层恰好两个字段：

```json
{
  "editions": [
    {"id": "ed-1", "source": { ... v1 工单结构 ... }, "revisions": ["r1", "r1"]}
  ],
  "legacy_lots": [
    {
      "id": "lot-1",
      "source": { ... v1 工单结构 ... },
      "revisions": ["r1"],
      "retained": [{"stock_id": "stock-1", "signature": 1}]
    }
  ]
}
```

- `editions`：1–2 个待校样版次。每个待出版次展开后 1–512 个逻辑页。
- `legacy_lots`：0–8 个 lot，全部 lot 合计最多 8 件留存实物；允许空数组。每个 lot 至少登记一件。
- 版次 ID、lot ID、stock_id 各自在自己的命名空间内唯一，沿用 v1 的 ASCII 标识符规则
  （`[A-Za-z0-9_:-]{1,64}`）。
- `source` 是完整的 v1 工单输入（sections / signature_pages / binding / geometry），
  其纸面页数上限等 v1 规则照旧。
- `revisions` 按该工单展开后的逻辑页顺序逐页给出一个**非空可打印 ASCII 标识符**
  （码点 0x21–0x7E，长度不限），数量必须恰好等于展开页数。
- 页身份是三元组 `(section_id, page, revision)`：三者全等才认作同一可印内容。版次 ID 与
  v1 全局页序都不能代替该身份。
- lot 按原 v1 固定切法还原其实际书帖；`retained[].signature` 是该 lot 内从 1 起的书帖号，
  同一 lot 内不得重复，且必须真实存在。未登记的帖不可用。
- 几何上不成立的旧工单、越界或重复的留存引用、修订数量不符等一律是输入错误（退出 2），
  即使该实物最终不会被领用也拒绝；合法但与任何新段不匹配的实物只是不安排领用。

## 切段与几何

- 每个版次按原章页顺序切成若干**非空连续段**，不要求落在章节边界；段覆盖全部逻辑页、无遗漏无重叠。
- `signature_pages` 在 v2 中是**单帖容量上限**：不再要求前面的帖装满，每段独立在段尾补空白到
  最小的 4 倍数，且补后 folio 数不超过容量。
- 每段用自己的实际帖长、装订方向和原纸面模型计算正背面、左右页位、嵌套深度与爬移，
  公式与 v1 完全一致（见 `impose.build_signature`、`geometry.place` 的同模型实现）。
- 不满足几何（例如最深处 `2 × 深度 × 纸厚 > gutter`）的切段直接不可用；规划器会改用其它切法，
  不会因为 v1 固定切法有爬移冲突就判整版无解。

## 领用与物理等价

每个可行段要么整帖新印，要么领用一件物理等价的留存旧帖；不允许拆旧帖、换单张或旋转后领用。
物理等价要求：

1. 装订方向相同，且全部 geometry 字段逐项相同；
2. 张数（= folio 数 / 4）相同；
3. 各嵌套层、各面（front/back）、各左右页位上的 folio、页框坐标，以及带修订号的逻辑页
   （含 `null` 空白）完全一致。

旧帖号与旧全局起点只用于输出溯源，不参与等价判断。同一件实物在整批中最多领用一次。

## 选优

按以下顺序最小化，靠前的目标严格优先：

1. 整批新印纸张数 `fresh_sheets`；
2. 成书空白页位总数 `blank_pages`（含旧帖自带空白）；
3. 成书书帖总数 `signatures`；
4. 仍平局时，按 `editions` 输入顺序，逐版比较成书顺序中的
   `(段末逻辑序号, 领用标志, stock_id)` 序列，字典序较小者胜。段末序号从 1 起且含端点；
   领用填 `(端点, 0, stock_id)`，新印填 `(端点, 1, "")`。

库存与 lot 的数组顺序不改变决策（决胜只看输入规定的标识与字典序）。
规划用带库存位掩码的动态规划一次性完成整批优化，不存在“先固定切点再看库存”的分步近似。

## 输出（`schema_version: 2`）

原子新建、严格 JSON、拒绝覆盖任何已存在的目标（含符号链接）；任何非法输入、配帖不一致或
写入失败均退出 2 且不留下半份文件。顶层：

```json
{
  "schema_version": 2,
  "editions": [
    {
      "id": "ed-1",
      "binding": "left",
      "geometry": { ... 原样回显 ... },
      "signatures": [
        {
          "signature": 1,
          "logical_start": 1,
          "logical_end": 8,
          "folio_count": 8,
          "blank_count": 0,
          "source": "fresh_print",
          "sheets": [
            {
              "sheet": 1,
              "nesting_depth": 0,
              "creep_um": 0,
              "sides": {
                "front": [
                  {"folio": 8, "logical_page": null,
                   "placement": {"x_um": 10000, "y_um": 10000, "width_um": 200000,
                                 "height_um": 280000, "rotation_degrees": 0}}
                ],
                "back": [ ... ]
              }
            }
          ]
        },
        {
          "signature": 2, "logical_start": 9, "logical_end": 12,
          "folio_count": 4, "blank_count": 0,
          "source": "retained",
          "stock_id": "stock-1", "lot_id": "lot-1", "legacy_signature": 1,
          "sheets": [ ... 实物原页位，结构同上 ... ]
        }
      ],
      "collation": [ {"section_id": "P", "page": 1, "revision": "r1"}, ... ],
      "collation_verified": true
    }
  ],
  "summary": {"fresh_sheets": 3, "blank_pages": 0, "signatures": 2},
  "optimization_verified": true
}
```

- 非空 `logical_page` 比 v1 多一个 `revision` 字段；空白仍为 `null`。
- `source` 为 `fresh_print` 时无溯源字段；为 `retained` 时给出 `stock_id`、`lot_id`、
  `legacy_signature`（旧帖号），并原样给出该实物的逐张双面页位与坐标。
- 每版的 `collation` 是按当前帖序与帖内 folio 从实际新印页位或实物原页位恢复的含修订号页序，
  并与源逐页核对；同时核对纸张守恒（`4 × 张数 = folio 数`）、真实页与空白页位守恒、
  段覆盖无遗漏。任何不一致都视为内部错误并拒绝发布。

## 用法

```sh
# v1（保持不变）
signature-press --source fixtures/input.json --output run/v1.json

# v2 打样批
signature-press --proof-batch --source fixtures/proof-batch.json --output run/proof.json
```

库入口：

```python
from signature_press.service import process      # v1
from signature_press.batch import process_batch  # v2，输入 dict，返回 schema_version=2 的 dict
```
