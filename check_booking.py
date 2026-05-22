"""
네이버 예약 자리 모니터 — GitHub Actions 클라우드 버전
monitors.json 파일에서 설정 읽기 (enabled 필드로 항목별 ON/OFF)
환경변수: NTFY_TOPIC (선택, monitors.json 값 override)
          CHECK_INTERVAL_SEC, LOOP_HOURS
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=schedule"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://m.booking.naver.com/",
}


def load_monitors() -> dict:
    path = Path(__file__).parent / "monitors.json"
    return json.loads(path.read_text(encoding="utf-8"))


def parse_naver_url(url: str) -> dict | None:
    m = re.search(r"/booking/(\d+)/bizes/(\d+)/items/(\d+)", url)
    if not m:
        return None
    return {"service_id": int(m.group(1)), "biz_id": m.group(2), "item_id": m.group(3)}


def check_availability(biz_id: str, item_id: str, service_id: int, target_dates: list) -> list | None:
    today = datetime.now()
    payload = {
        "operationName": "schedule",
        "variables": {
            "scheduleParams": {
                "businessId": biz_id,
                "bizItemId": item_id,
                "businessTypeId": service_id,
                "startDateTime": today.strftime("%Y-%m-%dT00:00:00+09:00"),
                "endDateTime": (today + timedelta(days=90)).strftime("%Y-%m-%dT23:59:59+09:00"),
                "partitionDays": 42,
            }
        },
        "query": (
            "query schedule($scheduleParams: ScheduleParams) {"
            "  schedule(input: $scheduleParams) {"
            "    bizItemSchedule { daily { date summary {"
            "      dateKey stock bookingCount hasBookableSlots isSaleDay __typename"
            "    } __typename } __typename } __typename } }"
        ),
    }
    try:
        resp = requests.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        summary = resp.json()["data"]["schedule"]["bizItemSchedule"]["daily"]["summary"]
        if target_dates:
            return [d for d in summary if d["dateKey"] in target_dates]
        return [d for d in summary if d["isSaleDay"]]
    except Exception as exc:
        print(f"  [오류] {exc}")
        return None


def send_ntfy(topic: str, title: str, body: str, url: str) -> None:
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": "urgent",
            "Click": url,
            "Tags": "bell",
        },
        timeout=10,
    )
    print(f"  → ntfy 전송 완료")


def check_all(monitors: list, ntfy_topic: str, alerted: set) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    active = [m for m in monitors if m.get("enabled", True)]

    for item in active:
        name = item.get("name", "?")
        url = item.get("url", "")
        target_dates = item.get("target_dates", [])

        parsed = parse_naver_url(url)
        if not parsed:
            print(f"[{now}] URL 파싱 실패: {name}")
            continue

        days = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], target_dates)
        if days is None:
            print(f"[{now}] {name} — API 실패")
            continue

        for d in days:
            date = d["dateKey"]
            alert_key = f"{item.get('id', name)}:{date}"
            if d["hasBookableSlots"]:
                body = f"{name} {date[5:]} 예약 가능! (재고:{d['stock']} / 예약:{d['bookingCount']})"
                print(f"[{now}] 🎉 {body}")
                if alert_key not in alerted:
                    if ntfy_topic:
                        send_ntfy(ntfy_topic, "🎉 네이버 예약 자리 생겼어요!", body, url)
                    alerted.add(alert_key)
            else:
                alerted.discard(alert_key)
                print(f"[{now}] ❌ {name} {date[5:]} 매진 (재고:{d['stock']} / 예약:{d['bookingCount']})")


def main():
    cfg = load_monitors()
    ntfy_topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    interval = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
    loop_hours = float(os.environ.get("LOOP_HOURS", "5.5"))
    monitors = cfg.get("monitors", [])

    active = [m for m in monitors if m.get("enabled", True)]
    if not active:
        print("활성화된 모니터링 항목 없음")
        sys.exit(0)

    print(f"=== 모니터 시작 | 주기: {interval}초 | 최대: {loop_hours}시간 ===")
    for m in active:
        dates = ", ".join(m.get("target_dates") or ["전체"])
        print(f"  • {m['name']} [{dates}]")

    alerted: set = set()
    end_time = time.time() + loop_hours * 3600

    while time.time() < end_time:
        check_all(monitors, ntfy_topic, alerted)
        remaining = end_time - time.time()
        if remaining > interval:
            time.sleep(interval)
        else:
            break

    print("=== 루프 종료 ===")


if __name__ == "__main__":
    main()
