"""
네이버 예약 자리 모니터 — GitHub Actions 클라우드 버전
monitors.json 파일에서 설정 읽기 (enabled 필드로 항목별 ON/OFF)
환경변수: NTFY_TOPIC (선택, monitors.json 값 override)
          CHECK_INTERVAL_SEC, LOOP_HOURS

monitors.json 항목 선택 필드:
  booking_open_datetime  예약 오픈 일시 (ISO 형식, 예: "2026-06-01T20:00:00+09:00")
                         설정 시 해당 시각 이후 + 자리 있을 때만 알림 발송
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=schedule"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://m.booking.naver.com/",
}


GITHUB_RAW_URL = "https://raw.githubusercontent.com/Gohyedeok/naver-booking-monitor/main/monitors.json"


def load_monitors(from_github: bool = False) -> dict:
    if from_github:
        try:
            resp = requests.get(GITHUB_RAW_URL, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"[경고] GitHub에서 monitors.json 읽기 실패, 로컬 파일 사용: {exc}", flush=True)
    path = Path(__file__).parent / "monitors.json"
    return json.loads(path.read_text(encoding="utf-8"))


def parse_naver_url(url: str) -> dict | None:
    m = re.search(r"/booking/(\d+)/bizes/(\d+)/items/(\d+)", url)
    if not m:
        return None
    return {"service_id": int(m.group(1)), "biz_id": m.group(2), "item_id": m.group(3)}


def check_availability(biz_id: str, item_id: str, service_id: int, target_dates: list) -> dict | None:
    today = datetime.now(timezone(timedelta(hours=9)))
    schedule_params = {
        "businessId": biz_id,
        "bizItemId": item_id,
        "businessTypeId": service_id,
        "startDateTime": today.strftime("%Y-%m-%dT00:00:00+09:00"),
        "endDateTime": (today + timedelta(days=90)).strftime("%Y-%m-%dT23:59:59+09:00"),
        "partitionDays": 42,
    }

    def _post(query: str) -> requests.Response:
        return requests.post(
            GRAPHQL_URL,
            json={"operationName": "schedule", "variables": {"scheduleParams": schedule_params}, "query": query},
            headers=HEADERS,
            timeout=15,
        )

    # 예약 오픈 일시(saleStartDate/saleEndDate) 포함 쿼리를 먼저 시도
    enhanced_query = (
        "query schedule($scheduleParams: ScheduleParams) {"
        "  schedule(input: $scheduleParams) {"
        "    bizItemSchedule { saleStartDate saleEndDate daily { date summary {"
        "      dateKey stock bookingCount hasBookableSlots isSaleDay __typename"
        "    } __typename } __typename } __typename } }"
    )
    # 서버가 알 수 없는 필드를 거부할 경우 기존 쿼리로 폴백
    base_query = (
        "query schedule($scheduleParams: ScheduleParams) {"
        "  schedule(input: $scheduleParams) {"
        "    bizItemSchedule { daily { date summary {"
        "      dateKey stock bookingCount hasBookableSlots isSaleDay __typename"
        "    } __typename } __typename } __typename } }"
    )

    for query, has_window in [(enhanced_query, True), (base_query, False)]:
        try:
            resp = _post(query)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                continue
            sched = data["data"]["schedule"]["bizItemSchedule"]
            summary = sched["daily"]["summary"]
            days = (
                [d for d in summary if d["dateKey"] in target_dates]
                if target_dates
                else [d for d in summary if d["isSaleDay"]]
            )
            return {
                "days": days,
                "sale_start_date": sched.get("saleStartDate") if has_window else None,
                "sale_end_date": sched.get("saleEndDate") if has_window else None,
            }
        except Exception:
            continue

    print("  [오류] API 요청 실패", flush=True)
    return None


def _parse_dt(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        return dt
    except ValueError:
        return None


def booking_window_status(item: dict, sale_start_date: str | None, sale_end_date: str | None) -> tuple[bool, str]:
    """(is_open, reason) 반환. is_open=True 이면 지금 예약 가능한 상태."""
    now = datetime.now(timezone(timedelta(hours=9)))

    # monitors.json의 수동 설정이 우선
    manual_open = _parse_dt(item.get("booking_open_datetime"))
    if manual_open and now < manual_open:
        return False, f"예약 오픈 전 ({manual_open.strftime('%m/%d %H:%M')} 오픈)"

    # API에서 받은 판매 기간
    api_start = _parse_dt(sale_start_date)
    api_end = _parse_dt(sale_end_date)

    if api_start and now < api_start:
        return False, f"예약 오픈 전 ({api_start.strftime('%m/%d %H:%M')} 오픈)"
    if api_end and now > api_end:
        return False, "예약 기간 종료"

    return True, ""


def send_ntfy(topic: str, title: str, body: str, url: str) -> None:
    try:
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
        print("  → ntfy 전송 완료", flush=True)
    except Exception as exc:
        print(f"  [ntfy 오류] {exc}", flush=True)


def check_all(monitors: list, ntfy_topic: str, alerted: set) -> None:
    now_str = datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M:%S")
    active = [m for m in monitors if m.get("enabled", True)]

    for item in active:
        name = item.get("name", "?")
        url = item.get("url", "")
        target_dates = item.get("target_dates", [])

        parsed = parse_naver_url(url)
        if not parsed:
            print(f"[{now_str}] URL 파싱 실패: {name}", flush=True)
            continue

        result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], target_dates)
        if result is None:
            print(f"[{now_str}] {name} — API 실패", flush=True)
            continue

        days = result["days"]
        if not days:
            print(f"[{now_str}] — {name} 체크 완료 (판매 중인 날짜 없음)", flush=True)
            continue

        window_open, window_reason = booking_window_status(item, result["sale_start_date"], result["sale_end_date"])

        for d in days:
            datekey = d["dateKey"]
            alert_key = f"{item.get('id', name)}:{datekey}"
            weekdays = ["월", "화", "수", "목", "금", "토", "일"]
            dow = weekdays[date.fromisoformat(datekey).weekday()]
            date_str = f"{datekey[5:]}({dow})"

            if d["hasBookableSlots"]:
                if window_open:
                    body = f"{name} {date_str} 예약 가능! (재고:{d['stock']} / 예약:{d['bookingCount']})"
                    print(f"[{now_str}] 🎉 {body}", flush=True)
                    if alert_key not in alerted:
                        if ntfy_topic:
                            send_ntfy(ntfy_topic, f"🎉 {name} 예약 자리 생겼어요!", body, url)
                        alerted.add(alert_key)
                else:
                    # 자리는 있지만 예약창이 아직 열리지 않음 — alerted에 추가하지 않아
                    # 예약창이 열리는 순간 다음 체크에서 즉시 알림이 발송됨
                    print(f"[{now_str}] ⏳ {name} {date_str} 자리 있음 · {window_reason}", flush=True)
            else:
                alerted.discard(alert_key)
                print(f"[{now_str}] ❌ {name} {date_str} 매진 (재고:{d['stock']} / 예약:{d['bookingCount']})", flush=True)


def main():
    cfg = load_monitors()
    ntfy_topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    interval = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
    loop_hours = float(os.environ.get("LOOP_HOURS", "5.5"))
    monitors = cfg.get("monitors", [])

    active = [m for m in monitors if m.get("enabled", True)]
    if not active:
        print("활성화된 모니터링 항목 없음", flush=True)
        sys.exit(0)

    print(f"=== 모니터 시작 | 주기: {interval}초 | 최대: {loop_hours}시간 ===", flush=True)
    for m in active:
        dates = ", ".join(m.get("target_dates") or ["전체"])
        print(f"  • {m['name']} [{dates}]", flush=True)

    alerted: set = set()
    end_time = time.time() + loop_hours * 3600
    iteration = 0

    while time.time() < end_time:
        iteration += 1
        # 매 회차마다 GitHub에서 최신 monitors.json 읽기 → 웹에서 수정 시 3분 내 반영
        try:
            cfg = load_monitors(from_github=True)
            monitors = cfg.get("monitors", [])
            ntfy_topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
        except Exception as exc:
            print(f"[경고] monitors.json 읽기 실패, 이전 설정 유지: {exc}", flush=True)

        remaining_min = (end_time - time.time()) / 60
        print(f"--- [{iteration}회차] 남은 시간: {remaining_min:.1f}분 ---", flush=True)
        try:
            check_all(monitors, ntfy_topic, alerted)
        except Exception as exc:
            print(f"[오류] check_all 예외: {exc}", flush=True)

        remaining = end_time - time.time()
        if remaining > interval:
            time.sleep(interval)
        else:
            break

    print("=== 루프 종료 ===", flush=True)


if __name__ == "__main__":
    main()
