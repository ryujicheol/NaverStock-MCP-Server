#!/usr/bin/env python3
"""도구가 실제로 쓸 만한 값을 돌려주는지 네이버 API에 직접 물어 확인한다.

코드가 꺼내 쓰는 응답 키를 실제 응답과 대조하고, 여러 종목으로 호출해
"어느 종목에서도 값이 없는 항목"을 찾는다. 한 종목만 보면 그 종목이 쓰지
않는 항목을 죽은 필드로 오인하므로 대형주·중소형주·적자기업·리츠·외국주를
섞어서 본다.

의존성 없이 돈다 — mcp/starlette는 스텁으로 갈아끼우고 server.py를 import해
도구 함수를 직접 호출한다. 다만 스텁으로는 진짜 패키지에서만 나는 오류
(import 실패, 스키마 생성 오류)를 못 잡으니, 배포 전에는 실제 의존성을 깔고
`python server.py`로 띄워 tools/list까지 확인할 것.

사용법:
    python verify.py
"""
import io
import json
import re
import sys
import types
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


class _StubFastMCP:
    """데코레이터를 무력화해 함수 자체를 남긴다."""

    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def run(self, *args, **kwargs):
        pass


for name, attrs in [
    ("mcp", {}), ("mcp.server", {}), ("mcp.server.fastmcp", {"FastMCP": _StubFastMCP}),
    ("starlette", {}), ("starlette.requests", {"Request": object}),
    ("starlette.responses", {"JSONResponse": object}),
]:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module

import server  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon) MCP-Korean-Stock/1.0"}

STOCKS = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("058470", "리노공업"),
    ("039030", "이오테크닉스"), ("196170", "알테오젠"), ("068270", "셀트리온"),
    ("042660", "한화오션"), ("003490", "대한항공"), ("451800", "한화리츠"),
    ("900140", "엘브이엠씨홀딩스"),
]

# server.py가 각 엔드포인트에서 실제로 꺼내 쓰는 키
REFERENCED = {
    "basic": ["stockName", "closePrice", "compareToPreviousClosePrice",
              "fluctuationsRatio", "marketStatus", "compareToPreviousPrice"],
    "integration": ["stockName", "totalInfos", "consensusInfo"],
    "trend": ["bizdate", "closePrice", "foreignerPureBuyQuant",
              "organPureBuyQuant", "individualPureBuyQuant", "foreignerHoldRatio"],
    "finance/annual": ["financeInfo"],
}

# stock_detail이 표에 싣는 라벨
DETAIL_LABELS = [
    "전일", "시가", "고가", "저가", "거래량", "대금", "시총", "외인소진율",
    "52주 최고", "52주 최저", "PER", "EPS", "추정PER", "추정EPS", "PBR",
    "BPS", "배당수익률", "주당배당금",
]

problems = []


def fetch(url):
    try:
        request = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)[:60]}


def check_referenced_fields():
    print("=" * 62)
    print("[1] 코드가 참조하는 키가 응답에 있는가")
    print("=" * 62)
    for endpoint, keys in REFERENCED.items():
        query = "?pageSize=10" if endpoint == "trend" else ""
        seen = {key: 0 for key in keys}
        checked = 0
        for code, _ in STOCKS:
            data = fetch(f"https://m.stock.naver.com/api/stock/{code}/{endpoint}{query}")
            if isinstance(data, dict) and "__error__" in data:
                continue
            rows = data if isinstance(data, list) else [data]
            if not rows:
                continue
            checked += 1
            for key in keys:
                if any(isinstance(r, dict) and r.get(key) not in (None, "", []) for r in rows):
                    seen[key] += 1
        print(f"\n  {endpoint}  ({checked}종목)")
        for key, count in seen.items():
            if count:
                print(f"    ok   {key:30s} {count}/{checked}")
            else:
                print(f"    DEAD {key:30s} 0/{checked}")
                problems.append(f"{endpoint}.{key} 가 어느 종목 응답에도 없다")


def check_detail_labels():
    print("\n" + "=" * 62)
    print("[2] stock_detail 이 싣는 라벨이 실제로 오는가")
    print("=" * 62)
    seen = {label: 0 for label in DETAIL_LABELS}
    checked = 0
    for code, _ in STOCKS:
        data = fetch(f"https://m.stock.naver.com/api/stock/{code}/integration")
        if "__error__" in data:
            continue
        checked += 1
        labels = {item["key"] for item in data.get("totalInfos", [])}
        for label in DETAIL_LABELS:
            if label in labels:
                seen[label] += 1
    for label, count in seen.items():
        if count:
            print(f"    ok   {label:12s} {count}/{checked}")
        else:
            print(f"    DEAD {label:12s} 0/{checked}")
            problems.append(f"stock_detail 의 '{label}' 라벨이 응답에 없다")


