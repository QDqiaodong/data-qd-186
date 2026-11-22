# data-qd-186 · 书帖拼版与装订工单

将章节逻辑页映射到逐帖、逐纸双面页位；装订方向和嵌套深度决定实际放置坐标与爬移限制，最后按folio复核配帖页序。

impose.py 分配物理页，geometry.py 计算纸面位置，service.py 独立恢复配帖页序。 完整字段、单位、身份与失败约定见 [数据契约](docs/contract.md)。样例是作者自制合成数据，许可见 [来源说明](fixtures/SOURCES.md)。

## 安装、测试与演示

Python >=3.11，本批实际使用本机 CPython3.12.14。运行与测试仅用标准库；构建工具精确锁定。每仓独立 .venv，公共包缓存可共享；不共享其他项目的site-packages。

下面命令已在本机已有uv、Python与公共缓存条件下离线执行通过。其他机器如未准备缓存，需先准备构建包；不保证首次安装可以离线完成。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/signature-press --source fixtures/input.json --output run/report-initial.json
```

实际6项 v1 测试通过；加上 v2 打样批共 23 项测试通过；加上 v3 装订审计共 44 项测试通过；加上 v4 离线制版费结算共 77 项测试通过；加上 v5 缓存换版共 114 项测试通过；加上 v6 纸张批号召回共 134 项测试通过；加上 v7 供纸计数班单共 170 项测试通过。CLI 演示生成 JSON，v1 摘要为：

```json
{
  "blank_pages": 2,
  "collation_verified": true,
  "logical_pages": 10,
  "physical_sheets": 3,
  "signatures": 2
}
```

输出是原子新建；演示已生成过的同名路径不能重用，复跑请换一个新文件名。非法输入与文件写入错误退出2；业务诊断的表达方式见契约。已经实际核验重复JSON键拒绝、旧结果字节不变、已有符号链接不覆盖。

## v2 双版打样共用余帖

显式 `--proof-batch` 启用 schema 2 的离线规划，v1 入口与输出保持不变。该模式把 1–2 个待校样版次
各自切成非空连续段，每段可整帖新印或领用一件**物理等价**的留存旧帖（同一实物全批最多一次），
用带库存位掩码的动态规划一次性最小化：整批新印纸张数 → 成书空白页位 → 书帖数，再按契约规定的
`(段末逻辑序号, 领用标志, stock_id)` 字典序稳定决胜。身份是 `(section_id, page, revision)`
三元组；旧帖号与旧全局起点只作溯源。完整输入/输出字段、几何与等价规则见
[打样批契约](docs/proof-batch.md)。

```sh
.venv/bin/signature-press --proof-batch \
  --source fixtures/proof-batch.json --output run/proof-report.json
```

库入口为 `signature_press.batch.process_batch(dict)`。该夹具的最优解是甲版整印 12 页、乙版领用
唯一留存帖，整批新印 3 张纸；把乙版任一页修订号改掉后，规划器会把甲版重切成 1 / 8 / 3 并领用
中间帖、乙版改新印，整批新印 4 张。v2 同样原子新建、拒绝覆盖，非法输入退出 2 且不留半份工单。

## v3 装订后离线抽检

显式 `--binding-audit`（与 `--proof-batch` 互斥）对**已执行**的 v2 工单做装订后审计：输送线日志
只给出每张实物可能进入的几个装订位置，正背面版单记录仍在。审计先用纯物流证据求出所有完整装订
解释（完美匹配，绝不枚举），再按正背面页位内容逐位置判合格，逐版给出 `verified` / `defective` /
`uncertain`，并为每种存在的情形附全批一对一去向证据；`defective` 附霍尔缺口证据说明调换也凑不
出好书，物流自相矛盾时输出冲突诊断并退出 0。完整契约见 [装订审计契约](docs/binding-audit.md)。

```sh
.venv/bin/signature-press --binding-audit \
  --source fixtures/binding-audit.json --output run/audit-report.json
