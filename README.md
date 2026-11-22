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

实际6项 v1 测试通过；加上 v2 打样批共 23 项测试通过；加上 v3 装订审计共 44 项测试通过；加上 v4 离线制版费结算共 77 项测试通过。CLI 演示生成 JSON，v1 摘要为：

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
