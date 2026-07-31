#!/usr/bin/env python3
"""
WorkBuddy 每日自动签到脚本 (Buddy 加油站)
==========================================
功能：
  - 自动读取 WorkBuddy 客户端本地登录 Token（DPAPI 解密）
  - 查询签到状态，若未签到则自动执行签到
  - 显示积分领取情况、连续签到天数等信息

用法：
  python wb_daily_checkin.py              # 手动运行一次
  python wb_daily_checkin.py --dry-run    # 仅查询状态，不执行签到
  python wb_daily_checkin.py --json       # 输出 JSON 格式结果（方便日志/监控）

定时任务（Windows 任务计划程序）：
  建议每天 9:00~10:00 之间执行一次即可
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import ctypes
import ctypes.wintypes
import base64

import requests


# ── 路径配置（泛化用户名，自动适配当前用户）─────────────
WORKBuddy_APPDATA = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "WorkBuddy"
)
STATE_DB = os.path.join(WORKBuddy_APPDATA, "User", "globalStorage", "state.vscdb")
LOCAL_STATE = os.path.join(WORKBuddy_APPDATA, "Local State")

# API 端点
BASE_URL = "https://copilot.tencent.com"
API_CHECKIN_STATUS = f"{BASE_URL}/billing/meter/checkin-status"
API_DAILY_CHECKIN = f"{BASE_URL}/billing/meter/daily-checkin"

# Token 存储键
TOKEN_KEY = 'secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}'

# 时区（北京时间）
CST = timezone(timedelta(hours=8))


# ── DPAPI 解密 ────────────────────────────────────────────
def dpapi_unprotect(data: bytes) -> bytes:
    """使用 Windows DPAPI 解密数据（必须在当前用户会话下运行）"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    inp = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(inp, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()

    if ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ) == 0:
        err = ctypes.GetLastError()
        raise OSError(f"DPAPI 解密失败，错误码: {err}。请确认在当前 Windows 用户下运行。")

    result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return result


def decrypt_workbuddy_token() -> dict:
    """
    从 WorkBuddy 本地存储中解密并读取 accessToken。
    返回包含 accessToken 等字段的字典。
    """
    # 1. 读取加密主密钥
    with open(LOCAL_STATE, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    encrypted_key_b64 = local_state["os_crypt"]["encrypted_key"]
    encrypted_key = base64.b64decode(encrypted_key_b64)
    # 去掉前 5 字节 "DPAPI" 前缀
    aes_key = dpapi_unprotect(encrypted_key[5:])

    # 2. 从 SQLite 数据库读取加密的 token
    conn = sqlite3.connect(STATE_DB)
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key=?", (TOKEN_KEY,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise FileNotFoundError(
            f"未找到 WorkBuddy 登录 Token。\n"
            f"数据库路径: {STATE_DB}\n"
            f"请先打开 WorkBuddy 客户端并确保已登录。"
        )

    # 3. 解密 token
    raw = json.loads(row[0])
    data = bytes(raw["data"])
    # os_crypt v10 格式：3字节 "v10" + 12字节 nonce + 密文+tag
    nonce = data[3:15]
    ciphertext_and_tag = data[15:]

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext_and_tag, None)
    return json.loads(plaintext.decode("utf-8"))


