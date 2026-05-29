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
                "_all_summary": summary,  # 디버그용
            }
        except Exception:
            continue

    print("  [오류] API 요청 실패", flush=True)
    return None


def fetch_slots(biz_id: str, item_id: str, service_id: int, target_date: str) -> dict:
    """
    hourlySchedule API로 시간대별 슬롯 조회. 이미 지난 시간대는 제외.
      times   : 예약 가능한 미래 시간대 목록 (HH:MM)
      total   : 미래 슬롯 수 (지난 슬롯 제외, 가용 여부 무관)
      queried : API 호출 성공 여부
    """
    KST = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST)

    try:
        resp = requests.post(
            "https://m.booking.naver.com/graphql?opName=hourlySchedule",
            json={
                "operationName": "hourlySchedule",
                "variables": {
                    "scheduleParams": {
                        "businessId": biz_id,
                        "businessTypeId": service_id,
                        "bizItemId": item_id,
                        "startDateTime": f"{target_date}T00:00:00+09:00",
                        "endDateTime": f"{target_date}T00:00:00+09:00",
                    }
                },
                "query": (
                    "query hourlySchedule($scheduleParams: ScheduleParams) {"
                    "  schedule(input: $scheduleParams) {"
                    "    bizItemSchedule {"
                    "      hourly {"
                    "        unitStartTime unitBookingCount unitStock isUnitSaleDay __typename"
                    "      } __typename"
                    "    } __typename"
                    "  }"
                    "}"
                ),
            },
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return {"times": [], "total": 0, "queried": False}

        hourly = data["data"]["schedule"]["bizItemSchedule"].get("hourly") or []

        future_slots = []
        for slot in hourly:
            if not slot.get("isUnitSaleDay"):
                continue
            t_str = slot.get("unitStartTime")  # "2026-05-31 10:00:00" KST
            if t_str:
                try:
                    slot_dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                    if slot_dt <= now_kst:
                        continue
                except ValueError:
                    pass
            future_slots.append(slot)

        available_times = [
            s["unitStartTime"][11:16]
            for s in future_slots
            if s.get("unitStock", 0) - s.get("unitBookingCount", 0) > 0
        ]

        return {"times": available_times, "total": len(future_slots), "queried": True}

    except Exception:
        return {"times": [], "total": 0, "queried": False}


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

    # monitors.json 수동 설정 (API보다 우선)
    manual_open = _parse_dt(item.get("booking_open_datetime"))
    manual_close = _parse_dt(item.get("booking_close_datetime"))

    if manual_open and now < manual_open:
        return False, f"예약 오픈 전 ({manual_open.strftime('%m/%d %H:%M')} 오픈)"
    if manual_close and now > manual_close:
        return False, f"예약 마감 ({manual_close.strftime('%m/%d %H:%M')} 종료)"

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


def check_booking_accessible(url: str) -> bool:
    """예약 URL이 에러 페이지로 리다이렉트되는지 확인. True = 접근 가능."""
    try:
        with requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True, stream=True) as resp:
            return "/error/" not in resp.url
    except Exception:
        return True  # 네트워크 오류 시 '접근 가능'으로 처리 (오알림 방지)


