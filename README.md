# data-qd-147 · 分级水权短缺配给

Priority ordered proportional allocation under basin withdrawal caps。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/water-rights --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/water_rights/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 网络配给与交割（network_cli）

在共享渠道网络中把调度季水权计划落到可追溯的实际交割，详见
[网络配给数据契约](docs/network_contract.md)。一个SQLite文件即一个输入冻结的调度季账本；
计划只签发身份不预占水量，交割按实际量重解路由并在同一原子提交内扣减实际源头取水、
渠道流量与用户量。原 `water-rights` 命令与 v1 结果保持不变。

```sh
.venv/bin/python -m water_rights.network_cli init --source fixtures/network_input.json --db run/season.db
.venv/bin/python -m water_rights.network_cli plan --db run/season.db --output run/plan.json
.venv/bin/python -m water_rights.network_cli settle --db run/season.db --plan run/plan.json \
    --source fixtures/network_delivery.json --delivery-id D-1 --output run/receipt.json
.venv/bin/python -m water_rights.network_cli status --db run/season.db --output run/status.json
```

network_flow.py 为精确字典序网络求解（三次最小费用流，大整数支配权重，不按单位展开），
network_model.py 为输入契约，network_ledger.py 为SQLite账本（HMAC计划身份、余额版本、
原子扣账、回执恢复），network_cli.py 衔接命令行。业务错误退出2并保留既有输出与账本。

## 客户分时水费审计（billing_cli）

独立的客户分时水费审计，从累计表读数与费率表重建账期用量，输出每个客户每个账期
可行应付金额的精确下界/上界及逐日/时段 kWh 见证。它与网络配给、计划、交割、季节
余额完全隔离，不读取也不改变任何网络状态，详见[客户分时水费审计契约](docs/billing_contract.md)。

```sh
python -m water_rights.billing_cli --source fixtures/billing_input.json --output run/billing_report.json
```

billing_model.py 为受限输入契约与读数规范化（重复折叠、同刻冲突拒绝、一次回卷），
billing_audit.py 在层状容量族的整数多拟阵基多面体上做边际贪心，跨账期区间在同一
联合可行域上求解、差额只守恒一次；billing_verify.py 从报告见证独立复算读数守恒、
周期阶梯量与分时收费；billing_cli.py 衔接命令行。无可行分配或契约违规时退出2且
保留旧报告，输入输出不得相同、拒绝符号链接输出。

## 水质连续暴露数据仓（quality_cli）

独立的“水质连续暴露数据仓”，把可迟到更正的瞬时浓度观测与实验室校准变更流，
转为持久化的已知覆盖、超标暴露及告警区间。它以单个 SQLite 文件为一个配置冻结的
新仓，不读取配给、交割、表位或费率数据，详见
[水质连续暴露数据契约](docs/quality_contract.md)。

```sh
.venv/bin/python -m water_rights.quality_cli init --source fixtures/quality_config.json --db run/quality.db
.venv/bin/python -m water_rights.quality_cli ingest --source fixtures/quality_changes.jsonl --db run/quality.db
.venv/bin/python -m water_rights.quality_cli ingest --source fixtures/quality_corrections.jsonl --db run/quality.db
.venv/bin/python -m water_rights.quality_cli ingest --source fixtures/quality_restore.jsonl --db run/quality.db
.venv/bin/python -m water_rights.quality_cli report --db run/quality.db \
    --start 1767225600 --end 1767398400 --output run/quality_report.json
```

quality_model.py 为受限配置与 JSONL 变更契约（`[n,d]` 有理数、实体修订、最终态
冲突），quality_warehouse.py 为 SQLite 事实仓与精确片段化物化（插值、校准、
共同已知域、超标并集、解析积分、全仓告警、原子提交与重放），quality_cli.py 衔接
命令行。业务错误退出2并保留既有报告与仓，`init` 拒绝覆盖。

## 取水许可证公告采集器（permit_collect）

独立的“取水许可证公告采集器”，仅用标准库把分页公告、许可详情与月配额 CSV 采集为
可离线复核的数据集。一个 SQLite 文件即绑定入口 URL 的采集器身份，持久保存原始响应
正文、请求/最终 URL、ETag、快照标识与 SHA256；只处理公开许可的声明配额，不接触
配给交割、计费与水质仓，详见[取水许可证公告采集契约](docs/permit_contract.md)。

```sh
# 终端 1：自制源站（标准库 ThreadingHTTPServer，无需真实网站或账号）
python3 -m water_rights.permit_demo --root fixtures/permit_site --snapshot G1 --port 8080
# 终端 2：采集与离线复核
PYTHONPATH=src python3 -m water_rights.permit_collect \
  --index http://127.0.0.1:8080/announce/ --state run/permits.db --output run/permits.json
PYTHONPATH=src python3 -m water_rights.permit_collect \
  --index http://127.0.0.1:8080/announce/ --state run/permits.db --output run/permits.json verify
```

