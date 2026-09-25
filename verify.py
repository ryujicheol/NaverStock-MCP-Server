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
    ("900140", "엘브이엠씨홀딩스"), ("011170", "롯데케미칼"),
]

# server.py가 각 엔드포인트에서 실제로 꺼내 쓰는 키
REFERENCED = {
    "polling": ["stockName", "closePrice", "compareToPreviousClosePrice", "fluctuationsRatio",
                "compareToPreviousPrice", "marketStatus", "marketSessionType", "marketValueFull"],
    "integration": ["stockName", "totalInfos", "consensusInfo"],
    "trend": ["bizdate", "closePrice", "foreignerPureBuyQuant",
              "organPureBuyQuant", "individualPureBuyQuant", "foreignerHoldRatio"],
    "finance/annual": ["financeInfo"],
}
URLS = {
    "polling": "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
    "trend": "https://m.stock.naver.com/api/stock/{code}/trend?pageSize=10",
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
        url = URLS.get(endpoint, "https://m.stock.naver.com/api/stock/{code}/" + endpoint)
        seen = {key: 0 for key in keys}
        checked = 0
        for code, _ in STOCKS:
            data = fetch(url.format(code=code))
            if isinstance(data, dict) and "__error__" in data:
                continue
            if endpoint == "polling":
                data = data.get("datas") or []
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
            # 네이버가 세션 값 이름을 바꾸면 가격 기준이 원래 값 그대로 나온다. 틀린 건 아니라 warn.
            if "확인되지 않은 시장 상태" in output:
                print(f"    warn {fn_name}({name}) {output.splitlines()[-1]}")
                quiet = False
            # 가격이 어느 시세인지 늘 적혀 있어야 한다(2026-09-23 리뷰). 장중이 아니면 정규장 종가도.
            # 조회 시각만 있으면 휴장일에 받은 값이 오늘 시세처럼 읽힌다(2026-09-24 추석 연휴) — 체결 시각도.
            required = {"stock_price": ["가격 기준:", "마지막 체결"], "stock_detail": ["[시세 기준", "마지막 체결"],
                        "stock_investor_trend": ["종가: 그날 마지막 체결가"]}.get(fn_name, [])
            if fn_name == "stock_price" and "정규장 실시간" not in output:
                required = required + ["정규장 종가:"]
            for label in required:
                if label not in output:
                    print(f"    FAIL {fn_name}({name}) '{label}' 표시가 없다")
                    problems.append(f"{fn_name}({name}) 에 '{label}' 표시가 없다")
                    quiet = False
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
    # 현재가가 정규장인지 애프터마켓인지 표 위에 늘 적혀 있어야 한다(2026-09-23 리뷰: 안내가 사라졌다).
    basis = next((l for l in markdown.split("\n") if l.startswith("현재가 기준:")), None)
    if basis is None:
        print("    FAIL 표 위에 현재가 기준 줄이 없다")
        problems.append("stock_compare 에 현재가 기준 표시가 없다")
    else:
        print(f"    ok   {basis[:60]}")
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
    # 적자면 음수 PER 대신 '적자'여야 한다. 음수 PER은 "10배 이하" 필터를 통과하고,
    # 크기 순서도 뜻이 없다(적자가 클수록 0에 가깝다).
    col = {label: index for index, label in enumerate(header)}
    checked = stars = losses = off = 0
    for r in rows:
        if len(r) != len(header):
            continue
        price, eps = server._to_int(r[col["현재가"]]), server._to_int(r[col["EPS(E)"]])
        per = r[col["선행PER"]]
        if eps is not None and eps < 0:
            losses += 1
            if per != "적자":
                off += 1
                print(f"    FAIL 선행PER {r[0]}: 적자 추정(EPS(E) {eps:,})인데 {per}")
                problems.append(f"stock_compare 적자 추정인데 선행PER이 '적자'가 아니다: {r[0]}")
            continue
        if not price or not eps or per.rstrip("*") in ("-", "적자"):
            continue
        checked += 1
        stars += per.endswith("*")
        if abs(float(per.rstrip("*")) - price / eps) > 0.006:
            off += 1
            print(f"    FAIL 선행PER {r[0]}: {per} ≠ {price:,} ÷ {eps:,} = {price / eps:.2f}")
            problems.append(f"stock_compare 선행PER이 현재가 ÷ EPS(E)와 다르다: {r[0]}")
    if not off:
        print(f"    ok   선행PER = 현재가 ÷ EPS(E) — {checked}행 검산 일치 (`*` 보완 {stars}행 포함), "
              f"적자 추정 {losses}행은 '적자'")

    # 시총·PER·PBR도 그 행의 현재가로 계산돼야 한다. 종목 상세의 값을 옮기면 요청 시점이 달라
    # 현재가와 틱이 어긋난다 — 2026-09-23 애프터마켓 중 삼성전자가 같은 현재가 285,500원에 시총
    # 1,666조/1,669조로 갈렸다. 표에 EPS·BPS·주식수가 없으니 따로 받아 대조한다(주식수 = 시세
    # 응답의 시총 ÷ 현재가). 후행 PER은 적자면 '적자'여야 한다.
    valid = [r for r in rows if len(r) == len(header) and not r[0].endswith("조회 실패")]
    codes = [r[0].rsplit("(", 1)[-1].rstrip(")") for r in valid]
    polled = fetch(URLS["polling"].format(code=",".join(codes)))
    quotes = {q.get("itemCode"): q for q in polled.get("datas") or []}
    trailing_losses = cells = derived_off = 0
    for r, code in zip(valid, codes):
        price = server._to_int(r[col["현재가"]])
        data = fetch(f"https://m.stock.naver.com/api/stock/{code}/integration")
        infos = {} if "__error__" in data else {i["key"]: i["value"] for i in data.get("totalInfos", [])}
        eps = server._to_int(server._strip_unit(infos.get("EPS")))
        bps = server._to_int(server._strip_unit(infos.get("BPS")))
        quote = quotes.get(code) or {}
        cap_full, close = server._to_int(quote.get("marketValueFull")), server._to_int(quote.get("closePrice"))
        expected = {}
        if eps is not None and eps < 0:
            trailing_losses += 1
            expected["PER"] = "적자"
        elif price and eps:
            expected["PER"] = price / eps
        if price and bps and bps > 0:
            expected["PBR"] = price / bps
        if price and cap_full and close:
            expected["시총"] = server._fmt_cap(round(cap_full / close) * price)
        for label, want in expected.items():
            got = r[col[label]]
            if isinstance(want, float):
                try:
                    same = abs(float(got) - want) <= 0.006
                except ValueError:
                    same = False
                want = f"{want:.2f}"
            else:
                same = got == want
            cells += 1
            if not same:
                derived_off += 1
                print(f"    FAIL {label} {r[0]}: 표 {got} ≠ 그 행의 현재가 {r[col['현재가']]} 기준 {want}")
                problems.append(f"stock_compare {label}이 그 행의 현재가 기준이 아니다: {r[0]}")
    if not derived_off:
        print(f"    ok   시총·PER·PBR = 그 행의 현재가 기준 — {cells}칸 검산 일치, 후행 적자 {trailing_losses}행은 '적자'")


def check_compare_sort():
    print("\n" + "=" * 62)
    print("[5] stock_compare 정렬 — 숫자는 순서대로, 적자·값 없음·조회 실패는 맨 아래")
    print("=" * 62)
    codes = ",".join(code for code, _ in STOCKS) + ",091990"
    for sort_by, descending in [("선행PER", False), ("ROE(E)", True)]:
        markdown = server.stock_compare(codes, sort_by=sort_by)
        table = [l for l in markdown.split("\n") if l.startswith("|")]
        if len(table) < 3:
            print(f"    FAIL {sort_by} 정렬 표가 없다: {markdown[:60]}")
            problems.append(f"stock_compare sort_by={sort_by} 가 표를 만들지 못했다")
            continue
        header = [c.strip() for c in table[0].strip("|").split("|")]
        rows = [[c.strip() for c in l.strip("|").split("|")] for l in table[2:]]
        values = [server._num(r[header.index(sort_by)].rstrip("*")) for r in rows]
        failed = [r[0].endswith("조회 실패") for r in rows]
        numeric = [v for v in values if v is not None]
        # 숫자 행 → 숫자 아닌 행 → 조회 실패 행 순서여야 한다.
        rank = [2 if f else (1 if v is None else 0) for v, f in zip(values, failed)]
        ordered = numeric == sorted(numeric, reverse=descending) and rank == sorted(rank)
        if ordered:
            print(f"    ok   {sort_by} {'높은' if descending else '낮은'} 순 — 숫자 {len(numeric)}행, "
                  f"아래로 {rank.count(1)}행·조회 실패 {rank.count(2)}행")
        else:
            print(f"    FAIL {sort_by} 정렬이 어긋났다: {[r[0][:8] for r in rows]}")
            problems.append(f"stock_compare sort_by={sort_by} 정렬이 어긋났다")


def check_regular_close():
    print("\n" + "=" * 62)
    print("[7] 정규장 종가 — 분봉 방식이 네이버 기준가(정규장 전일 종가)를 재현하는가")
    print("=" * 62)
    # 현재가 − 전일대비 = 기준가 = 직전 거래일 정규장 종가다. 현재가·일봉 종가는 애프터마켓 종가라
    # 이 검산이 분봉(KRX 단독)이 정규장 종가를 준다는 유일한 근거다. 기준가가 언제 다음 날로
    # 넘어가는지(자정·개장 전) 모르므로 최근 3거래일 중 하나와 맞으면 통과.
    for code, name in [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("451800", "한화리츠")]:
        quote = server._fetch_quotes([code])[0].get(code) or {}
        price = server._to_int(quote.get("closePrice"))
        change = server._to_int(quote.get("compareToPreviousClosePrice"))
        if price is None or change is None:
            print(f"    FAIL {name} 시세를 받지 못했다")
            problems.append(f"정규장 종가 검산용 시세 조회 실패: {name}")
            continue
        base = price - change
        closes = {d: server._regular_close_on(code, d) for d in server._trading_dates(code)[-3:]}
        hit = [d for d, c in closes.items() if c == base]
        if hit:
            print(f"    ok   {name} 기준가 {base:,} = {hit[-1]} 정규장 종가 (현재가 {price:,})")
        else:
            print(f"    FAIL {name} 기준가 {base:,}가 최근 정규장 종가 {closes}와 다르다")
            problems.append(f"분봉 정규장 종가가 기준가와 안 맞는다: {name}")


def check_edges():
    print("\n" + "=" * 62)
    print("[6] 엣지 — 없는 코드는 실패를 말해야 한다")
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
    # 한도를 넘긴 종목은 코드로 알려야 한다(2026-09-23 리뷰: '나머지 1개 생략'만 나와
    # 51번째 삼성전자우가 빠진 걸 몰랐다). 한도를 2로 줄여 확인한다.
    saved, server._COMPARE_MAX = server._COMPARE_MAX, 2
    try:
        output = server.stock_compare("005930,000660,005935")
    finally:
        server._COMPARE_MAX = saved
    note = next((l for l in output.split("\n") if "생략" in l), "")
    if "005935" in note:
        print("    ok   한도 초과 → 생략된 코드(005935)를 적는다")
    else:
        print(f"    FAIL 한도 초과분의 코드를 알려주지 않는다: {note[:60]}")
        problems.append("stock_compare 가 생략한 종목코드를 알려주지 않는다")

    # 같은 코드는 한 번만 조회한다(2026-09-23 리뷰: 두 줄로 나오고 한도만 먹었다). 소문자 코드도
    # 조회돼야 한다 — polling은 소문자 코드를 응답에서 빼서, 옮긴 날 '조회 실패'가 됐었다.
    # 0126Z0(삼성에피스홀딩스)이 상장폐지되면 다른 영숫자 코드로 바꿀 것.
    output = server.stock_compare("005930,005930,0126z0")
    names = [l.split("|")[1].strip() for l in output.split("\n") if l.startswith("|")][2:]
    dedup = len(names) == 2 and any("한 번만" in l and "005930" in l for l in output.split("\n"))
    lower = not any("조회 실패" in n for n in names) and "실패" not in server.stock_price("0126z0")
    print(f"    {'ok  ' if dedup else 'FAIL'} 같은 코드 두 번 → 한 행, 중복 안내 ({len(names)}행)")
    print(f"    {'ok  ' if lower else 'FAIL'} 소문자 코드(0126z0) → 조회된다")
    if not dedup:
        problems.append("stock_compare 가 중복 코드를 걸러내지 않는다")
    if not lower:
        problems.append("소문자 종목코드가 조회 실패로 나온다")

    # 우선주는 EPS·BPS가 보통주 값이라 배수가 낮게 나온다 — 행에 †와 주석이 붙어야 한다
    # (2026-09-23 리뷰). 주석의 전제(보통주 값과 같다)도 함께 본다. 네이버가 우선주 자체 EPS를
    # 주기 시작하면 주석이 거짓이 된다.
    output = server.stock_compare("005930,005935")
    names = [l.split("|")[1].strip() for l in output.split("\n") if l.startswith("|")][2:]
    marked = [n for n in names if "†" in n]
    if marked == ["삼성전자우† (005935)"] and "`†` 표시는 **우선주**" in output:
        print("    ok   우선주 행에만 †와 주석")
    else:
        print(f"    FAIL 우선주 표시가 어긋났다: {names}")
        problems.append("stock_compare 우선주 표시가 어긋났다")
    common, pref = ({i["code"]: i["value"] for i in fetch(
        f"https://m.stock.naver.com/api/stock/{code}/integration").get("totalInfos", [])}
        for code in ("005930", "005935"))
    differ = [k for k in ("eps", "cnsEps", "bps") if common.get(k) != pref.get(k)]
    if differ:
        print(f"    FAIL 삼성전자우의 {differ}가 보통주와 다르다 — † 주석(보통주 값)을 고칠 것")
        problems.append(f"우선주 EPS·BPS가 더 이상 보통주 값이 아니다: {differ}")
    else:
        print("    ok   주석 전제 — 삼성전자우 EPS·추정EPS·BPS = 보통주 값")


def check_labels_2026_09_24():
    print("\n" + "=" * 62)
    print("[8] 표시 — 지수 기준 시각, 뉴스 엔티티, 해외 종목, 정규장 마감 시각")
    print("=" * 62)
    output = server.market_index("KOSPI")
    ok = "기준:" in output
    print(f"    {'ok  ' if ok else 'FAIL'} market_index 에 기준 시각 → {output.splitlines()[-1]}")
    if not ok:
        problems.append("market_index 에 기준 시각이 없다")
    # 네이버 뉴스 제목은 HTML 엔티티째 온다(&quot;…&quot;).
    leaked = [code for code, _ in STOCKS[:3] if re.search(r"&(quot|amp|lt|gt|#\d+);", server.stock_news(code))]
    print(f"    {'ok  ' if not leaked else 'FAIL'} 뉴스 제목에 HTML 엔티티 없음 {leaked or ''}")
    if leaked:
        problems.append(f"뉴스 제목에 HTML 엔티티가 남았다: {leaked}")
    # 검색은 해외 종목도 돌려주지만 다른 도구는 국내만 조회한다 — 빠지고 안내가 붙어야 한다.
    output = server.stock_search("애플")
    ok = "AAPL" not in output and "해외 종목" in output
    print(f"    {'ok  ' if ok else 'FAIL'} 해외 종목(애플) 제외 + 안내")
    if not ok:
        problems.append("stock_search 가 해외 종목을 걸러내지 않는다")
    # 평소엔 15:20~15:30 종가 단일가 동안 분봉이 없다 — 최근 거래일이 16:30으로 잡히면 판정이 틀린 것.
    # (수능일엔 16:30이 맞다. 그날 돌리면 이 검사가 FAIL로 알려준다.)
    ends = {d: server._regular_session_end(d) for d in server._trading_dates("005930")[-3:]}
    ok = all(v == "1530" for v in ends.values())
    print(f"    {'ok  ' if ok else 'FAIL'} 최근 거래일 정규장 마감 {ends}")
    if not ok:
        problems.append(f"정규장 마감 시각 판정이 평일에 15:30이 아니다: {ends}")


def consensus_tables(text):
    """stock_consensus 출력 → {섹션: [(표 위 제목 줄, 머리행, 행들)]}."""
    sections, section, title, table = {}, None, "", None
    for line in text.split("\n"):
        if line.startswith("[") and "]" in line:
            section, title, table = line[1:line.index("]")], "", None
            sections[section] = []
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= {"-"}:
                continue  # 구분선
            if table is None:
                table = (title, cells, [])
                sections.setdefault(section, []).append(table)
            else:
                table[2].append(cells)
        else:
            table = None
            if line.strip():
                title = line.strip()
    return sections


def _float(text):
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


# 추이 항목 → 실적·추정 표의 열, 허용 오차(억원은 표가 소수 한 자리, 추이가 정수 반올림)
TREND_TO_TABLE = {
    "매출액(억원)": ("매출액", 1), "영업이익(억원)": ("영업이익", 1), "순이익(억원)": ("순이익", 1),
    "EPS(원)": ("EPS", 1), "BPS(원)": ("BPS", 1), "PER(배)": ("PER", 0.011), "PBR(배)": ("PBR", 0.011),
    "ROE(%)": ("ROE(%)", 0.011),
}


def check_consensus():
    print("\n" + "=" * 62)
    print("[9] stock_consensus — 추정 기간 전부, 모바일 API와 같은 컨센서스, 산술 검산")
    print("=" * 62)
    # 전제: 모바일 API는 추정 열을 1개만 준다(2026-09-25) — stock_financials가 stock_consensus를
    # 안내하는 이유다. 더 주기 시작하면 그 안내가 거짓이 된다.
    many = []
    for code, name in STOCKS:
        for period in ("annual", "quarter"):
            data = fetch(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}")
            titles = [] if "__error__" in data else (data.get("financeInfo") or {}).get("trTitleList") or []
            estimates = [t.get("title") for t in titles if t.get("isConsensus") == "Y"]
            if len(estimates) > 1:
                many.append(f"{name} {period} {estimates}")
    if many:
        print(f"    FAIL 모바일 API가 추정 열을 2개 이상 준다 — stock_financials 안내를 고칠 것: {many}")
        problems.append("모바일 API 추정 열이 1개가 아니다 — stock_financials 안내 수정 필요")
    else:
        print(f"    ok   전제 — 모바일 API 추정 열은 연간·분기 각 1개 이하 ({len(STOCKS)}종목)")

    for code, name in [("005930", "삼성전자"), ("000660", "SK하이닉스")]:
        for period in ("annual", "quarter"):
            tag = f"{name} {period}"
            output = server.stock_consensus(code, period)
            sections = consensus_tables(output)
            # 분기 각주(연환산 아님)는 분기 표에만 붙어야 한다 — 연간 표에 붙은 회귀가 있었다(if/else 어긋남).
            if ("분기의 PER·ROE·EV/EBITDA" in output) != (period == "quarter"):
                print(f"    FAIL {tag} 분기 각주가 {'없다' if period == 'quarter' else '연간 표에 붙었다'}")
                problems.append(f"stock_consensus({tag}) 분기 각주가 어긋났다")
            main = (sections.get("실적·추정") or [None])[0]
            if main is None:
                print(f"    FAIL {tag} 실적·추정 표가 없다")
                problems.append(f"stock_consensus({tag}) 표가 없다")
                continue
            _, header, rows = main
            col = {label: index for index, label in enumerate(header)}
            by_period = {r[0][:7]: r for r in rows}
            estimates = [r for r in rows if "(E)" in r[0] and any(c != "-" for c in r[1:])]
            # 모바일 API는 추정이 1개 기간뿐이었다 — 이 도구를 만든 이유다.
            if len(estimates) < 2:
                print(f"    FAIL {tag} 추정 기간이 {len(estimates)}개뿐이다")
                problems.append(f"stock_consensus({tag}) 추정 기간이 2개 미만")
            # 첫 추정은 모바일 API의 추정 열과 같은 컨센서스여야 한다(억원은 반올림 차 1까지).
            mobile = fetch(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}")
            info = {} if "__error__" in mobile else mobile.get("financeInfo") or {}
            head = next((t for t in info.get("trTitleList") or [] if t.get("isConsensus") == "Y"), None)
            values = {r.get("title"): (r.get("columns") or {}).get(head.get("key"), {}).get("value")
                      for r in info.get("rowList") or []} if head else {}
            first = estimates[0] if estimates else None
            same = first is not None and head is not None and first[0][:7] == head.get("title", "")[:7]
            for label, mobile_label, tolerance in [("매출액", "매출액", 1), ("영업이익", "영업이익", 1), ("EPS", "EPS", 0)]:
                got, want = _float(first[col[label]]) if first else None, _float(values.get(mobile_label))
                if first and got is None and want is None:
                    continue  # 두 출처 모두 없는 항목(은행의 매출액)
                same = same and got is not None and want is not None and abs(got - want) <= tolerance
            if same:
                print(f"    ok   {tag} 추정 {len(estimates)}개 기간, 첫 추정({first[0]}) 매출액·영업이익·EPS = 모바일 API")
            else:
                print(f"    FAIL {tag} 첫 추정이 모바일 API와 다르다: {first} vs {head and head.get('title')} {values}")
                problems.append(f"stock_consensus({tag}) 첫 추정이 모바일 API 추정 열과 다르다")
            # YoY = 1년 전 같은 달 대비 매출액 증가율.
            yoy_checked = yoy_off = 0
            for r in rows:
                prev = by_period.get(f"{int(r[0][:4]) - 1}{r[0][4:7]}")
                now, before, yoy = _float(r[col["매출액"]]), prev and _float(prev[col["매출액"]]), _float(r[col["YoY(%)"]])
                if now is None or not before or yoy is None:
                    continue
                yoy_checked += 1
                # 표의 매출액이 소수 한 자리(억원)라 매출이 작으면 계산값이 0.02 넘게 흔들린다.
                if abs((now / before - 1) * 100 - yoy) > (0.05 / before + now * 0.05 / before ** 2) * 100 + 0.006:
                    yoy_off += 1
                    print(f"    FAIL {tag} {r[0]} YoY {yoy} ≠ {now:,} ÷ {before:,} − 1")
                    problems.append(f"stock_consensus({tag}) YoY 검산 불일치: {r[0]}")
            # 추이 칸은 원본 값이어야 한다. 원본의 0.0은 '값 없음'이라(1년 전 추정이 없던 기간) '-'여야 하고,
            # 진짜 작은 값은 숫자로 나와야 한다(롯데케미칼 2027E 1년 전 ROE 0.003% → 0.00). 값이 있는 항목이
            # 빠져서도 안 된다. 기준일 매출액은 표의 그 기간 매출액과 같아야 한다.
            frq = 0 if period == "annual" else 1
            trend_checked = trend_off = 0
            for title, trend_header, trend_rows in sections.get("컨센서스 추이") or []:
                raw = fetch(server._consensus_url(code, 4, frq, yymm=title[:7].replace(".", "")))
                items = [] if "__error__" in raw else raw.get("JsonData") or []
                shown_rows = {t[0]: t for t in trend_rows}
                for item in items:
                    acc_name, values = item.get("ACC_NM", ""), [item.get(f"VAL{n}") for n in range(1, 6)]
                    present = [isinstance(v, (int, float)) and v != 0 for v in values]
                    cells = shown_rows.get(acc_name)
                    digits = 1 if "(억원)" in acc_name else 0 if "(원)" in acc_name else 2
                    if cells is None:
                        wrong = any(present)
                    else:
                        wrong = any(cell != "-" if not ok else
                                    _float(cell) is None or abs(_float(cell) - v) > 0.5 * 10 ** -digits + 1e-9
                                    for v, ok, cell in zip(values, present, cells[1:]))
                    if wrong:
                        trend_off += 1
                        print(f"    FAIL {tag} 추이 {title[:10]} {acc_name}: 표 {cells and cells[1:]} vs 원본 {values}")
                        problems.append(f"stock_consensus({tag}) 추이 칸이 원본과 다르다: {title[:10]} {acc_name}")
                # 기준일 추정치는 위 표의 그 기간 값과 같아야 한다. 한쪽에만 있는 항목(은행의 매출액,
                # 분기 추이에 없는 BPS)은 건너뛴다.
                row = by_period.get(title[:7])
                if row is None:
                    continue
                trend_checked += 1
                for acc_name, (label, tolerance) in TREND_TO_TABLE.items():
                    cells = shown_rows.get(acc_name)
                    a, b = (_float(cells[1]) if cells else None), _float(row[col[label]])
                    if a is not None and b is not None and abs(a - b) > tolerance:
                        trend_off += 1
                        print(f"    FAIL {tag} 추이 {title[:10]} 기준일 {acc_name} {cells[1]} ≠ 표 {row[col[label]]}")
                        problems.append(f"stock_consensus({tag}) 추이 기준일 값이 표와 다르다: {title[:10]} {acc_name}")
            # 서프라이즈(%) = (실적 − 추정) ÷ |추정| — 추정이 음수여도(SK하이닉스 2023 영업이익) 이 식이다.
            # FnGuide가 주는 %는 음수 추정에서 부호가 뒤집히거나 표의 추정과 안 맞을 때가 있어 도구가
            # 직접 계산하고, 원본과 다른 칸에만 ✎를 단다. 원본 %를 따로 받아 ✎ 판정도 대조한다.
            shown = {}
            for acc, account in server._CNS_SURPRISE:
                data = fetch(server._consensus_url(code, 5, frq, acc_cd=acc))
                found = {} if "__error__" in data else data.get("tableData") or {}
                header_row = (found.get("tableHeaderData") or [{}])[0]
                for row in found.get("tableData") or []:
                    for key in server._CNS_SURPRISE_KEYS:
                        shown[(header_row.get(f"CNS_{key}"), account, row.get("QTR"))] = row.get(f"{key}_S")
            surprise_checked = surprise_off = marks = 0
            for _, surprise_header, surprise_rows in sections.get("어닝서프라이즈") or []:
                for r in surprise_rows:
                    actual = _float(r[2].split(" (")[0])
                    for head, cell in zip(surprise_header[3:], r[3:]):
                        m = re.match(r"^(-?[\d,]+(?:\.\d+)?) \(([+-]\d+\.\d+)%(✎?)\)$", cell)
                        if not m or actual is None:
                            continue
                        estimate, ratio, marked = _float(m.group(1)), float(m.group(2)), bool(m.group(3))
                        surprise_checked += 1
                        marks += marked
                        computed = (actual - estimate) / abs(estimate) * 100
                        if abs(computed - ratio) > 0.006:
                            surprise_off += 1
                            print(f"    FAIL {tag} 서프라이즈 {r[0]} {r[1]}: {cell} vs 실적 {r[2]}")
                            problems.append(f"stock_consensus({tag}) 서프라이즈 % 검산 불일치: {r[0]} {r[1]}")
                        original = shown.get((r[0], r[1], head))
                        tolerance = (0.05 / abs(estimate) + abs(actual) * 0.05 / estimate ** 2) * 100 + 0.006
                        should = isinstance(original, (int, float)) and abs(original - computed) > tolerance
                        if marked != should:
                            surprise_off += 1
                            print(f"    FAIL {tag} ✎ 판정 {r[0]} {r[1]} {head}: {cell}, 화면 {original}")
                            problems.append(f"stock_consensus({tag}) ✎ 판정이 어긋났다: {r[0]} {r[1]} {head}")
            if not (yoy_off or trend_off or surprise_off):
                print(f"    ok   {tag} 검산 — YoY {yoy_checked}행, 추이 기준일 {trend_checked}기간, "
                      f"서프라이즈 {surprise_checked}칸 (화면과 달라 ✎ {marks}칸)")
            if not surprise_checked or not trend_checked:
                print(f"    FAIL {tag} 검산할 추이·서프라이즈가 없다 (추이 {trend_checked}, 서프라이즈 {surprise_checked})")
                problems.append(f"stock_consensus({tag}) 추이 또는 서프라이즈가 비었다")

    # 추정이 없는 종목(지금은 에스티아이·한화리츠·엘브이엠씨홀딩스)은 그렇다고 말해야 하고, 추정이 있는
    # 종목엔 그 말이 없어야 한다. 종목의 커버리지가 바뀌어도 이 검사는 스스로 맞다.
    mismatched, failed = [], []
    for code, name in STOCKS + [("039440", "에스티아이")]:
        output = server.stock_consensus(code)
        main = (consensus_tables(output).get("실적·추정") or [None])[0]
        if main is None:
            failed.append(name)
            continue
        estimates = [r for r in main[2] if "(E)" in r[0]]
        blank = all(c == "-" for r in estimates for c in r[1:])
        if blank != ("현재 컨센서스 추정치가 없습니다" in output):
            mismatched.append(name)
    if failed or mismatched:
        print(f"    FAIL 표가 없음 {failed} / 추정 없음 안내 불일치 {mismatched}")
        problems.append(f"stock_consensus 표 없음 {failed} 또는 추정 없음 안내 불일치 {mismatched}")
    else:
        print(f"    ok   {len(STOCKS) + 1}종목 모두 표가 나오고, 추정이 빈 종목에만 '추정치가 없습니다'")
    # 결산 주기가 6개월인 리츠는 원본 YoY에 직전 결산기 대비가 섞여 있다(한화리츠 2026.04: 원본 3.50%,
    # 1년 전 대비 7.53%). 도구의 YoY는 1년 전 같은 결산기 대비이거나, 그 결산기가 표에 없으면 비어야 한다.
    # 한화리츠가 결산 주기를 바꾸거나 상장폐지되면 다른 6개월 결산 리츠(롯데리츠 330590 등)로 바꿀 것.
    output = server.stock_consensus("451800")
    main = (consensus_tables(output).get("실적·추정") or [None])[0]
    if main is None:
        print(f"    FAIL 한화리츠 표가 없다: {output[:60]}")
        problems.append("stock_consensus(한화리츠) 표가 없다 — 6개월 결산 YoY 검사를 못 했다")
    else:
        _, header, rows = main
        col = {label: index for index, label in enumerate(header)}
        months = {int(r[0][:4]) * 12 + int(r[0][5:7]): _float(r[col["매출액"]]) for r in rows}
        wrong, checked = [], 0
        for r in rows:
            now, before = months[int(r[0][:4]) * 12 + int(r[0][5:7])], months.get(int(r[0][:4]) * 12 + int(r[0][5:7]) - 12)
            cell = r[col["YoY(%)"]].rstrip("✎")
            if now is None or not before:
                if cell != "-":
                    wrong.append(f"{r[0]} {cell}(1년 전 결산기가 없는데 값이 있다)")
                continue
            checked += 1
            if _float(cell) is None or abs(_float(cell) - (now / before - 1) * 100) > \
                    (0.05 / before + now * 0.05 / before ** 2) * 100 + 0.006:
                wrong.append(f"{r[0]} {cell} ≠ 1년 전 대비 {(now / before - 1) * 100:.2f}")
        noted = "결산 주기가 1년이 아닙니다" in output
        if wrong or not noted or not checked:
            print(f"    FAIL 한화리츠 YoY {wrong}, 각주 {'있음' if noted else '없음'}, 검산 {checked}행")
            problems.append("stock_consensus 6개월 결산 YoY가 1년 전 같은 결산기 대비가 아니다")
        else:
            print(f"    ok   6개월 결산(한화리츠) YoY = 1년 전 같은 결산기 대비 {checked}행, 나머지는 빈칸 + 각주")
    # 우선주·없는 코드는 WiseReport가 빈 목록을 준다. 우선주 값을 주기 시작하면 안내를 고칠 것.
    for code, label in [("005935", "우선주(005935)"), ("999999", "없는 코드")]:
        ok = "데이터가 없습니다" in server.stock_consensus(code)
        print(f"    {'ok  ' if ok else 'FAIL'} {label} → 데이터 없음 안내")
        if not ok:
            problems.append(f"stock_consensus {label} 안내가 어긋났다")


def main():
    check_referenced_fields()
    check_detail_labels()
    check_tool_output()
    check_compare_table()
    check_compare_sort()
    check_edges()
    check_regular_close()
    check_labels_2026_09_24()
    check_consensus()
    print("\n" + "=" * 62)
    if problems:
        print(f"문제 {len(problems)}건")
        for item in problems:
            print(f"  - {item}")
        sys.exit(1)
    print("문제 없음")


if __name__ == "__main__":
    main()
