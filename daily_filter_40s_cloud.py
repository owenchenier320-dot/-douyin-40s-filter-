#!/usr/bin/env python3
"""
抖音热梗 <=40秒短视频 自动筛选 + 飞书Webhook推送
- 数据源: tophub.today (主榜 + 聚合页)
- 时长检查: iesdouyin.com 分享页 (含WAF绕过)
- 推送: 飞书自定义机器人 Webhook (纯HTTP, 无需lark-cli)
- 纯Python标准库, 可在GitHub Actions / 任意云平台运行
"""

import urllib.request, urllib.parse, urllib.error
import re, hashlib, base64, json, time, os, sys
from datetime import datetime, timezone, timedelta

# === 配置 ===
TOPHUB_MAIN_URL = "https://tophub.today/n/DpQvNABoNE"
TOPHUB_AGGREGATE_URL = "https://tophub.today/c/news?p=1&q=%E6%8A%96%E9%9F%B3"
MAX_DURATION = 40
TARGET_COUNT = 20
MAX_CHECK = 120
UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

BEIJ_TZ = timezone(timedelta(hours=8))


# ===== WAF 绕过 =====
def fix_b64(s):
    """修复 base64 padding"""
    padding = 4 - (len(s) % 4)
    if padding != 4:
        s += "=" * padding
    return s


def solve_waf_challenge(html):
    """解析 WAF 挑战并求解 SHA-256 PoW"""
    match = re.search(r'cs="([^"]+)"', html)
    if not match:
        return None
    cs = match.group(1)
    try:
        c = json.loads(base64.b64decode(fix_b64(cs)).decode())
    except Exception:
        return None
    try:
        prefix = base64.b64decode(fix_b64(c["v"]["a"]))
        expected_hex = base64.b64decode(fix_b64(c["v"]["c"])).hex()
    except Exception:
        return None
    for i in range(1000001):
        h = hashlib.sha256(prefix + str(i).encode()).hexdigest()
        if h == expected_hex:
            c["d"] = base64.b64encode(str(i).encode()).decode()
            cookie_value = base64.b64encode(json.dumps(c).encode()).decode()
            return f"_wafchallengeid={cookie_value}"
    return None


def fetch_with_waf(url, max_retries=3):
    """获取页面, 自动处理 WAF 挑战"""
    cookie = ""
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_IPHONE,
            "Cookie": cookie,
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            if "wafchallengeid" in html and len(html) < 5000:
                cookie = solve_waf_challenge(html)
                if cookie:
                    continue
                else:
                    return None
            else:
                return html
        except Exception:
            return None
    return None


def fetch_html(url, ua=None):
    """普通 HTML 抓取"""
    req = urllib.request.Request(url, headers={"User-Agent": ua or UA_DESKTOP})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


# ===== tophub.today 抓取 =====
def extract_vids_from_html(html):
    """从 HTML 中提取所有抖音视频ID和标题"""
    pattern = r'douyin\.com/video/(\d{15,})'
    vids = re.findall(pattern, html)
    seen = set()
    items = []
    for vid in vids:
        if vid not in seen:
            seen.add(vid)
            title_pattern = f'video/{vid}"[^>]*>([^<]+)'
            title_match = re.search(title_pattern, html)
            title = title_match.group(1).strip() if title_match else ""
            items.append((vid, title))
    return items


def fetch_tophub_main():
    """抓取 tophub 抖音主榜"""
    html = fetch_html(TOPHUB_MAIN_URL)
    if not html:
        print("  [WARN] tophub main list fetch failed")
        return []
    items = extract_vids_from_html(html)
    print(f"  Main list: {len(items)} video IDs")
    return items


def fetch_tophub_aggregate():
    """抓取 tophub 聚合页 (多个分类榜单)"""
    html = fetch_html(TOPHUB_AGGREGATE_URL)
    if not html:
        print("  [WARN] tophub aggregate fetch failed")
        return []
    items = extract_vids_from_html(html)
    print(f"  Aggregate: {len(items)} video IDs")
    return items


# ===== 时长检查 =====
def get_video_duration(vid):
    """通过 iesdouyin 分享页获取视频时长(秒)、标题、作者"""
    url = f"https://www.iesdouyin.com/share/video/{vid}/"
    html = fetch_with_waf(url)
    if not html:
        return None, None, None
    durations = re.findall(r'"duration":\s*(\d+)', html)
    if durations:
        durations = [int(d) for d in durations]
        max_dur = max(durations)
        secs = max_dur / 1000.0 if max_dur > 10000 else float(max_dur)
    else:
        return None, None, None
    title_match = re.search(r'"desc":\s*"([^"]*)"', html)
    title = title_match.group(1) if title_match else ""
    author_match = re.search(r'"nickname":\s*"([^"]*)"', html)
    author = author_match.group(1) if author_match else ""
    return secs, title, author


