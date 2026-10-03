#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPX (标普500) 150MA 突破监控告警

逻辑:
  - 工作日盘后 (UTC 22:00, 美股收盘后约1小时): 拉取 SPX 日线, 计算今日收盘 vs 150MA
    * 若与上次状态不同 -> 推送微信告警
    * 若状态不变 -> 不推送 (避免噪音)
  - 周末心跳 (周六/周日 UTC 02:00, 各一次): 固定推送一次, 确认服务存活 + 当前状态
    * 模式判定不依赖触发时刻的 wall-clock, 而是依据 workflow 透传的
      github.event.schedule (见 INPUT_MODE / HEARTBEAT_CRON), 避免 schedule
      触发延迟漂移导致心跳被误判成 daily 而漏推

状态持久化: state/sp500_state.json (在 GitHub Actions 中用 actions/cache 持久化)
连续天数: 从 K 线回溯计算, 不依赖状态文件, 即使 cache 丢失也能给出真实值

用法:
  python scripts/sp500_ma_alert.py            # 依据 INPUT_MODE 环境变量判断模式
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
def fetch_spx_data():
    """返回 (today_close, today_ma, today_date_str, last_close, close_series, ma_series)"""
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
    return today_close, today_ma, today_date_str, last_close, close, ma150


def compute_run_stats(close, ma) -> tuple[int, str | None]:
    """从 K 线回溯计算当前状态已持续多少个交易日, 以及本段持续期的起始日期

    不依赖状态文件, 即使 cache 丢失也能给出真实值.

    Returns:
        (consecutive_days, run_start_date)
        - consecutive_days: 当前状态 (above/below) 已连续保持的交易日数 (含今天)
        - run_start_date: 本段持续期第一天 (即上次翻转当日), 'YYYY-MM-DD'
                          若 K 线全程都是同一状态, 返回最早一个有 MA 的日期
    """
    import pandas as pd
    n = len(close)
    if n == 0:
        return 1, None

    today_state = "above" if close.iloc[-1] > ma.iloc[-1] else "below"

    count = 0
    run_start_date = None
    for i in range(n - 1, -1, -1):
        if pd.isna(ma.iloc[i]):
            break
        state_i = "above" if close.iloc[i] > ma.iloc[i] else "below"
        if state_i == today_state:
            count += 1
            run_start_date = str(close.index[i].date())
        else:
            break

    if count == 0:
        count = 1
        run_start_date = str(close.index[-1].date())
    return count, run_start_date


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
def run_daily(today_close: float, today_ma: float, today_date_str: str,
              last_state: dict | None, consecutive_days: int, run_start_date: str | None) -> dict:
    """日常模式: 状态变化才推送"""
    today_state = "above" if today_close > today_ma else "below"
    print(f"📊 今日({today_date_str}) SPX 收盘 {today_close:.2f}, 150MA {today_ma:.2f}, "
          f"状态={today_state}, 持续 {consecutive_days} 个交易日 (自 {run_start_date} 起)")

    last_state_str = last_state.get("state") if last_state else None
    last_state_date = last_state.get("date") if last_state else None

    if last_state_str is None:
        # 首次运行 / 状态丢失: 不告警, 只初始化
        print("🆕 无历史状态, 视为初始化, 不告警")
    elif last_state_str == today_state:
        # 状态未变, 不推送
        print(f"✅ 状态未变 (上次 {last_state_date} = {last_state_str}), 不推送")
    else:
        # 状态变化! 推送告警
        arrow = "站上 → 跌破" if last_state_str == "above" else "跌破 → 站上"
        title = f"⚠️ SPX 150MA 状态变化: {arrow}"
        content = (
            f"## 标普500 150MA 突破告警\n\n"
            f"**交易日**: {today_date_str}\n\n"
            f"**今日收盘**: {today_close:.2f}\n\n"
            f"**150MA**: {today_ma:.2f}\n\n"
            f"**状态变化**: {arrow}\n\n"
            f"**当前持续**: {consecutive_days} 个交易日 (自 {run_start_date} 起)\n\n"
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
        "run_start_date": run_start_date,
    }


def run_heartbeat(today_close: float, today_ma: float, today_date_str: str,
                  last_state: dict | None, consecutive_days: int, run_start_date: str | None) -> dict:
    """心跳模式: 固定推送"""
    today_state = "above" if today_close > today_ma else "below"
    state_text = "站上150MA" if today_state == "above" else "跌破150MA"

    title = "✅ SPX 150MA 监控服务存活 (心跳)"
    content = (
        f"## 心跳 - 服务正常\n\n"
        f"**最近交易日**: {today_date_str}\n\n"
        f"**收盘**: {today_close:.2f}\n\n"
        f"**150MA**: {today_ma:.2f}\n\n"
        f"**当前状态**: {state_text} (已持续 {consecutive_days} 个交易日, 自 {run_start_date} 起)\n\n"
        f"---\n_GitHub Actions 心跳, 收到本消息说明服务没崩_"
    )
    print(f"💓 心跳, 推送: {state_text}, 持续 {consecutive_days} 个交易日 (自 {run_start_date} 起)")
    push_serverchan(title, content)

    return {
        "state": today_state,
        "date": today_date_str,
        "close": today_close,
        "ma150": today_ma,
        "consecutive_days": consecutive_days,
        "run_start_date": run_start_date,
    }


def main():
    # 模式判断 (按可靠性从高到低):
    #   1) 命令行参数 (手动触发 / 调试)
    #   2) INPUT_MODE 环境变量: workflow 把 github.event.schedule 原样透传,
    #      与 HEARTBEAT_CRON 中任一条精确匹配 => 心跳
    #   3) 兜底: 无任何输入时按 UTC 周六判断
    # 注意: 不能用 utcnow() 直接判断模式 —— GitHub schedule 触发会漂移,
    #      触发时刻可能滑到周日凌晨, 导致周六心跳被误判成 daily 而不推送.
    heartbeat_crons = [c.strip() for c in os.environ.get("HEARTBEAT_CRON", "0 2 * * 6").split(",") if c.strip()]
    raw_input = os.environ.get("INPUT_MODE", "").strip()

    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode not in ("daily", "heartbeat"):
        if raw_input:
            mode = "heartbeat" if raw_input in heartbeat_crons else "daily"
            print(f"🕐 依据触发 cron '{raw_input}' 判定模式: {mode}")
        else:
            today_utc = datetime.datetime.utcnow().date()
            mode = "heartbeat" if today_utc.weekday() == 5 else "daily"
            print(f"🕐 兜底判断模式: UTC={today_utc} (weekday={today_utc.weekday()}), mode={mode}")

    print(f"🚀 SPX 150MA 监控 - 模式: {mode}")

    # 拉数据 + 从 K 线算真实持续天数 (不依赖 cache)
    today_close, today_ma, today_date_str, _, close, ma = fetch_spx_data()
    consecutive_days, run_start_date = compute_run_stats(close, ma)
    last_state = load_state()

    # 执行
    if mode == "daily":
        new_state = run_daily(today_close, today_ma, today_date_str, last_state,
                              consecutive_days, run_start_date)
    else:
        new_state = run_heartbeat(today_close, today_ma, today_date_str, last_state,
                                  consecutive_days, run_start_date)

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