```

库入口为 `signature_press.audit.process_audit(dict)`。该夹具中甲册两个位置各自都能拿到正确纸、
合起来却是 `uncertain`（乙册的 y 位要占用甲的候选纸），乙册因 y 位无合格纸可配而 `defective`。
v3 同样原子新建、拒绝覆盖，非法输入退出 2。

## v4 离线制版费结算

显式 `--plate-settlement`（与 `--proof-batch`、`--binding-audit` 互斥）把多个打样工单的
供应商制版费来账与财务结账按事件顺序离线重放。每个 `{order_id, proof_batch}` 独立走现有
v2 规划得到执行工单；只有新印帖中含非空逻辑页的一面可计费，旧帖与全白面引用直接拒绝。
供应商在同一账项（pool）里声明多面共用一张版时，逐面核对纸张宽高、逐页位的带修订号逻辑页
（含空位）与完整页框；面名、帖号、纸张号、folio 不参与同版判断，印面相同却没有共同账项
也绝不合并收费。完整输入/输出字段、分摊与重放规则见
[制版费结算契约](docs/plate-settlement.md)。

```sh
.venv/bin/signature-press --plate-settlement \
  --source fixtures/plate-settlement.json --output run/plate-settlement.json
```

库入口为 `signature_press.settlement.process_settlement(dict)`。`revise` 事件全量替换账项
（版本号递增，可用 0 分空列表撤销后再重开），占用按整批替换后的状态校验；`close` 事件只快照
当时账面出单，后续更正永不改写旧单。分摊全程 `Fraction` 精确整数运算，按面数比例取整、余数
决胜按 ASCII `order_id`；退出账项或整账撤销以带符号负数在新单冲回，零差额留空单。夹具覆盖
验收主流程（101 分按 2:1 摊 67/34，更正后 A51/B50，同单冲回 16、补收 16，B 待结 50）、
退出/撤销冲回、空单、零费用账项、账项重开、同事件转移与置换、异修订/异纸型/旧帖/全白面拒绝、
最终重复认领拒绝及输出不可覆盖；把日志截断到首次结账单独重放，首张单与完整日志逐字段一致。

## 当前边界

不生成PDF或控制打印机；纸面坐标是各面正视的工作票约定。 实际后续需求可从插页和混合纸型、指定纸位重印及配帖操作沿既有身份和结果集成；当前未实现这些扩展，也不保证固定数量的未来任务。

## v5 数字印刷缓存换版

显式 `--cache-rollout`（与前四个模式互斥）为 1–12 个待换版批次离线计算缓存容量内的换版
清单。每个批次的 `current` / `next` 各内嵌一个完整 v2 `proof_batch` 与 `page_assets`
映射；规划器只承认 v2 工单新印帖里出现的非空逻辑页身份，领用旧帖与空白页位不要数字素材，
页身份只在单个快照内有效。相同 `asset_id` 全局只占一份空间。每步保留全部在线工单素材
（含本批旧工单），新目标可取整包或以**步首已驻留**文件为基底的差分；流式差分不占配额，
峰值按去重整文件大小检查；切换后才回收无人引用的文件。子集动态规划在全部批次排列上最小化
总下载字节，打平按批次 ID 的 ASCII 字典序决胜，同价差分按基底 ID 决胜。无完整路线时退出 0
并发布 `blocked` 诊断（最长可达前缀 + 每剩余批次的超容量峰值与/或无来源目标）；非法输入
退出 2。完整输入/输出字段与阻塞诊断规则见
[缓存换版契约](docs/cache-rollout.md)。

```sh
.venv/bin/signature-press --cache-rollout \
  --source fixtures/cache-rollout.json --output run/rollout.json