# ===== 飞书 Webhook 推送 =====
def send_to_feishu_webhook(results, now):
    """通过飞书自定义机器人 Webhook 发送消息"""
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print("  [FEISHU] FEISHU_WEBHOOK_URL not set, skipping")
        return False

    # 构建消息文本
    lines = []
    lines.append(f"抖音热梗 <=40秒短视频 ({len(results)}条)")
    lines.append(f"{now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 30)
    for i, (title, author, vid, dur) in enumerate(results):
        m = dur // 60
        s = dur % 60
        url = f"https://www.douyin.com/video/{vid}"
        lines.append(f"\n{i+1}. [{m}:{s:02d}] {title}")
        if author and author != "未知":
            lines.append(f"   @{author}")
        lines.append(f"   {url}")

    text_content = "\n".join(lines)

    # Feishu webhook text message
    payload = json.dumps({
        "msg_type": "text",
        "content": {
            "text": text_content
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            if resp_data.get("code") == 0 or resp_data.get("StatusCode") == 0:
                print("  [FEISHU] Webhook message sent successfully")
                return True
            else:
                print(f"  [FEISHU] Webhook error: {resp_data}")
                return False
    except Exception as e:
        print(f"  [FEISHU] Webhook send failed: {e}")
        return False


def send_error_to_feishu(error_msg, now):
    """发送错误通知到飞书"""
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        return
    text = f"抖音热梗筛选出错\n{now.strftime('%Y-%m-%d %H:%M')}\n\n{error_msg}"
    payload = json.dumps({
        "msg_type": "text",
        "content": {"text": text}
    }).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


# ===== 主流程 =====
def main():
    now = datetime.now(BEIJ_TZ)
    print("=" * 60)
    print(f"Douyin Hot <= {MAX_DURATION}s Filter + Feishu Webhook")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} (Beijing)")
    print("=" * 60)

    # 1. 抓取视频ID
    print("\n[1/4] Fetching video IDs...")
    main_items = fetch_tophub_main()
    agg_items = fetch_tophub_aggregate()

    all_vids = {}
    for vid, title in main_items + agg_items:
        if vid not in all_vids:
            all_vids[vid] = title
    print(f"  Total unique: {len(all_vids)} video IDs")

    if not all_vids:
        print("[ERROR] No video IDs found")
        send_error_to_feishu("No video IDs fetched from tophub.today", now)
        return 1

    # 2. 检查时长
    print(f"\n[2/4] Checking durations (target: {TARGET_COUNT} videos <= {MAX_DURATION}s)...")
    results = []
    checked = set()

    check_list = list(all_vids.items())

    def priority_key(item):
        vid, title = item
        keywords = ["搞笑", "萌", "cos", "反转", "哭", "笑", "挑战", "吓", "贱",
                     "猜", "揭", "魔术", "宠物", "狗", "猫", "娃", "短"]
        return 0 if any(k in title for k in keywords) else 1

    check_list.sort(key=priority_key)

    checked_count = 0
    for vid, fallback_title in check_list:
        if len(results) >= TARGET_COUNT:
            break
        if checked_count >= MAX_CHECK:
            print(f"  Reached max check limit {MAX_CHECK}, stopping")
            break
        if vid in checked:
            continue
        checked.add(vid)
        checked_count += 1

        secs, title, author = get_video_duration(vid)

        if secs is not None:
            m = int(secs) // 60
            s = int(secs) % 60
            tag = "OK" if secs <= MAX_DURATION else "skip"
            display_title = (title or fallback_title)[:30]
            print(f"  [{len(results)+1}/{TARGET_COUNT}] ({checked_count}) {m}:{s:02d} {tag} | {display_title}")

            if secs <= MAX_DURATION:
                results.append((title or fallback_title, author or "未知", vid, int(secs)))
        else:
            print(f"  [{len(results)+1}/{TARGET_COUNT}] ({checked_count}) -- FAIL | {fallback_title[:30]}")

        time.sleep(1.5)

    # 3. 排序
    results.sort(key=lambda x: x[3])

    print(f"\n[3/4] Results: {len(results)}/{TARGET_COUNT} videos <= {MAX_DURATION}s (checked {checked_count})")

    # 4. 发送飞书
    print(f"\n[4/4] Sending to Feishu via webhook...")
    if results:
        send_to_feishu_webhook(results, now)
    else:
        send_error_to_feishu("No videos <= 40s found", now)

    # 总结
    print(f"\n{'=' * 60}")
    print(f"Done! Found {len(results)} videos <= {MAX_DURATION}s")
    print(f"{'=' * 60}")
    for i, (title, author, vid, dur) in enumerate(results):
        m = dur // 60
        s = dur % 60
        print(f"  {i+1}. {m}:{s:02d} | {title[:35]} | {vid}")

    return 0 if len(results) >= TARGET_COUNT else 1


if __name__ == "__main__":
    sys.exit(main())