每轮先复验首页得到快照 G，其后请求均带 `X-Expected-Snapshot: G`；304 仅在同 URL、
同 G 且本地 SHA 核验通过时复用，缓存损坏则无条件重下。permit_html.py 严格恢复
rowspan/colspan 逻辑矩形并按叶标题取三列，permit_csv.py 以整数升精确换算并校验
年度守恒，permit_http.py 强制同源重定向（≤3 跳、无 userinfo），permit_store.py
以 `candidate→published→superseded` 状态机和原子发布指针保证中断恢复不丢出口、
不重复配额，且已发布报告始终能凭库内原始字节离线复核。业务错误退出 2 并保留旧报告
与全部证据，成功回执含数据集身份与出口数。

## 地形排水空间底图（terrain_cli）

独立的地形与降雨数据产品，把数字高程栅格转为可逐格复算的填洼高程、等 H 平台导流、
出口汇水分区与精确整数径流/填洼体积，为后续系统提供排水空间底图。它不读取网络调度、
计费、水质仓或许可采集状态，不连接外部服务，也不新增持久账本，详见
[地形排水空间底图契约](docs/terrain_contract.md)。

```sh
python -m water_rights.terrain_cli \
  --source fixtures/terrain_input.json --output run/terrain_report.json
```

terrain_model.py 为受限栅格契约（同形矩形、掩膜双 null、拒绝布尔/浮点/重复键/额外
字段/参差矩形），terrain_analysis.py 以一次堆松弛求每格最小极大路径填洼高程 H
（O(N log N)，不逐高程单位抬升、不枚举路径），再以固定字典序的平台多源 BFS 确定
无环接收链，并以非递归反向累计保证逐格与逐出口的降雨、面积、填洼体积守恒；全程整数
精确、无时间戳与随机值、同输入字节一致。业务错误退出 2 且保留旧报告，输入输出不得
同路径、拒绝符号链接输出。

## Scope and sample
# Priority water-right allocation v1

面向流域管理数据人员，将有限取水量按明确的优先级组配给。basins为1..100个唯一id/cap整数；claims最多20000项唯一id、已知basin、priority整数(小优先)与requested非负整数水量。所有水量单位为输入统一整数，不做跨单位换算；0容量/需求合法。
每盆地独立处理priority升序。只要当前优先级组总需求未超剩余容量则全额满足；若超额，按各自申请量比例分配整数基数，剩余单位按最大余数降序发放，同余按claim id字典序，然后更低优先级全为0。需求为0不分配余数。输出requested/allocated/unmet、组需求和剩余量，守恒验证allocated总和不超过cap。
样例同级80、80竞争100，分配farm-a=50、farm-b=50，town低优先级=0。优先级代表已由政策方确认的严格类别，不是水文学建议；无季节流量预测、生态基流、回流水、权利法律裁定或公平政策评估。


## Scope and sample
# Priority water-right allocation v1

面向流域管理数据人员，将有限取水量按明确的优先级组配给。basins为1..100个唯一id/cap整数；claims最多20000项唯一id、已知basin、priority整数(小优先)与requested非负整数水量。所有水量单位为输入统一整数，不做跨单位换算；0容量/需求合法。
每盆地独立处理priority升序。只要当前优先级组总需求未超剩余容量则全额满足；若超额，按各自申请量比例分配整数基数，剩余单位按最大余数降序发放，同余按claim id字典序，然后更低优先级全为0。需求为0不分配余数。输出requested/allocated/unmet、组需求和剩余量，守恒验证allocated总和不超过cap。
样例同级80、80竞争100，分配farm-a=50、farm-b=50，town低优先级=0。优先级代表已由政策方确认的严格类别，不是水文学建议；无季节流量预测、生态基流、回流水、权利法律裁定或公平政策评估。


## Scope and sample
# Priority water-right allocation v1

面向流域管理数据人员，将有限取水量按明确的优先级组配给。basins为1..100个唯一id/cap整数；claims最多20000项唯一id、已知basin、priority整数(小优先)与requested非负整数水量。所有水量单位为输入统一整数，不做跨单位换算；0容量/需求合法。
每盆地独立处理priority升序。只要当前优先级组总需求未超剩余容量则全额满足；若超额，按各自申请量比例分配整数基数，剩余单位按最大余数降序发放，同余按claim id字典序，然后更低优先级全为0。需求为0不分配余数。输出requested/allocated/unmet、组需求和剩余量，守恒验证allocated总和不超过cap。
样例同级80、80竞争100，分配farm-a=50、farm-b=50，town低优先级=0。优先级代表已由政策方确认的严格类别，不是水文学建议；无季节流量预测、生态基流、回流水、权利法律裁定或公平政策评估。

