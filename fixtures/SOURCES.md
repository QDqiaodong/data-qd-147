本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。
input.json 为v1盆地配给样例；network_input.json/network_delivery.json 为网络配给与交割样例（两源头、三渠道、两优先级三用户，交割为部分执行）。
billing_input.json 为客户分时水费审计样例（两客户三账期，含重复读数折叠、一次 modulus 回卷与跨账期边界读数区间）。

quality_config.json / quality_changes.jsonl / quality_corrections.jsonl / quality_restore.jsonl
为水质连续暴露数据仓样例：两站点（river-a 双污染物、well-b 单污染物），初始批次含缺测空档、
一跨日超标与一次校准缺口；修正批次移动一条观测并撤销一段校准（使共同已知域出现空档、
告警分裂），恢复批次重新启用该校准（告警合并）。全部为原创虚构合成数据。

permit_site/ 为取水许可证公告采集器的自制源站样例（分页公告、详情、RFC4180 配额 CSV、
同源重定向与 BOM/引号字段），全部为原创虚构合成数据，仅用于本地标准库 HTTP 服务演示，
不对应任何真实网站或账号。

terrain_input.json 为地形排水空间底图样例（5x5 同形高程/降雨栅格、三处不可通行掩膜、
双出口、一个内部洼地需 10 mm 填洼），全部为原创虚构合成数据。