```

库入口为 `signature_press.rollout.process_rollout(dict)`。夹具是验收用的两批场景
（A: x/a → p；B: x/b → q；p 有整包及基于 b 的 50 字节差分，q 仅整包）：容量 1100 时最优
顺序为先 A 后 B、用差分取 p、总下载 350；容量降到 1000 时 A 首切峰值 1100 超容，只能先 B
后 A，此时 b 已回收、p 只能取整包，总下载 500，峰值 1000、900。

## v6 纸张批号召回

显式 `--lot-recall`（与前五个模式互斥）把供应商的批号召回与同一份离线台账核对。它先用
现有 v2 规划器得到**执行工单**，再用 `print_lots` / `bind_spans` / `pack_spans` /
`dispatches` 追踪受影响的实物书帖最终进入哪一册、哪一箱、是否已整箱发给哪个收货方，
**不改变工单、拼版或出库记录，也不向外发送通知**。报告分三份操作清单：未装订的受影响
书帖（按印刷批给实物码范围）、在厂只要一册受影响便整箱扣留的箱号（并单列因整箱扣留而
搭扣的好书册）、以及按收货方给出的发出箱号与真正应召回册号。装订可按 `+1`/`-1` 方向
取料，打包也可倒序装箱，出库可横跨相邻包装行。完整输入/输出字段与全部校验规则见
[批号召回契约](docs/lot-recall.md)。

```sh
.venv/bin/signature-press --lot-recall \
  --source fixtures/lot-recall.json --output run/recall.json
```

库入口为 `signature_press.recall.process_recall(dict)`。夹具的两帖 16 册版中，真实受影响
册为 2–4、15–16，剩余扣件 L17–18；箱 C103 发给 D 时只召回册 2–4、在厂扣 C100 并搭扣
册 13–14。全部集合以区间表示，10⁹ 册 / 10⁸ 箱的大用例报告约 1 KB，可在 256 MiB 进程
地址空间限制下运行。v6 同样原子新建、拒绝覆盖，非法输入退出 2 且不留半份报告。

## v7 供纸计数班单

显式 `--press-meter`（与前六个模式互斥）把印厂供纸计数器的**间歇回卷读数**与常晚到的
机长上机记录离线归集成**每班一张**的用纸单：按班次、机台和 `{edition_id, signature}`
汇总实际送入机台的纸张（含试印和废张）。一条账链一次初始化（固定 v2 打样批、递增班界、
值班机台的每秒上限/计数器模数/0 秒初始读数）、多次接续：首包给 `init`，接续包只带上
一份产物的 `continuation` 作 `state` 和本次新增数据，不重取已归档流水、不重开已出班单。
两路证据各有完整水位，新增行可无序、可越过水位但不得倒灌到上次水位之前；读数段只约束
`[a,b)` 实耗与 `y−x` 模数同余，允许零张与多次回卷。跨换单或交班的差额不按时间比例摊：
证据齐全后逐键求精确上下界，只有全部键唯一才出单，否则报告待核验（附各键上下界与导致
差异的机台、读数段端点）；两路均完整却不存在合理实耗的读数段是输入矛盾（退出 2）。跨
截止点读数段的已归档部分留在续算状态里继续约束后续班单，仅凭最新状态继续与从头核算
相同。完整输入/输出字段、水位与结班规则、归集公式与核对方法见
[供纸计数契约](docs/press-meter.md)。

```sh
.venv/bin/signature-press --press-meter \
  --source fixtures/press-meter.json --output run/meter-1.json
# 接续包：state 取上一份产物的 continuation，再加本次新增数据
.venv/bin/signature-press --press-meter \
  --source pkg-2.json --output run/meter-2.json
```

库入口为 `signature_press.meter.process_meter(dict)`。夹具首包（读数水位只到 5）尚不能
出班单，摘要为 `{"diagnosis": "incomplete", "new_slips": 0, ...}`；补入 (5,103) 并把读数
水位推进到 16 后首班 A 帖恰为 6 张（其中 3 张来自跨班段 [5,15)，该段留给 B 帖恰好 5 张），
再接 (20,114) 并封存后第二班 B 帖为 8 张。T=10⁹ 的长班不按秒展开，报告与状态仍为几千字节，
可在 256 MiB 地址空间限制下运行。v7 同样原子新建、拒绝覆盖，合法待核验与缺数据退出 0，
非法输入退出 2。
