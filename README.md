# 入仓台 · 批次校验与发布

客户、合同、发票三类 CSV 的整批入仓工具。批次内文件可以按**任意顺序**到达；上传只做逐行格式检查，
主键唯一、跨表引用、合同金额上限统一在**封口**后核对；全部约束通过才**一次发布**。发布失败或中断时，
对外查询始终读取上一个完整版本。

无第三方依赖：Python 3.11 标准库（`http.server` + `sqlite3`）+ 原生 HTML/CSS/JS。

## 启动

```bash
python3 run.py                      # http://127.0.0.1:8080
python3 run.py --port 9000 --db /tmp/demo.db
```

打开页面后点左侧「本地样例 → 混乱批次」即可看到完整演示。数据默认保存在 `data/warehouse.db`
（已被 `.gitignore` 忽略）。

## 功能与规则

### 三类文件契约

| 类型 | 文件 | 列 |
| --- | --- | --- |
| 客户 | `customers.csv` | `customer_id,name,region,contact_email` |
| 合同 | `contracts.csv` | `contract_id,customer_id,title,amount,signed_on` |
| 发票 | `invoices.csv` | `invoice_id,contract_id,amount,issued_on` |

ID 形如 `CUST-0001` / `CTR-0001` / `INV-0001`；金额最多两位小数；日期 `YYYY-MM-DD`。
表头必须完全匹配（顺序固定），单文件上限 512 KiB / 2000 行。

### 阶段划分

1. **上传（任意顺序）** — 每次上传保存为一个候选版本；同类型再次上传替换“当前候选”，旧版本仅归档
   用于审计，不参与封口/发布。此阶段**不检查外键**，因为被引用的文件可能尚未到达。
2. **候选预览** — 查看暂存行、逐行格式错误、行号与文件版本号。
3. **封口校验** — 只读当前候选版本，一次性核对：
   - `pk.duplicate`：主键在批次封口范围内唯一（含跨文件重复）；
   - `fk.missing_customer` / `fk.missing_contract`：合同引用客户、发票引用合同；
   - `amount.contract_cap_exceeded`：同一合同的发票金额合计不得超过合同金额；
   - `file.missing`：缺任一类型文件即阻止发布；
   - 行级 `row.*`：必填、ID/金额/日期/邮箱格式、字段数。
   封口按候选内容指纹（`kind:version:hash`）幂等：**同一候选重复点击封口复用同一封口记录**，
   不产生重复数据。修订文件后旧封口标记为过期，发布被拒绝。
4. **发布** — 两阶段：
   - 阶段 1：把封口通过的全部行写入带 `staged` 状态的版本快照，**不切指针**；
   - 阶段 2：校验快照行数、封口指纹与基线版本，再在一个事务里切换 `meta.latest_release_id`。
   对外查询只跟随该指针，因此阶段 1 之后崩溃/中断不影响线上数据。

### 错误报告

- 页面「约束明细」按规则分组、可筛选，显示 `文件名:行号`、字段、值与关联位置。
- `⤓ 下载拒绝原因` 导出 CSV（UTF-8 BOM，Excel 可直接打开），含文件名、行号；对以 `=`、`+`、`-`、
  `@` 开头的单元格做公式注入转义。历史封口也可单独下载。

### 中断与恢复

发布页可选择「模拟：暂存完成后中断」或「模拟：复制到一半中断」。中断后：

- 对外查询仍读旧版本，页面顶部会显示当前对外版本号；
- 完整暂存版本可**恢复发布**（重新核对行数、封口指纹、基线版本后切指针）；
- 半截快照不完整，只能**丢弃**后重新发布；
- 若中断期间候选被修订或基线版本已前进，恢复会被拒绝（`staged_stale` / `base_moved`）;
- 进程真正崩溃留下的 `running` 操作会在下次启动时标记为 `interrupted`（`reconcile_interrupted`）。

所有切换都在 `BEGIN IMMEDIATE` 事务内完成，并有 `guard` 更新保证并发点击只产生一个激活版本。

## 本地样例（全部为虚构数据）

| 目录 | 用途 |
| --- | --- |
| `samples/scenario_initial/` | 混乱批次：`重复主键`（客户 CUST-0003 重复）、`外键缺失`（合同引用 CUST-0009、发票引用 CTR-0099）、`金额超限`（CTR-0002 开票 58,000 > 合同 50,000） |
| `samples/scenario_fixed/` | 修正后版本，可直接封口发布 |
| `samples/scenario_next/` | 第二个完整批次，用于验证版本指针切换 |

「混乱批次」按钮的到达顺序固定为 **发票 → 合同 → 客户 → 发票（修订）**，由此验证：
外键在文件未到齐时不误报，重复上传同类型文件只替换候选、不污染已发布版本。

## 测试

```bash
python3 -m unittest tests.test_service tests.test_http -v     # 31 项：领域 + HTTP + 并发
node tests/dom_e2e.js http://127.0.0.1:8080 samples           # 可选：jsdom 驱动真实前端
```

`tests/dom_e2e.js` 需要 `jsdom`（测试环境已装在 `/tmp/node_modules`）；它直接加载
`server/static/` 的真实资源对接运行中的服务，覆盖上传、预览、封口、修订、发布、中断恢复与只读查询。

## 目录

```
run.py                  启动入口
server/db.py            SQLite schema、连接、版本指针初始化
server/csvparse.py      CSV 解析与行级校验（不在此做跨表检查）
server/service.py       批次、封口、发布/恢复、只读查询、拒绝报告
server/app.py           HTTP 路由与静态资源
server/static/          页面、样式、前端逻辑
samples/                本地虚构样例
tests/                  unittest + DOM 端到端
```