# ── API 调用 ──────────────────────────────────────────────
def api_call(url: str, token: str) -> dict:
    """
    调用 WorkBuddy API。
    返回解析后的 JSON 响应（包含 http_status 字段）。
    注意：不抛 HTTP 异常，因为签到接口对"已签到"返回 HTTP 400 + body 内 code=10001，
    需要由调用方从 body 中解析业务码。
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy-DailyCheckin/1.0",
    }
    resp = requests.post(url, headers=headers, json={}, timeout=15)
    try:
        body = resp.json()
    except Exception:
        body = {"code": -1, "msg": f"HTTP {resp.status_code}: {resp.text}"}
    body["http_status"] = resp.status_code
    return body


def get_checkin_status(token: str) -> dict:
    """查询签到活动状态（仅用于展示，不依赖其 today_checked_in 判断）"""
    return api_call(API_CHECKIN_STATUS, token)


def do_daily_checkin(token: str) -> dict:
    """执行每日签到，返回完整响应（含 http_status 与业务 code）"""
    return api_call(API_DAILY_CHECKIN, token)


# ── 主逻辑 ────────────────────────────────────────────────
def run(dry_run: bool = False, json_output: bool = False):
    """主流程"""
    now = datetime.now(CST)
    result = {
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "success": False,
        "status": "unknown",
        "message": "",
        "credits_today": 0,
        "streak_days": 0,
        "total_credits": 0,
    }

    try:
        # 1. 读取 Token
        print(f"🔐 正在读取 WorkBuddy 登录凭证...")
        token_data = decrypt_workbuddy_token()
        # 注意：API 需要纯 JWT（token 字段），不是带 uid 前缀的 accessToken
        jwt_token = token_data.get("token", "")

        if not jwt_token:
            raise ValueError("Token 为空，可能登录态已过期")

        # 检查过期时间
        expires_at = token_data.get("expiresAt", 0)
        if expires_at and expires_at < now.timestamp() * 1000:
            exp_time = datetime.fromtimestamp(expires_at / 1000, CST)
            print(f"⚠️  Token 已于 {exp_time.strftime('%Y-%m-%d %H:%M')} 过期")
            print("   请重新打开 WorkBuddy 客户端登录以刷新 Token")

        uid = token_data.get("account", {}).get("uid", "未知")
        nickname = token_data.get("account", {}).get("nickname", "未知")
        print(f"✅ 登录成功 | 用户: {nickname} ({uid[:8]}...)")

        # 2. 查询签到状态（仅用于展示，不依赖其 today_checked_in 判断）
        print(f"\n📋 查询签到状态...")
        status_resp = get_checkin_status(jwt_token)

        status_data = status_resp.get("data") or {}

        # 解析状态（注意：today_checked_in 字段不可靠，仅作展示参考）
        streak_days = status_data.get("streak_days", 0)
        today_credit = status_data.get("today_credit", 0)
        total_credits = status_data.get("total_credits", 0)
        is_streak_day = status_data.get("is_streak_day", False)
        next_streak_day = status_data.get("next_streak_day", 0)
        streak_bonus_credit = status_data.get("streak_bonus_credit", 0)
        active = status_data.get("active", False)
        today_checked_in_hint = status_data.get("today_checked_in", False)

        result["streak_days"] = streak_days
        result["total_credits"] = total_credits
        result["credits_today"] = today_credit

        print(f"   🎯 活动状态: {'✅ 已开启' if active else '⚠️  未开启（但可能仍可签到）'}")
        print(f"   📅 状态接口提示今日已签到: {'✅ 是' if today_checked_in_hint else '❌ 否'}")
        print(f"   🔥 连续签到: {streak_days} 天")
        print(f"   💰 今日可领: {today_credit} 积分")
        print(f"   💳 累计余额: {total_credits:.2f} 积分")

        if is_streak_day:
            print(f"   🎊 今天是里程碑日！连续 {next_streak_day} 天奖励: +{streak_bonus_credit} 积分")

        # 3. 执行签到（以实际签到接口返回为准，幂等安全）
        if dry_run:
            result["status"] = "dry_run"
            result["success"] = True
            result["message"] = "[Dry Run] 未实际执行签到"
            print(f"\n🔍 [Dry Run] 将尝试领取今日积分（未实际执行）")
        else:
            print(f"\n🚀 正在执行签到（以服务端返回为准）...")
            checkin_resp = do_daily_checkin(jwt_token)
            code = checkin_resp.get("code")
            http_status = checkin_resp.get("http_status")

            if code == 0:
                # 签到成功
                claim_data = checkin_resp.get("data", {})
                credit = claim_data.get("credit", today_credit)
                new_streak = claim_data.get("streak_days", streak_days)

                result["success"] = True
                result["status"] = "claimed"
                result["credits_today"] = credit
                result["streak_days"] = new_streak
                result["message"] = f"签到成功！获得 {credit} 积分，连续 {new_streak} 天"

                print(f"   ✅ 签到成功！")
                print(f"   🎁 本次获得: {credit} 积分")
                print(f"   🔥 连续签到: {new_streak} 天")

                if is_streak_day:
                    print(f"   🎊 里程碑奖励: +{streak_bonus_credit} 积分已到账！")
            elif code == 10001:
                # 今日已领取（HTTP 可能为 200 或 400，业务码统一为 10001）
                result["status"] = "already_claimed"
                result["success"] = True
                result["message"] = "今天已经领过啦，明天再来～"
                print(f"\n🟡 今天已经领过啦，明天再来～")
            elif code == 401 or http_status == 401:
                result["status"] = "unauthorized"
                result["success"] = False
                result["message"] = "登录凭证无效或已过期，请重新打开 WorkBuddy 客户端登录"
                print(f"\n❌ 登录凭证无效（401），请重新打开 WorkBuddy 客户端登录刷新 Token")
            else:
                # 其他业务错误（含活动未开启、活动结束等）
                msg = checkin_resp.get("msg", "未知错误")
                result["status"] = "failed"
                result["success"] = False
                result["message"] = f"签到失败 (HTTP {http_status}, code={code}): {msg}"
                print(f"\n❌ 签到失败 (HTTP {http_status}, code={code}): {msg}")

    except FileNotFoundError as e:
        result["status"] = "token_not_found"
        result["message"] = str(e)
        print(f"\n❌ {e}")
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"{type(e).__name__}: {e}"
        print(f"\n❌ 错误: {e}", file=sys.stderr)

    # 输出 JSON（如果需要）
    if json_output:
        print("\n--- JSON Output ---")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    return result


# ── 入口 ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="WorkBuddy 每日自动签到脚本 (Buddy 加油站)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python wb_daily_checkin.py               # 正常签到
  python wb_daily_checkin.py --dry-run     # 仅查看状态
  python wb_daily_checkin.py --json        # 输出 JSON 结果

注意:
  - 需要在当前 Windows 用户环境下运行（依赖 DPAPI 解密）
  - 需要先通过 WorkBuddy 客户端完成至少一次登录
  - Token 有过期时间，过期后需重新登录客户端刷新
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅查询签到状态，不实际执行签到",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="输出 JSON 格式结果",
    )

    args = parser.parse_args()
    result = run(dry_run=args.dry_run, json_output=args.json_output)

    # 退出码：0=成功/已签过，1=错误
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
