# Network allocation and delivery settlement ledger

离线"网络配给与交割"数据应用。一个 SQLite 数据库文件就是一个独立的、输入冻结的
调度季账本；计划（plan）只对当前余额快照签发不可篡改的身份，不预占水量；交割
（settle）按实际交割量重新求解路由，在同一原子提交里只扣减实际发生的源头取水、
渠道流量和用户交水。全部计算仅用 Python 标准库，整数精确比较，不按单位展开水量。

命令（业务错误一律退出 2，拒绝时保留既有输出与账本）：

```sh
python -m water_rights.network_cli init   --source season.json --db season.db
python -m water_rights.network_cli plan   --db season.db --output plan.json
python -m water_rights.network_cli settle --db season.db --plan plan.json \
    --source delivery.json --delivery-id D-1 --output receipt.json
python -m water_rights.network_cli status --db season.db --output status.json
```

## 调度季输入（init --source）

受限 JSON（同 v1：≤8MB、拒绝重复键/非有限数/未定义字段），字段必须准确：

```json
{
  "nodes": ["up", "down"],
  "sources":  [{"id": "s1", "node": "up", "cap": 40}],
  "channels": [{"id": "e1", "from": "up", "to": "down", "cap": 45}],
  "claims":   [{"id": "farm", "node": "down", "priority": 1, "requested": 30}]
}
```

- id 遵循既有 `ident` 规则（非空、≤128 字符、无控制字符），各集合内唯一；
  节点引用必须存在；渠道构成有向无环图；允许平行渠道、同节点多个源头/用户。
- 上限：60 节点、240 渠道、100 用户、16 种优先级；源头另设防御性上限 1000。
- `cap`/`requested` 为 0..10^9 整数；`priority` 沿用 v1 范围 0..10^6（小优先）。
- `cap` 是全季可用量：无补水、无损耗。中间节点必须满足
  流入 + 源头取水 = 流出 + 用户交水（流量守恒）。
- init 拒绝覆盖已有文件（含符号链接），输入与数据库路径不得相同；
  校验全部通过前不落盘，失败不留半个数据库。

## 网络求解目标（plan 与交割路由共用）

所有求解都使用源头、渠道、用户的**剩余额度**。计划在全部可行整数流中按以下
严格字典序择优（精确整数比较，不用浮点）：

1. 按 priority 升序，字典序最大化各组交水总量向量 S；
2. 固定全部 S：每组用该组剩余申请量按 v1 最大余数法求 S 的 Hamilton 目标，
   再按 priority 升序，字典序最小化各组计划量偏离目标的绝对值之和；
3. 再按 claim id 升序，字典序最大化各用户计划量；
4. 最后按 source id 升序、channel id 升序拼接的向量，字典序最小化取水与流量。

确定前级最优总量时**不冻结其路径**：第 1 层只保留组总量 S，第 2/3 层在完整
剩余网络上重解，允许高优先级改道为低优先级让路；渠道瓶颈使 Hamilton 目标
不可行时，仍按上述层级给出唯一结果。相同余额输入的数值结果与输入数组顺序无关。

### 求解结构为何保证目标层级

四层目标由**三次精确最小费用流**完成（`network_flow.py`）：

- 第 1 层一次最小费用最大流：每组一条汇边，单位费用取 −W_g，权重
  W_g = (F+1)^(k−1−i)（F 为可行总量上界，k 为组数）。网络的可行汇向量构成
  多拟阵（polymatroid），其字典序最大元是一个基（总量=最大流），且在支配
  权重下与最大权向量一致——故最大权流恰给出字典序最大的 S。只保留 S，
  路由丢弃。
- 第 2+3 层一次最小费用流：组总量用容量 S_g 的汇边固定；每个用户拆两段边，
  边际费用编码 ±W2_g·|x−目标| 再减 ε_c（ε 按 claim id 支配）。权重由全局
  上界推出并严格支配：D_g 改善 1 单位超过所有低组偏差与全部 ε 项之和，
  ε_c 改善 1 单位超过所有更小 id 用户的 ε 项之和。因此该线性目标的最小值
  点就是"先分层最小化偏差、再按 id 最大化"的唯一字典序最优点。
- 第 4 层一次最小费用流：用户量固定为上限，源头/渠道边带支配正权重，
  最小化拼接向量。交割路由复用同一构造（量给定为实际交割量）。

所有费用为精确大整数；SSP 最短路增广保持整数性，瓶颈增广一次推满整条
最短路，10^9 容量不按单位展开。由于每层都在完整网络上重解、层间只传递
标量（S、偏差、用户量），前级路径从不被冻结。

### 跨路由扣账为何正确

交割不引用计划的路线：它以"计划签发时的余额版本"为条件，在同一
`BEGIN IMMEDIATE` 事务内读取余额、对**实际交割量**重新求解第 4 层路由，
并用带 `used + ? <= cap` 守卫的 UPDATE 只扣减实际源头取水、实际渠道用量和
实际用户量，版本 +1 并写入回执。版本一致 ⟺ 余额与计划所见一致，故实际量
≤ 计划量保证路由可行；版本漂移、额度不足或守卫失败整体回滚，零扣减。

## 计划文档（plan --output）

`{schema_version:1, kind:"network-plan", ledger_id, plan_id, balance_version,
input_sha256, result, tag}`。`result` 含逐用户 `planned`/`target`、各组总量、
源头取水与渠道流量。`plan_id` 是对 `{ledger_id, balance_version, input_sha256,
result}` 规范化 JSON 的 SHA256；`tag` 是用账本内随机密钥对 plan_id 的
HMAC-SHA256。plan 只读快照、不写账本、不预占水量；同一余额版本可签发多份
计划，交割竞争时最多一个提交。

## 交割输入（settle --source）

准确为 `{"deliveries":[{"claim","amount"}]}`：用户不重复，`amount` 为
0..10^9 整数，至少一项为正，每项（含零额项）不超过该计划中的用户计划量。
规范化 = 去零项并按 claim 排序，仅用于路由求解与重放比较。拒绝（均零扣减、退出 2）：未签发/被改写/异账本/
余额版本过期/额度不足的计划，以及不合法交割。

- 幂等：相同 delivery-id + 同一 plan_id + 相同规范化用户量 → 返回原回执，
  不因余额版本已变化而拒绝，不重复扣减；同 ID 不同请求 → 冲突拒绝。
- 回执在扣账的同一事务内写入账本，进程在提交后、输出发布前退出时，用同
  delivery-id 重试即可恢复；账本不依赖外部 JSON 文件判定是否已扣减。
  回执输出失败不撤销已提交交割，明确报错并支持同 ID 恢复。
- 回执含 plan 身份、前后余额版本、实际逐用户量、实际源头取水与渠道流量。

## 状态（status --output）

`{schema_version:1, kind:"network-status", ledger_id, balance_version,
input_sha256, sources, channels, claims, deliveries}`：三类对象的累计用量
（used/delivered）与剩余额度（remaining），以及全部交割回执（按提交顺序）。

## 输出安全

输出（计划/回执/状态）原子替换、拒绝符号链接；输入与数据库路径不得被输出
覆盖；init 拒绝覆盖已有数据库文件。部分交割消耗源头/共享渠道后，剩余额度
约束下一次求解的可行域，未交割用户按剩余申请量与剩余网络容量重新参与配给；
旧计划因余额版本过期而失效。
