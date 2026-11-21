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

实际6项 v1 测试通过；加上 v2 打样批共 23 项测试通过。CLI 演示生成 JSON，v1 摘要为：

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

## 当前边界

不生成PDF或控制打印机；纸面坐标是各面正视的工作票约定。 实际后续需求可从插页和混合纸型、指定纸位重印及配帖操作沿既有身份和结果集成；当前未实现这些扩展，也不保证固定数量的未来任务。
