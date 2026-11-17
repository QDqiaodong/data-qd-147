# 客户分时水费审计（billing_cli）

独立的"客户分时水费审计"数据应用。它从**累计表读数**（register）与**费率表**重建
账期用量，不读取也不改变网络配给、计划、交割或季节余额（与 `network_cli` /
`network_ledger` 完全隔离，无共享文件、数据库或状态）。全部计算仅用 Python 标准库，
整数精确比较与任意精度整数分计费，不按每 kWh 逐单位展开。

```sh
python -m water_rights.billing_cli --source INPUT --output REPORT
```

业务错误（契约违规、无可行分配、输出不安全等）一律退出 2 且**不改写已有报告**；
成功时原子替换输出。输入与输出不得相同，输出路径是符号链接时拒绝。

## 受限输入 JSON

准确包含 `meters`、`readings`、`cycles`、`rates` 四个字段（拒绝未定义字段、重复
JSON 键、非有限数值；文档 ≤8 MB，规则与 v1 受限 JSON 一致）。

### meters

每项 `{id, customer, modulus, max_kwh_per_hour}`：

- `id`、`customer` 遵循仓库 `ident` 规则（非空、去空白、≤128 字符、无控制字符）；
  表具 id 全局唯一，每客户至多 1 块表、不覆盖表具也不换表。
- `modulus` 为 1..10^9 整数；`register` 始终落在 `0..modulus-1`。
- `max_kwh_per_hour` 为 0..10^9 整数。

### readings

每项 `{meter, at, register}`：

- `meter` 必须引用已声明表具；每表最多 4000 条原始记录。
- `at` 是带 `Z` 的 UTC 分钟时间戳，形如 `2026-01-01T00:00Z`。
- `register` 为 `0..modulus-1` 整数。
- 规范化：同表同一时刻、同一表位的重复记录**折叠为一条**（报告中给出
  `folded_duplicate_readings` 计数）；同表同一时刻出现不同表位直接拒绝；
  其余读数按时刻升序排列。
- 相邻两读数的用量差：表位未下降时取差值；表位下降视为**恰好一次** modulus
  回卷，用量 = 后值 − 前值 + modulus。回卷最多一次，不猜测多圈。

### cycles

每项 `{id, customer, start, end, tiers}`：

- `id` 遵循 `ident` 且全局唯一；`customer` 必须拥有一块表。
- `start`/`end` 是 UTC 午夜边界；周期为半开区间 `[start, end)`，长度只能是
  30 或 31 个整日；同一客户最多 3 个连续、首尾相接且互不重叠的周期。
- `tiers` 非空，每项 `{threshold, price}`：`threshold` 为升序累计 kWh 整数阈值，
  **末项阈值必须为 null**（开口档）；`price` 为每 kWh 整数分费率且逐档不下降。
- 账期只有在**首尾都被读数包围**（存在 `at <= start` 与 `at >= end` 的读数）时
  才开票；否则该周期在报告中标注 `billed: false`，不猜测缺失读数、不插值。

### rates

每项 `{date, segments}`，`date` 为 UTC 日期 `YYYY-MM-DD`：

- `segments` 为该日期内互不重叠的半开分钟时段
  `{start_minute, end_minute, cents_per_kwh}`，每日最多 8 段；
  必须从分钟 0 连续覆盖到 1440（完整 24 小时）。
- `cents_per_kwh` 为非负整数分。所有被读数区间触及的 UTC 日期都必须有费率表，
  否则审计失败。

## 可行分配与守恒规则

一个读数区间（相邻两条规范化读数之间）的用量差必须被**精确守恒一次**：整数 kWh
被分配到该区间覆盖的每个 UTC 日期，再细分到当日各费率段。

- **区间功率上限**：区间用量不得超过
  `floor(elapsed_minutes * max_kwh_per_hour / 60)`，否则审计失败。
- **逐日功率上限**：任一 UTC 日期上的区间用量不得超过 `24 * max_kwh_per_hour`。
- **跨周期联动**：若一个读数区间跨过账期边界，同一原始差额只分配一次；其各日
  用量同时构成相邻周期的约束，两个周期在**同一个联合可行域**上求解，绝不把差额
  复制一份分别独立计费。
- 可行分配是上述守恒与上限约束下的非负整数分配。若任一开票周期不存在可行分配，
  整份报告失败并保留旧输出。

## 计价（每个周期分别计）

对一份联合可行分配，周期应付整数分由两部分相加，无中间舍入：

1. **累计阶梯费**：周期总用量按 `tiers` 从低到高分层累进，每 kWh 取所在档整数分；
2. **分时费**：周期内每个日/费率段用量乘以该时段 `cents_per_kwh` 后求和。

审计对**所有同时满足**读数差守恒、逐日速率、周期边界与累计阶梯量的整数分配，
分别求周期总账单的精确最小值与最大值。

## 求解方法（为何精确且不按单位展开）

可行流的容量集合只有"每个区间"与其"每个日子集"，构成**层状族（laminar family）**，
因此可行整数流构成一个整数多拟阵基多面体（integral polymatroid base polytope）。
阶梯价随累计量不降、分时价为常数，目标是可分凸（最小化）/可分凹（最大化）函数，
其边际贪心（marginal greedy）给出精确整数最优：

- 最小化：每次在仍可接纳用量的 (区间, 日期, 时段) 槽位中取边际费用最小者，按
  整批瓶颈量推进（受区间剩余量与当日剩余容量约束），绝不逐 kWh 迭代；
- 最大化：取边际费用最大者；跨阶梯边界时拆批，保证批内边际不变；
- 每个周期求解两次：一次只给该周期挂真实价格（其余周期槽位边际为 0）做最小化，
  一次做最大化——等价于在完整联合可行域上对该周期账单寻优，跨周期共享差额因此
  被天然地一次性守恒；
- 价格并列时，按见证向量的槽位顺序（UTC 日期、时段起点、读数时刻）取**字典序
  最小**的见证：等价价格下让排序靠后的槽位优先填充。

10^9 量级用量下，推进次数只与区间数、日期数、阶梯数和费率段数相关，与用量无关。

## 报告 JSON（可独立复算）

`{schema_version: 1, kind: "billing-audit", source_sha256, customers: [...]}`，
每个客户块含：

- `customer`、`meter`（id/modulus/max_kwh_per_hour）、规范化后的 `readings`、
  `folded_duplicate_readings`、`billed_intervals`；
- `cycles`：未包围的周期仅标注 `billed: false`；开票周期含 `minimum` 与
  `maximum` 两个边界，各自给出 `usage_kwh`、`tier_cents`、`time_of_use_cents`、
  `total_cents`，以及覆盖全部相关读数区间的 `intervals` 见证：
  每个区间列出首尾读数与表位、是否回卷、分钟跨度、精确 `usage_kwh`，再按
  UTC 日期给出 `day_cap_kwh`、`total_kwh` 与逐费率段 `kwh` 见证。

复算要点（`billing_verify.verify_report` 已实现）：

1. 区间各日各段 kWh 之和必须等于首尾读数差（含一次回卷）——读数守恒；
2. 逐日合计不得超过 `day_cap_kwh`，时段窗口与分时单价必须与 `rates` 一致；
3. 周期内各段合计 = `usage_kwh`，按 `tiers` 重算阶梯费、按段重算分时费，
   两者之和必须等于 `total_cents`（任意精度整数分）。

## 输出安全

与仓库其他应用一致：原子替换、拒绝符号链接、输入输出同路径拒绝；校验或可行性
失败时不触碰既有报告文件。
