# WorkBuddy 每日自动签到（Buddy 加油站）

让 WorkBuddy（腾讯 AI 办公助手）自己帮你每天签到领积分，告别"总忘记点"。

## 它能做什么

- 自动从本机读取 WorkBuddy 登录态（DPAPI 解密，**不碰任何账号密码**）
- 调用签到接口完成每日签到，领取 Buddy 加油站积分
- 支持连续签到、里程碑奖励展示
- **幂等安全**：重复运行不会重复领取，可挂定时任务天天跑

## 快速开始

```bash
pip install requests cryptography

# 正常签到（幂等）
python wb_daily_checkin.py

# 只看状态，不实际签到
python wb_daily_checkin.py --dry-run

# 输出 JSON（便于日志/监控/自动化判断）
python wb_daily_checkin.py --json
```

## 自动化（二选一）

1. **WorkBuddy 自动化面板**（推荐）：创建自动化任务，prompt 让它用 Bash 运行本脚本，设为每天 9:30，状态 ACTIVE。可视化可管理/暂停。
2. **Windows 任务计划程序**：`Register-ScheduledTask` 每天 9:30 执行 `python.exe wb_daily_checkin.py`。即使 WB 没开也能跑。

## 工作原理

WorkBuddy 登录态存于：
- `AppData\Roaming\WorkBuddy\Local State`（DPAPI 加密主密钥）
- `AppData\Roaming\WorkBuddy\User\globalStorage\state.vscdb`（AES-256-GCM 加密的 Token）

脚本解密后，用**纯 JWT** 调两个接口：
- `POST https://copilot.tencent.com/billing/meter/checkin-status`
- `POST https://copilot.tencent.com/billing/meter/daily-checkin`

## 注意事项

- 必须在**当前 Windows 用户**下运行（DPAPI 限制）。
- Token 有有效期（约几个月），过期后重开 WorkBuddy 客户端登录刷新即可。
- 签到活动可能有赛季间歇期（返回 `active=false`），属正常，非脚本故障。
- 仅本地运行，只读自己机器的登录态，安全合规。

## 踩坑记录

- `checkin-status` 的 `today_checked_in` / `active` 字段**不可靠**，判断"是否已签"须以 `daily-checkin` 接口实际返回为准。
- 签到接口对"今日已领"返回 **HTTP 400 + body `code:10001`**，须用业务码判断，不能 `raise_for_status()`。

---

本脚本为个人效率工具，仅供本地自动化自己账号的签到使用。
