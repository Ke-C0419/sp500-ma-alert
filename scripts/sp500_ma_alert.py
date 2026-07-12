#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPX (标普500) 150MA 突破监控告警

逻辑:
  - 工作日盘后 (UTC 22:00, 美股收盘后约1小时): 拉取 SPX 日线, 计算今日收盘 vs 150MA
    * 若与上次状态不同 -> 推送微信告警
    * 若状态不变 -> 不推送 (避免噪音)
  - 周六心跳 (UTC 02:00): 固定推送一次, 确认服务存活 + 当前状态

状态持久化: state/sp500_state.json (在 GitHub Actions 中用 actions/cache 持久化)

用法:
  python scripts/sp500_ma_alert.py            # 自动判断模式 (UTC 周六=心跳, 其它=日常)
  python scripts/sp500_ma_alert.py daily      # 强制日常模式
  python scripts/sp500_ma_alert.py heartbeat  # 强制心跳模式
"""

import os
import sys
import json
import datetime
from pathlib import Path

# ─── 配置 ────────────────────────────────────────────────────────────────
STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "sp500_state.json"
MA_PERIOD = 150
SYMBOL = "^GSPC"  # yfinance 的标普500代码
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()


# ─── 微信推送 (Server酱, 与 daily_signal.py 同 key) ─────────────────────
def push_serverchan(title: str, content: str) -> bool:
    """通过 Server酱 推送消息到微信"""
    if not SERVERCHAN_KEY:
        print("⚠️  未设置 SERVERCHAN_KEY, 跳过推送")
        return False
    import urllib.request, urllib.parse
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("code") == 0:
                print("  📱 Server酱推送成功")
                return True
            print(f"  ⚠️ Server酱推送失败: {result}")
            return False
    except Exception as e:
        print(f"  ⚠️ Server酱推送异常: {e}")
        return False


# ─── 数据获取 ────────────────────────────────────────────────────────────
def fetch_spx_close_and_ma():
    """返回 (today_close, today_ma150, today_date_str, last_close)"""
    import yfinance as yf
    # period=1y 给足 150 个交易日; auto_adjust=False 取原始收盘价
    hist = yf.Ticker(SYMBOL).history(period="1y", auto_adjust=False)
    if hist is None or len(hist) < MA_PERIOD:
        raise RuntimeError(f"SPX 历史数据不足 {MA_PERIOD} 个交易日, 实际 {len(hist) if hist is not None else 0}")

    close = hist["Close"].dropna()
    if len(close) < MA_PERIOD:
        raise RuntimeError(f"清洗后 SPX 收盘价不足 {MA_PERIOD} 个, 实际 {len(close)}")

    ma150 = close.rolling(MA_PERIOD).mean()
    today_close = float(close.iloc[-1])
    today_ma = float(ma150.iloc[-1])
    last_close = float(close.iloc[-2]) if len(close) >= 2 else None
    today_date_str = str(close.index[-1].date())  # 用交易日期, 比 UTC 更准
    return today_close, today_ma, today_date_str, last_close


# ─── 状态文件 ────────────────────────────────────────────────────────────
def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  状态文件读取失败, 视为无历史: {e}")
        return None


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📝 状态已保存到 {STATE_FILE}")


# ─── 主流程 ──────────────────────────────────────────────────────────────
def run_daily(today_close: float, today_ma: float, today_date_str: str, last_state: dict | None) -> dict:
    """日常模式: 状态变化才推送"""
    today_state = "above" if today_close > today_ma else "below"
    print(f"📊 今日({today_date_str}) SPX 收盘 {today_close:.2f}, 150MA {today_ma:.2f}, 状态={today_state}")

    last_state_str = last_state.get("state") if last_state else None
    last_state_date = last_state.get("date") if last_state else None

    if last_state_str is None:
        # 首次运行 / 状态丢失: 不告警, 只初始化
        print("🆕 无历史状态, 视为初始化, 不告警")
        # 计算已持续天数 (粗略): 从今天开始计 1
        consecutive_days = 1
    elif last_state_str == today_state:
        # 状态未变, 不推送
        consecutive_days = int(last_state.get("consecutive_days", 0)) + 1
        print(f"✅ 状态未变 (上次 {last_state_date} = {last_state_str}), 持续 {consecutive_days} 个交易日, 不推送")
    else:
        # 状态变化! 推送告警
        consecutive_days = 1
        arrow = "站上 → 跌破" if last_state_str == "above" else "跌破 → 站上"
        title = f"⚠️ SPX 150MA 状态变化: {arrow}"
        content = (
            f"## 标普500 150MA 突破告警\n\n"
            f"**交易日**: {today_date_str}\n\n"
            f"**今日收盘**: {today_close:.2f}\n\n"
            f"**150MA**: {today_ma:.2f}\n\n"
            f"**状态变化**: {arrow}\n\n"
            f"**上次状态日期**: {last_state_date}\n\n"
            f"---\n_GitHub Actions 自动告警_"
        )
        print(f"🚨 状态变化! {arrow}, 推送告警")
        push_serverchan(title, content)

    return {
        "state": today_state,
        "date": today_date_str,
        "close": today_close,
        "ma150": today_ma,
        "consecutive_days": consecutive_days,
    }


def run_heartbeat(today_close: float, today_ma: float, today_date_str: str, last_state: dict | None) -> dict:
    """心跳模式: 固定推送"""
    today_state = "above" if today_close > today_ma else "below"
    consecutive_days = (
        int(last_state.get("consecutive_days", 0)) + 1
        if last_state and last_state.get("state") == today_state
        else 1
    )
    state_text = "站上150MA" if today_state == "above" else "跌破150MA"

    title = "✅ SPX 150MA 监控服务存活 (周六心跳)"
    content = (
        f"## 周六心跳 - 服务正常\n\n"
        f"**最近交易日**: {today_date_str}\n\n"
        f"**收盘**: {today_close:.2f}\n\n"
        f"**150MA**: {today_ma:.2f}\n\n"
        f"**当前状态**: {state_text} (已持续 {consecutive_days} 个交易日)\n\n"
        f"---\n_GitHub Actions 周六心跳, 收到本消息说明服务没崩_"
    )
    print(f"💓 周六心跳, 推送: {state_text}, 持续 {consecutive_days} 个交易日")
    push_serverchan(title, content)

    return {
        "state": today_state,
        "date": today_date_str,
        "close": today_close,
        "ma150": today_ma,
        "consecutive_days": consecutive_days,
    }


def main():
    # 模式判断: 命令行参数 > 自动判断 (UTC 周六=心跳)
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode not in ("daily", "heartbeat"):
        today_utc = datetime.datetime.utcnow().date()
        mode = "heartbeat" if today_utc.weekday() == 5 else "daily"
        print(f"🕐 自动判断模式: UTC={today_utc} (weekday={today_utc.weekday()}), mode={mode}")

    print(f"🚀 SPX 150MA 监控 - 模式: {mode}")

    # 拉数据
    today_close, today_ma, today_date_str, _ = fetch_spx_close_and_ma()
    last_state = load_state()

    # 执行
    if mode == "daily":
        new_state = run_daily(today_close, today_ma, today_date_str, last_state)
    else:
        new_state = run_heartbeat(today_close, today_ma, today_date_str, last_state)

    # 持久化
    save_state(new_state)
    print("✅ 完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"❌ 执行失败: {e}")
        traceback.print_exc()
        # 失败也推送告警, 让用户知道服务出问题了
        push_serverchan("❌ SPX 150MA 监控执行失败", f"```\n{traceback.format_exc()}\n```")
        sys.exit(1)
