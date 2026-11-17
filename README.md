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