def check_all(monitors: list, ntfy_topic: str, alerted: dict) -> None:
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    now_str = now_kst.strftime("%H:%M:%S")
    today_str = now_kst.strftime("%Y-%m-%d")
    active = [m for m in monitors if m.get("enabled", True)]

    for item in active:
        name = item.get("name", "?")
        url = item.get("url", "")

        # target_dates 파싱:
        #   "2026-05-30"           → 전체 시간
        #   "2026-05-30 10:00-14:00" → 10:00~14:00 범위
        #   "2026-05-30 10:00"     → 10:00 단일 (하위 호환)
        # target_time_map: { date: None(전체) or (t_from, t_to) }
        target_time_map: dict[str, tuple[str, str] | None] = {}
        for entry in item.get("target_dates", []):
            parts = entry.strip().split(" ", 1)
            d_part = parts[0]
            if len(parts) > 1:
                t_str = parts[1]
                if "-" in t_str[3:]:          # "10:00-14:00" 범위 형식
                    t_from, t_to = t_str.split("-", 1)
                else:                          # "10:00" 단일 → 동일 값 범위로 처리
                    t_from = t_to = t_str[:5]
                if d_part not in target_time_map:
                    target_time_map[d_part] = (t_from, t_to)
            else:
                if d_part not in target_time_map:  # 시간 범위 항목이 먼저 있으면 덮어쓰지 않음
                    target_time_map[d_part] = None  # None = 전체 시간
        target_dates_only = list(target_time_map.keys())

        parsed = parse_naver_url(url)
        if not parsed:
            print(f"[{now_str}] URL 파싱 실패: {name}", flush=True)
            continue

        if not check_booking_accessible(url):
            print(f"[{now_str}] 🔒 {name} — 예약창 닫힘 (에러 페이지로 리다이렉트)", flush=True)
            item_prefix = f"{item.get('id', name)}:"
            for k in [k for k in alerted if k.startswith(item_prefix)]:
                alerted.pop(k, None)
            continue

        result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], target_dates_only)
        if result is None:
            print(f"[{now_str}] {name} — API 실패", flush=True)
            continue

        days = result["days"]
        if not days:
            # 디버그: API가 반환한 전체 날짜 출력 (왜 매칭 안 됐는지 확인)
            all_keys = [d["dateKey"] for d in (result.get("_all_summary") or [])]
            hint = f" | API반환날짜: {all_keys[:5]}{'...' if len(all_keys)>5 else ''}" if all_keys else ""
            print(f"[{now_str}] — {name} 체크 완료 (판매 중인 날짜 없음{hint})", flush=True)
            continue

        window_open, window_reason = booking_window_status(item, result["sale_start_date"], result["sale_end_date"])

        for d in days:
            datekey = d["dateKey"]
            alert_key = f"{item.get('id', name)}:{datekey}"
            weekdays = ["월", "화", "수", "목", "금", "토", "일"]
            dow = weekdays[date.fromisoformat(datekey).weekday()]
            date_str = f"{datekey[5:]}({dow})"

            if d["hasBookableSlots"]:
                slot_info = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)

                # 시간 범위 지정된 경우 → 범위 내 슬롯만 필터링
                time_range = target_time_map.get(datekey)  # None=전체, (t_from, t_to)=범위
                if time_range is not None and slot_info["queried"]:
                    t_from, t_to = time_range
                    slot_info = {**slot_info, "times": [t for t in slot_info["times"] if t_from <= t <= t_to]}

                # 오늘 날짜이고 slot 쿼리 성공했는데 미래 슬롯이 하나도 없으면 스킵 (모두 지남)
                if slot_info["queried"] and slot_info["total"] == 0 and datekey == today_str:
                    alerted.pop(alert_key, None)
                    alerted.pop(f"{alert_key}:pre", None)
                    print(f"[{now_str}] ⏭ {name} {date_str} 오늘 남은 시간대 없음 (모두 지남)", flush=True)
                    continue

                # 슬롯 쿼리 성공 + 미래 슬롯 있음 + 예약 가능 슬롯 0개
                # → API 일별 요약은 자리 있다고 하지만 실제 예약은 불가한 상태 (마감/비활성)
                if slot_info["queried"] and slot_info["total"] > 0 and not slot_info["times"]:
                    alerted.pop(alert_key, None)
                    alerted.pop(f"{alert_key}:pre", None)
                    print(f"[{now_str}] 🚫 {name} {date_str} 예약 마감 (자리 있으나 슬롯 예약 불가)", flush=True)
                    continue

                slot_str = f" [{', '.join(slot_info['times'])}]" if slot_info["times"] else ""
                available = d["stock"] - d["bookingCount"]
                avail_str = f"잔여 {available}자리"

                if window_open:
                    last_available = alerted.get(alert_key)  # None이면 처음 감지
                    is_more = last_available is not None and available > last_available

                    if is_more:
                        delta = available - last_available
                        title = f"🎉 {name} 자리 추가됐어요!"
                        body = f"{date_str}{slot_str} {avail_str} (+{delta}자리)"
                    else:
                        title = f"🎉 {name} 예약 가능!"
                        body = f"{date_str}{slot_str} {avail_str}"

                    print(f"[{now_str}] 🎉 {name} | {body}", flush=True)
                    if last_available is None or is_more:
                        if ntfy_topic:
                            send_ntfy(ntfy_topic, title, body, url)
                    alerted[alert_key] = available
                else:
                    # 예약창 미오픈 + 자리 있음 — 별도 키로 한 번만 알림
                    # 예약창이 열리면 alert_key(pre 없는 키)가 alerted에 없으므로 즉시 재알림
                    pre_key = f"{alert_key}:pre"
                    title = f"⏳ {name} 자리 있음 (예약창 미오픈)"
                    body = f"{date_str}{slot_str} {avail_str} · {window_reason}"
                    print(f"[{now_str}] ⏳ {name} | {body}", flush=True)
                    if pre_key not in alerted:
                        if ntfy_topic:
                            send_ntfy(ntfy_topic, title, body, url)
                        alerted[pre_key] = 1
            else:
                alerted.pop(alert_key, None)
                alerted.pop(f"{alert_key}:pre", None)
                print(f"[{now_str}] ❌ {name} {date_str} 매진 (재고:{d['stock']} / 예약:{d['bookingCount']})", flush=True)


def print_startup_info(active: list) -> None:
    """시작 시 각 모니터 항목의 예약 오픈 시각을 조회해 출력."""
    print("=== 예약 오픈 정보 조회 중... ===", flush=True)
    for m in active:
        name = m.get("name", "?")
        parsed = parse_naver_url(m.get("url", ""))
        if not parsed:
            print(f"  • {name}: URL 파싱 실패", flush=True)
            continue

        raw = m.get("target_dates", [])
        dates_only = [e.split(" ")[0] for e in raw]
        result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], dates_only)
        dates_label = ", ".join(raw) or "전체"

        if result is None:
            print(f"  • {name} [{dates_label}] | 예약창: 조회 실패", flush=True)
            continue

        is_open, _ = booking_window_status(m, result["sale_start_date"], result["sale_end_date"])
        open_src = m.get("booking_open_datetime") or result.get("sale_start_date")
        dt = _parse_dt(open_src)

        if is_open:
            status = "오픈됨 ✅"
        elif dt:
            status = f"오픈 예정 → {dt.strftime('%Y/%m/%d %H:%M')} ⏳"
        else:
            status = "오픈 시각 정보 없음 (monitors.json에 booking_open_datetime 설정 가능)"

        print(f"  • {name} [{dates_label}] | 예약창: {status}", flush=True)


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
    print_startup_info(active)

    alerted: dict[str, int] = {}  # alert_key → 마지막으로 알림 보낸 시점의 가용 자리 수
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
