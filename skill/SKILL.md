---
name: workbuddy-daily-checkin
description: WorkBuddy（腾讯 AI 办公助手）每日自动签到领积分的整套方案：从本机解密登录 Token、调用签到 API、到设置定时任务/自动化。适用于想"白嫖"Buddy 加油站积分、不再忘记签到的用户。当用户提到 WorkBuddy 自动签到、每日签到脚本、Buddy 加油站积分自动领取、或想自动化 WorkBuddy 自身功能时使用。
---

# WorkBuddy 每日自动签到

## 背景
WorkBuddy 客户端内置「Buddy 加油站」每日签到领积分（连续签到有抽奖/周边）。本方案用 Python 脚本自动完成，无需手动点，可挂定时任务每天跑。

## 关键知识（踩坑总结，最重要的部分）

### 1. Token 存储位置
WorkBuddy 登录态存在：
- `C:\Users\<用户名>\AppData\Roaming\WorkBuddy\Local State`
  - 加密主密钥：`os_crypt.encrypted_key`，**DPAPI 加密**，前 5 字节为 `"DPAPI"` 前缀
- `C:\Users\<用户名>\AppData\Roaming\WorkBuddy\User\globalStorage\state.vscdb`（SQLite）
  - key = `secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}`
  - 值为 **AES-256-GCM 密文**，格式：`v10`（3字节）+ 12字节 nonce + 密文+tag

### 2. 解密流程
```
DPAPI 解主密钥 (去前5字节前缀)
  → AESGCM(master_key).decrypt(nonce, ciphertext)
  → JSON { accessToken, token(纯JWT), expiresAt, account:{uid,nickname} }
```

### 3. API 鉴权（致命细节）
- **必须用纯 JWT**（`token` 字段），**不是**带 uid 前缀的 `accessToken` 字段。用错会 401。
- Header：`Authorization: Bearer <纯JWT>`，`Content-Type: application/json`

### 4. 接口
| 用途 | 方法 | URL |
|------|------|-----|
| 查询状态 | POST | `https://copilot.tencent.com/billing/meter/checkin-status` |
| 执行签到 | POST | `https://copilot.tencent.com/billing/meter/daily-checkin` |

### 5. 两个大坑（已踩过）
- ⚠️ `checkin-status` 返回的 `today_checked_in` / `active` **不可靠**，不能用来判断"是否已签"。要以"签到接口实际返回"为准。
- ⚠️ 签到接口对"今日已领"返回 **HTTP 400 + body 内 `code: 10001`**（不是 200）。必须用业务码判断，**不能** `raise_for_status()` 抛异常，否则读不到真正的提示。
- ✅ 签到接口**幂等安全**：重复调用不会重复领取，可天天定时跑。

## 依赖
- Python 3 + `requests` + `cryptography`
- **必须在当前 Windows 用户会话下运行**（DPAPI 限制，换用户/服务账户会解密失败）

## 脚本
见 `scripts/wb_daily_checkin.py`（已泛化用户名路径，用 `os.path.expanduser("~")`）。

```bash
python wb_daily_checkin.py              # 正常签到（幂等）
python wb_daily_checkin.py --dry-run    # 仅查询状态，不执行
python wb_daily_checkin.py --json       # 输出 JSON（便于日志/监控/自动化判断）
```

## 自动化设置（二选一，或双保险）
1. **WorkBuddy 自动化面板**（推荐，可视化可管理/暂停）
   - 创建自动化 → prompt 让其用 Bash 运行 `python wb_daily_checkin.py` → 每天 9:30 → ACTIVE
2. **Windows 任务计划程序**（即使 WB 没开也能跑，零 AI 消耗）
   - `Register-ScheduledTask` 每天 9:30 执行 `python.exe wb_daily_checkin.py`
   - 注意：创建需提权，沙箱/普通权限可能失败，失败则用 WB 自动化兜底

## 注意事项
- Token 有有效期（约几个月），过期后重开 WorkBuddy 客户端登录刷新即可，脚本会提示 401。
- 签到活动可能有赛季间歇期（返回 `active=false`），脚本会正确提示"活动未开启"，非脚本故障。
- **仅本地运行，只读自己机器的登录态，不触碰任何账号密码，安全合规。**
