"""
네이버 예약 자리 모니터 — GitHub Actions 클라우드 버전
환경변수: NTFY_TOPIC, MONITORS_JSON
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta

import requests

GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=schedule"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://m.booking.naver.com/",
}


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
        print(f"[오류] {exc}")
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
    print(f"  → ntfy 전송: {title}")


def main():
    ntfy_topic = os.environ.get("NTFY_TOPIC", "")
    monitors_json = os.environ.get("MONITORS_JSON", "[]")
    monitors = json.loads(monitors_json)

    if not monitors:
        print("모니터링 항목 없음 (MONITORS_JSON 환경변수 확인)")
        sys.exit(0)

    found_any = False

    for item in monitors:
        name = item.get("name", "?")
        url = item.get("url", "")
        target_dates = item.get("target_dates", [])

        parsed = parse_naver_url(url)
        if not parsed:
            print(f"URL 파싱 실패: {name}")
            continue

        days = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], target_dates)
        if days is None:
            print(f"{name} — API 실패")
            continue

        for d in days:
            date = d["dateKey"]
            if d["hasBookableSlots"]:
                body = f"{name} {date[5:]} 예약 가능! (재고:{d['stock']} / 예약:{d['bookingCount']})"
                print(f"🎉 {body}")
                found_any = True
                if ntfy_topic:
                    send_ntfy(ntfy_topic, "🎉 네이버 예약 자리 생겼어요!", body, url)
            else:
                print(f"❌ {name} {date[5:]} 매진 (재고:{d['stock']} / 예약:{d['bookingCount']})")

    sys.exit(0)


if __name__ == "__main__":
    main()
