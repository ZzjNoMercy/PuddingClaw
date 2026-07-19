# ADR-003：兼容 VPN/代理的 HTTPS Fake-IP DNS

| 字段 | 内容 |
|---|---|
| 编号 | ADR-003 |
| 标题 | 在不放宽私网 SSRF 边界的前提下兼容 VPN/代理 Fake-IP |
| 状态 | **Accepted** |
| 日期 | 2026-07-18 |
| 相关模块 | `backend/utils/network_safety.py`, `backend/services/skill_management.py`, `backend/tools/fetch_url_tool.py` |

## 1. 背景

本地用户可能通过 Clash、sing-box 等 VPN/代理访问互联网。启用 Fake-IP 模式后，DNS 不返回目标网站的真实公网地址，而是返回代理保留地址；代理再截获该连接并转发到真实目标。

2026-07-18 在真实环境中观察到：

| 域名 | DNS 结果 | 直接 HTTPS |
|---|---:|---:|
| `aihot.virxact.com` | `198.18.0.118` | HTTP 200 |
| `github.com` | `198.18.0.116` | HTTP 200 |
| `codeload.github.com` | `198.18.0.119` | 可下载 GitHub Archive |

`198.18.0.0/15` 是 RFC 2544 基准测试保留网段，也是 Clash/sing-box 常见的 Fake-IP 地址池。Python `ipaddress` 将其判断为非公网地址。旧逻辑只接受 `ip.is_global`，导致 `prepare_skill_install`、`prepare_skill_update` 和 `fetch_url` 在 HTTP 请求发出前把正常 VPN 流量误判为 SSRF。

这不是执行容器内网，也不代表宿主机无法出站。`execute` 沙箱的网络隔离是另一套独立策略。

## 2. 决策

共享地址策略接受两类目标：

1. 正常公网 IP；
2. `198.18.0.0/15` 中的地址，但必须同时满足：
   - URL 使用 `https://`；
   - URL 使用域名而不是 IP 字面量；
   - TLS 证书和原始域名匹配；
   - 每次重定向重新执行相同校验。

以下目标仍然拒绝：

- HTTP Fake-IP；
- `https://198.18.x.x/` 形式的 IP 字面量；
- localhost、loopback、RFC1918、link-local、metadata service；
- 非标准端口、带用户名/密码的 URL；
- 其他未经识别的非公网 Fake-IP 网段。

共享实现位于 `backend/utils/network_safety.py`，Skill 管理下载器和 `fetch_url` 必须复用它，不再维护两套不同的地址分类。

## 3. 对非 VPN 用户的影响

没有 VPN，或 VPN 使用真实 DNS/TUN 转发时，域名仍解析为正常公网 IP，继续走原有 `is_global` 分支，行为不变。

并非所有 VPN 都使用 Fake-IP，也并非所有 Fake-IP 实现都必须使用同一个地址池。本次兼容的是 Clash/sing-box 常用且当前真实环境验证过的 `198.18.0.0/15`。如果未来需要支持其他非公网地址池，应增加显式、受管配置和针对性测试，不能自动放开 `10/8`、`172.16/12`、`192.168/16` 等私网。

## 4. 安全依据

Fake-IP 例外只用于基于域名的 HTTPS。客户端仍以原始 hostname 发起 TLS，并验证服务端证书；如果 Fake-IP 没有被可信代理接管，或被导向错误服务，TLS 握手/证书校验失败，HTTP 请求不会成功。

这是一条针对保留 Fake-IP 网段的窄兼容路径，不是“允许访问非公网地址”的通用开关。

## 5. 验证记录

修复后在当前 VPN 环境真实验证：

- AIHOT 官方源 `prepare_skill_update` 成功；
- GitHub 镜像 `prepare_skill_install` 成功；
- `fetch_url` 成功读取 AIHOT `SKILL.md`；
- 官方源和 GitHub 镜像生成相同的暂存 SHA-256：`593b81d9efc163653512bd60a093161b0367424606bf607d5f962948591501ee`；
- 安全测试覆盖 HTTPS Fake-IP、HTTP Fake-IP、IP 字面量、RFC1918、metadata 地址和无 VPN 公网 DNS；
- `backend/tests` 691 项通过，前端 TypeScript 检查通过。

## 6. 运维排查提示

如果日志出现 `198.18.x.x`：

1. 先用相同运行环境的 `curl -v https://目标域名` 验证是否能返回正常 TLS/HTTP；
2. 不要直接断言“容器无外网”或“目标解析到内网”；
3. 区分宿主机/后端工具网络与 `execute` 沙箱网络；
4. 如果地址不属于 `198.18.0.0/15`，记录 VPN/代理类型和 Fake-IP 配置后再评估，不要扩大私网白名单。