def check_tool_output():
    print("\n" + "=" * 62)
    print("[3] 도구 호출 — 실패하거나 값이 비는 항목")
    print("=" * 62)
    blank = re.compile(r":\s*(N/A|-|None)\s*$")
    quiet = True
    for code, name in STOCKS[:6]:
        calls = [("stock_price", (code,)), ("stock_detail", (code,)),
                 ("stock_investor_trend", (code, 5)), ("stock_financials", (code, "annual"))]
        for fn_name, args in calls:
            try:
                output = getattr(server, fn_name)(*args)
            except Exception as exc:  # noqa: BLE001
                print(f"    ERR  {fn_name}({name}) {exc}")
                problems.append(f"{fn_name}({name}) 예외: {exc}")
                quiet = False
                continue
            if "실패" in output or "없습니다" in output:
                print(f"    FAIL {fn_name}({name}) → {output.splitlines()[0][:50]}")
                problems.append(f"{fn_name}({name}) 가 실패를 반환")
                quiet = False
                continue
            empties = [l.split(":")[0].strip() for l in output.split("\n") if blank.search(l)]
            if empties:
                # 컨센서스가 없는 종목은 추정PER/추정EPS가 비는 게 정상이다.
                print(f"    warn {fn_name}({name}) 빈 항목 {empties}")
                quiet = False
    if quiet:
        print("    모든 호출에서 값이 찼다")


def check_compare_table():
    print("\n" + "=" * 62)
    print("[4] stock_compare 표 무결성")
    print("=" * 62)
    markdown = server.stock_compare(",".join(code for code, _ in STOCKS))
    lines = [l for l in markdown.split("\n") if l.startswith("|")]
    if len(lines) < 3:
        print("    FAIL 표가 만들어지지 않았다")
        problems.append("stock_compare 가 표를 만들지 못했다")
        return
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = [[c.strip() for c in l.strip("|").split("|")] for l in lines[2:]]
    ragged = sum(1 for r in rows if len(r) != len(header))
    for index, label in enumerate(header):
        values = [r[index] for r in rows if len(r) == len(header)]
        if values and all(v in ("-", "", "N/A") for v in values):
            print(f"    DEAD 컬럼 {label}")
            problems.append(f"stock_compare 의 '{label}' 컬럼이 전 종목에서 비었다")
    if ragged:
        print(f"    FAIL 깨진 행 {ragged}개")
        problems.append(f"stock_compare 에 깨진 행 {ragged}개")
    else:
        print(f"    ok   {len(rows)}행, 깨진 행 없음, 죽은 컬럼 없음")

    # 선행PER은 표에 찍힌 현재가 ÷ EPS(E)여야 한다. 실적표의 PER(E)는 배치 시점 값(대개
    # 전일 종가 기준)이라 옮겨 쓰면 틀린다 — 2026-09-23에 `*` 행에서 실제로 났던 버그다.
    col = {label: index for index, label in enumerate(header)}
    checked = stars = off = 0
    for r in rows:
        if len(r) != len(header):
            continue
        price, eps = server._to_int(r[col["현재가"]]), server._to_int(r[col["EPS(E)"]])
        per = r[col["선행PER"]]
        if not price or not eps or per.rstrip("*") == "-":
            continue
        checked += 1
        stars += per.endswith("*")
        if abs(float(per.rstrip("*")) - price / eps) > 0.006:
            off += 1
            print(f"    FAIL 선행PER {r[0]}: {per} ≠ {price:,} ÷ {eps:,} = {price / eps:.2f}")
            problems.append(f"stock_compare 선행PER이 현재가 ÷ EPS(E)와 다르다: {r[0]}")
    if not off:
        print(f"    ok   선행PER = 현재가 ÷ EPS(E) — {checked}행 검산 일치 (`*` 보완 {stars}행 포함)")


def check_edges():
    print("\n" + "=" * 62)
    print("[5] 엣지 — 없는 코드는 실패를 말해야 한다")
    print("=" * 62)
    output = server.stock_price("999999")
    if "실패" in output:
        print("    ok   없는 코드 → 실패 메시지")
    else:
        print(f"    FAIL 없는 코드인데 성공처럼 보인다: {output[:50]}")
        problems.append("없는 종목코드가 실패로 처리되지 않는다")
    # 상장폐지·합병소멸 종목은 네이버가 HTTP 409 를 준다. 조용히 빠지면
    # 스크리닝에서 누락을 알아챌 수 없으므로 실패로 드러나야 한다.
    output = server.stock_price("124050")
    print(f"    {'ok  ' if '실패' in output else 'FAIL'} 소멸 종목(124050) → {output.splitlines()[0][:40]}")


def main():
    check_referenced_fields()
    check_detail_labels()
    check_tool_output()
    check_compare_table()
    check_edges()
    print("\n" + "=" * 62)
    if problems:
        print(f"문제 {len(problems)}건")
        for item in problems:
            print(f"  - {item}")
        sys.exit(1)
    print("문제 없음")


if __name__ == "__main__":
    main()
