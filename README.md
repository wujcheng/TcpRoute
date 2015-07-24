# TcpRoute

TCP 路由器会自动选择最快的线路转发TCP连接。

通过 socket5 代理服务器提供服务。目前支持直连及 socket5 代理线路。

具体细节：
* 对 DNS 解析获得的多个IP同时尝试连接，最终使用最快建立的连接。
* 同时使用直连及代理建立连接，最终使用最快建立的连接。
* 缓存10分钟上次检测到的最快线路方便以后使用。
* 不使用异常的dns解析结果。

