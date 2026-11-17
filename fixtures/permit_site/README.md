# 自制取水许可源站样例

本目录是一个完全手写、无需真实网站或账号的源站样例，配合
`python -m water_rights.permit_demo --root fixtures/permit_site` 提供标准库
`ThreadingHTTPServer` 演示。所有 HTML 为严格 UTF-8、结构正确嵌套；CSV 为
RFC4180，其中一个带 UTF-8 BOM。

## 布局

- `announce/index.html`：公告第 1 页，两层表头（rowspan/colspan）、数据
  rowspan（P-1001 跨两行两个出口），`a#next` 指向第 2 页；含被忽略的导航/广告。
- `announce/page2.html`：公告第 2 页，无 `a#next`，分页在此结束。
- `permits/p1001-o1.html`、`p1001-o2.html`：许可 P-1001 的两个出口详情，
  年度额度分别为 `12000.500`、`6000` m³；后者用相对链接 `../quotas/...`。
- `permits/p1002.html`：年度 `360.25` m³，附件为相对路径 `p1002-quota.csv`。
- `permits/p1003-real.html`：请求 `/permits/p1003.html` 时服务端 302 到本页，
  演示同源重定向及相对附件基址变化；年度额度为 `0`（合法零额度）。
- `quotas/p1001-o1.csv`：含引号字段；十二个月之和精确等于年度 12000.500 m³。
- `quotas/p1001-o2.csv`：带 UTF-8 BOM。
- `permits/p1002-quota.csv`、`p1003-quota.csv`：分别演示三位小数与全零额度。

每个详情页 `a#quota` 的 `data-sha256` 是对应 CSV 原始字节的真实 SHA256，
采集器据此交叉校验。修改任何 CSV 后必须重新计算并回填该摘要。

## 服务端契约行为

- 每个 200/304 响应都带非空 `ETag` 与 `X-Permit-Snapshot`；
  `If-None-Match` 命中时返回 304。
- `X-Expected-Snapshot` 与当前快照不符时返回 412。
- `--foreign-port` 启动第二个外域服务，用于验证跨源重定向被拒且外域零请求。
