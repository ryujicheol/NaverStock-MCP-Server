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
import urllib.parse
import urllib.request

# 줄마다 내보낸다 — CI가 제한 시간(20분)에 끊겨도 어디까지 돌았는지 로그에 남는다(버퍼에 남은 출력은 사라진다).
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)


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


def opinion_rows(code):
    """기업현황 페이지 '제공처별 투자의견' 표에서 투자의견이 있는 행 수, 실패하면 None.

    도구의 추정기관수는 같은 페이지의 '투자의견 컨센서스' 표에서 읽는다. 다른 표로 세어 맞춰 보면 파싱과 함께
    '최근 3개월 투자의견을 낸 증권사 수'라는 각주의 전제(2026-09-27 시총 상위 173종목 모두 같았다)도 확인된다.
    투자의견이 하나도 없으면 '의견이 없습니다' 한 칸짜리 행만 있어 0이다.
    """
    try:
        request = urllib.request.Request(f"{server.WISEREPORT_COMPANY_PAGE}?cmp_cd={code}", headers=UA)
        with urllib.request.urlopen(request, timeout=12) as response:
            page = response.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    start = page.find('id="cTB24"')
    if start < 0:
        return None
    count = 0
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page[start:page.find("</table>", start)], re.S):
        cells = [re.sub(r"<[^>]+>|&nbsp;|\s", "", c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        count += len(cells) >= 7 and bool(cells[5])  # 제공처·일자·목표가·직전목표가·변동률·투자의견·직전투자의견
    return count


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
    # 적자면 부호만 붙은 음수 PER 대신 '적자(현재가 ÷ EPS)'여야 한다(사용자 결정 2026-09-25, 괄호는
    # 계산값 보존용). 음수 PER은 "10배 이하" 필터를 통과하고, 크기 순서도 뜻이 없다(적자가 클수록 0에
    # 가깝다). 숫자가 아니므로 `*`(추정EPS 보완)도 붙지 않는다.
    col = {label: index for index, label in enumerate(header)}
    checked = stars = losses = off = 0
    for r in rows:
        if len(r) != len(header):
            continue
        price, eps = server._to_int(r[col["현재가"]]), server._to_int(r[col["EPS(E)"]])
        per = r[col["선행PER"]]
        if eps is not None and eps < 0:
            losses += 1
            want = f"적자({price / eps:.2f})" if price else "적자"
            if per != want:
                off += 1
                print(f"    FAIL 선행PER {r[0]}: 적자 추정(EPS(E) {eps:,})인데 {per} (기대 {want})")
                problems.append(f"stock_compare 적자 추정인데 선행PER이 '{want}'가 아니다: {r[0]}")
            continue
        if not price or not eps or per.rstrip("*") == "-" or per.startswith("적자"):
            continue
        checked += 1
        stars += per.endswith("*")
        if abs(float(per.rstrip("*")) - price / eps) > 0.006:
            off += 1
            print(f"    FAIL 선행PER {r[0]}: {per} ≠ {price:,} ÷ {eps:,} = {price / eps:.2f}")
            problems.append(f"stock_compare 선행PER이 현재가 ÷ EPS(E)와 다르다: {r[0]}")
    if not off:
        print(f"    ok   선행PER = 현재가 ÷ EPS(E) — {checked}행 검산 일치 (`*` 보완 {stars}행 포함), "
              f"적자 추정 {losses}행은 '적자(현재가 ÷ EPS(E))'")

    # 시총·PER·PBR도 그 행의 현재가로 계산돼야 한다. 종목 상세의 값을 옮기면 요청 시점이 달라
    # 현재가와 틱이 어긋난다 — 2026-09-23 애프터마켓 중 삼성전자가 같은 현재가 285,500원에 시총
    # 1,666조/1,669조로 갈렸다. 표에 EPS·BPS·주식수가 없으니 따로 받아 대조한다(주식수 = 시세
    # 응답의 시총 ÷ 현재가). 후행 PER은 적자면 '적자(현재가 ÷ EPS)'여야 한다.
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
            expected["PER"] = f"적자({price / eps:.2f})" if price else "적자"
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
        print(f"    ok   시총·PER·PBR = 그 행의 현재가 기준 — {cells}칸 검산 일치, "
              f"후행 적자 {trailing_losses}행은 '적자(현재가 ÷ EPS)'")

    # 선행PER+1·+2 = 그 행의 현재가 ÷ 선행PER 다음 해·그다음 해 EPS(E) — WiseReport 표(우선주는 보통주 코드)에서
    # 모바일 API 추정 열의 연도로 짝을 찾는다. 전제: 그 연도의 WiseReport EPS = 표의 EPS(E)(같은 컨센서스, 1원 반올림 차).
    # 기관수는 '제공처별 투자의견' 표의 투자의견 있는 행 수와 같아야 한다.
    later_cells = later_off = aligned = count_cells = count_off = 0
    # 도구 쪽 WiseReport 조회가 실패한 칸은 '?'가 맞는 출력이다(러너 IP에선 자주 끊긴다) — 값 대조는 건너뛰고, 그 코드가
    # '?' 각주에 있는지만 본다.
    unknown = [code for r, code in zip(valid, codes) if "?" in (r[col["선행PER+1"]], r[col["선행PER+2"]], r[col["기관수"]])]
    unknown_note = next((l for l in markdown.split("\n") if l.startswith("> `?` =")), "")
    if unknown:
        missing = [code for code in unknown if code not in unknown_note]
        print(f"    {'warn' if not missing else 'FAIL'} 도구의 WiseReport 조회 실패로 `?` 칸이 있는 {len(unknown)}행은 값 대조를 "
              f"건너뜀" + (f" — `?` 각주에 없는 코드 {missing}" if missing else ""))
        if missing:
            problems.append(f"stock_compare `?` 각주에 조회 실패 코드가 빠졌다: {missing}")
    elif unknown_note:
        print(f"    FAIL `?` 칸이 없는데 `?` 각주가 있다: {unknown_note[:60]}")
        problems.append("stock_compare `?` 각주가 칸과 어긋났다")
    for r, code in zip(valid, codes):
        price = server._to_int(r[col["현재가"]])
        base_code = code[:5] + "0" if "†" in r[0] else code
        wise, mobile = fetch(server._consensus_url(base_code, 2, 0)), fetch(
            f"https://m.stock.naver.com/api/stock/{code}/finance/annual")
        if "__error__" in wise or "__error__" in mobile:
            skip_if_external(f"stock_compare {r[0]} 선행PER+1·+2 원본", wise) or print(
                f"    warn stock_compare {r[0]} 원본 조회 실패 — 선행PER+1·+2 검산 건너뜀")
            continue
        estimated = {x["YYMM"][:7]: x.get("EPS") for x in wise.get("JsonData") or [] if "(E)" in x.get("YYMM", "")}
        title = next((t.get("title", "") for t in (mobile.get("financeInfo") or {}).get("trTitleList") or []
                      if t.get("isConsensus") == "Y"), "")
        base = title[:7] or min(estimated, default="")
        first, shown_eps = server._to_int(estimated.get(base)), server._to_int(r[col["EPS(E)"]])
        if first is not None and shown_eps is not None:
            aligned += 1
            if abs(first - shown_eps) > 1:
                later_off += 1
                print(f"    FAIL {r[0]} 첫 추정 연도({base}) WiseReport EPS {first:,} ≠ 표 EPS(E) {shown_eps:,} — 연도 짝이 어긋났다")
                problems.append(f"stock_compare 선행PER+1·+2의 기준 연도가 EPS(E)와 어긋났다: {r[0]}")
        for n, label in ((1, "선행PER+1"), (2, "선행PER+2")):
            eps = server._to_int(estimated.get(f"{int(base[:4]) + n}{base[4:]}")) if base else None
            got = r[col[label]]
            if got == "?":
                continue  # 도구의 조회 실패 — 위에서 각주만 확인했다
            if not price or not eps:
                same = got == "-"
                want = "-"
            elif eps < 0:
                want = f"적자({price / eps:.2f})"
                same = got == want
            else:
                want = f"{price / eps:.2f}"
                same = _float(got) is not None and abs(_float(got) - price / eps) <= 0.006
            later_cells += 1
            if not same:
                later_off += 1
                print(f"    FAIL {label} {r[0]}: 표 {got} ≠ 현재가 {price:,} ÷ {base} 다음 {n}년 EPS {eps} = {want}")
                problems.append(f"stock_compare {label}이 현재가 ÷ 그 해 EPS(E)가 아니다: {r[0]}")
        if r[col["기관수"]] == "?":
            continue
        rows_with_opinion = opinion_rows(base_code)
        if rows_with_opinion is None:
            print(f"    warn {r[0]} 제공처별 투자의견 표 조회 실패 — 기관수 대조 건너뜀")
            continue
        count_cells += 1
        if r[col["기관수"]] != str(rows_with_opinion):
            count_off += 1
            print(f"    FAIL 기관수 {r[0]}: 표 {r[col['기관수']]} ≠ 투자의견 낸 증권사 {rows_with_opinion}곳")
            problems.append(f"stock_compare 기관수가 최근 3개월 투자의견 수와 다르다: {r[0]}")
    if not later_off:
        print(f"    ok   선행PER+1·+2 = 현재가 ÷ 그다음 두 해 EPS(E) — {later_cells}칸 검산 일치, "
              f"첫 추정 연도 짝 {aligned}행 확인")
    if not count_off and count_cells:
        print(f"    ok   기관수 = 최근 3개월 투자의견을 낸 증권사 수 — {count_cells}행 일치")
    opm_note_problem(markdown, "stock_compare 검사 표")


def opm_note_problem(markdown, label):
    """OPM확정·OPM추정이 100%를 넘는 행이 있으면 그 종목 이름이 각주에 있어야 하고, 없으면 각주도 없어야 한다."""
    lines = [l for l in markdown.split("\n") if l.startswith("|")]
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    over = [r[0].split(" (")[0].rstrip("†") for r in ([c.strip() for c in l.strip("|").split("|")] for l in lines[2:])
            if len(r) == len(header) and any((_float(r[header.index(k)]) or 0) > 100 for k in ("OPM확정", "OPM추정"))]
    note = re.search(r"100%를 넘는 종목\(([^)]*)\)", markdown)
    if (note.group(1).split(", ") if note else []) != over:
        print(f"    FAIL {label} OPM 100% 초과 각주 {note and note.group(1)} ≠ 해당 행 {over}")
        problems.append(f"{label}: OPM 100% 초과 각주가 해당 행과 다르다")
        return True
    return False


def check_compare_sort():
    print("\n" + "=" * 62)
    print("[5] stock_compare 정렬 — 숫자는 순서대로, 적자·값 없음·조회 실패는 맨 아래")
    print("=" * 62)
    codes = ",".join(code for code, _ in STOCKS) + ",091990"
    for sort_by, descending in [("선행PER", False), ("선행PER+1", False), ("ROE(E)", True)]:
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

    # 적자 추정인데 종목 상세에 추정EPS가 없어 실적표로 보완한 행 — 선행PER이 '적자(…)'라 숫자가 아니므로
    # `*`는 붙지 않아야 한다(옛 조건 `not in ("-", "적자")`는 괄호가 붙자 `*`를 달았다). 지금 살아 있는
    # 종목엔 이 경우가 없어(2026-09-25) 가짜 응답으로 본다. 선행PER+1·+2는 연도로 짝을 찾아야 하고(WiseReport 표에
    # 2025.12(A)가 끼어 있어도), 그 해 추정이 적자면 '적자(…)', 없으면 '-', WiseReport 조회 실패는 '?'다.
    saved = server._fetch, server._analyst_count

    def fake_fetch(url, broken=False):
        if url.endswith("/integration"):
            return {"totalInfos": [{"key": "EPS", "value": "-1,000원"}, {"key": "BPS", "value": "10,000원"}]}
        if url.endswith("/finance/annual"):
            return {"financeInfo": {
                "trTitleList": [{"key": "202512", "title": "2025.12.", "isConsensus": "N"},
                                {"key": "202612", "title": "2026.12.", "isConsensus": "Y"}],
                "rowList": [{"title": "EPS", "columns": {"202612": {"value": "-500"}}}]}}
        if url.startswith(server.WISEREPORT_CONSENSUS_API):
            return {"error": "timed out"} if broken else {"JsonData": [
                {"YYMM": "2025.12(A)", "EPS": "300"}, {"YYMM": "2028.12(E)", "EPS": ""},
                {"YYMM": "2026.12(E)", "EPS": "-500"}, {"YYMM": "2027.12(E)", "EPS": "-250"}]}
        return saved[0](url)

    rows = {}
    for broken in (False, True):
        server._fetch = lambda url, broken=broken: fake_fetch(url, broken)
        server._analyst_count = lambda code, broken=broken: (None, "timed out") if broken else (3, None)
        try:
            rows[broken] = server._compare_one("000000", {"closePrice": "10,000", "stockName": "가짜"})
        finally:
            server._fetch, server._analyst_count = saved
    row = rows[False]
    ok = (row["fwd_per"], row["per"], row["fallback"]) == ("적자(-20.00)", "적자(-10.00)", False)
    print(f"    {'ok  ' if ok else 'FAIL'} 적자 + 추정EPS 보완 행 → 선행PER {row['fwd_per']}, PER {row['per']}, "
          f"`*` {'없음' if not row['fallback'] else '붙음'}")
    if not ok:
        problems.append("stock_compare 적자 보완 행의 표시가 어긋났다(`*` 또는 적자 괄호)")
    got = [(r["fwd_per1"], r["fwd_per2"], r["analysts"]) for r in (rows[False], rows[True])]
    ok = got == [("적자(-40.00)", "-", "3"), ("?", "?", "?")]
    print(f"    {'ok  ' if ok else 'FAIL'} 선행PER+1·+2·기관수 — 연도 짝·적자·추정 없음 {got[0]}, 조회 실패 {got[1]}")
    if not ok:
        problems.append(f"stock_compare 선행PER+1·+2·기관수 표시가 어긋났다: {got}")

    # OPM이 100%를 넘는 행(SK스퀘어 2026E 378.85% — 지분법이익이 영업이익에만 들어 있다)엔 각주가 붙어야 한다. 지금은
    # 519종목 중 이 종목뿐이라(2026-09-27) 가짜 응답으로 본다 — 가짜1은 OPM추정 150%, 가짜2는 20%.
    def fake_finance(url):
        if not url.endswith("/finance/annual"):
            return {}
        opm = "150.00" if "/000001/" in url else "20.00"
        return {"financeInfo": {
            "trTitleList": [{"key": "202512", "title": "2025.12.", "isConsensus": "N"},
                            {"key": "202612", "title": "2026.12.", "isConsensus": "Y"}],
            "rowList": [{"title": "영업이익률", "columns": {"202512": {"value": "10.00"}, "202612": {"value": opm}}}]}}

    saved = server._fetch, server._fetch_quotes, server._analyst_count
    server._fetch, server._analyst_count = fake_finance, lambda code: (1, None)
    server._fetch_quotes = lambda codes: ({c: {"closePrice": "10,000", "stockName": f"가짜{c[-1]}"} for c in codes}, "")
    try:
        output = server.stock_compare("000001,000002")
    finally:
        server._fetch, server._fetch_quotes, server._analyst_count = saved
    if "100%를 넘는 종목(가짜1)" in output and not opm_note_problem(output, "가짜 응답"):
        print("    ok   OPM 100% 초과 행에만 각주 (가짜 응답)")
    elif "100%를 넘는 종목(가짜1)" not in output:
        print("    FAIL OPM 100% 초과 행(가짜1)에 각주가 없다")
        problems.append("stock_compare OPM 100% 초과 각주가 없다")

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
    # WiseReport는 우선주 코드엔 빈 목록이라 보통주 코드로 받는다 — 우선주 행의 선행PER+1·+2 = 우선주 현재가 ÷ 보통주
    # EPS(E)(선행PER이 보통주 EPS를 쓰는 것과 같은 방식)라 보통주 행 값 × 가격 비율이어야 하고, 기관수도 보통주 값이다.
    # 보통주 행 자체의 선행PER+1·+2는 [4]에서 검산한다.
    table = [[c.strip() for c in l.strip("|").split("|")] for l in output.split("\n") if l.startswith("|")]
    col = {label: index for index, label in enumerate(table[0])}
    common, pref = (next((r for r in table[2:] if r[0].endswith(f"({code})")), None) for code in ("005930", "005935"))
    if pref is None or common is None:
        print("    FAIL 우선주 비교 표에 삼성전자·삼성전자우 행이 없다")
        problems.append("stock_compare 우선주 비교 표가 어긋났다")
    elif "?" in [row[col[label]] for row in (pref, common) for label in ("선행PER+1", "선행PER+2", "기관수")]:
        print("    warn 우선주 선행PER+1·+2 대조 건너뜀 — 도구의 WiseReport 조회 실패(`?`)")
    else:
        ratio = server._to_int(pref[col["현재가"]]) / server._to_int(common[col["현재가"]])
        pairs = [(_float(pref[col[label]]), _float(common[col[label]])) for label in ("선행PER+1", "선행PER+2")]
        ok = all(p is not None and c is not None and abs(p - c * ratio) <= 0.01 for p, c in pairs) \
            and pref[col["기관수"]] == common[col["기관수"]]
        print(f"    {'ok  ' if ok else 'FAIL'} 우선주 선행PER+1·+2 = 보통주 값 × 가격 비율 {pairs}, "
              f"기관수 {pref[col['기관수']]} = 보통주 {common[col['기관수']]}")
        if not ok:
            problems.append("stock_compare 우선주 행의 선행PER+1·+2·기관수가 보통주 컨센서스가 아니다")
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


def _plain(cell):
    """'적자(-14.34)'·'자본잠식(-67.45)' → 괄호 안 값. 그 밖의 칸은 그대로."""
    m = re.fullmatch(r"(?:적자|자본잠식)\((.*)\)", str(cell))
    return m.group(1) if m else cell


# 라벨 규칙(사용자 결정 2026-09-25): (기준 항목, 배수 항목, 라벨, 배수가 음수면 라벨인가).
# EPS가 음수면 PER은 '적자(원래 값)'. BPS가 음수(자본잠식)면 PBR·ROE는 '자본잠식(원래 값)' — 보로노이
# 2027E는 적자를 음수 자본으로 나눠 ROE +851.61%. ROE는 적자면 음수가 정상이라 음수 자체는 라벨 조건이 아니다.
LABEL_RULES = [("EPS", "PER", "적자", True), ("BPS", "PBR", "자본잠식", True), ("BPS", "ROE", "자본잠식", False)]


def loss_label_problems(sections):
    """적자·자본잠식 라벨이 규칙과 어긋난 칸(부호만 붙은 음수 배수 포함)."""
    triples = []  # (위치, 기준 칸, 배수 칸, 라벨, 음수 배수도 라벨 대상인가)
    main = (sections.get("실적·추정") or [None])[0]
    if main:
        _, header, rows = main
        col = {label.split("(")[0]: index for index, label in enumerate(header)}
        for base, target, label, by_value in LABEL_RULES:
            triples += [(f"{r[0]} {target}", r[col[base]], r[col[target]], label, by_value)
                        for r in rows if len(r) == len(header)]
    for title, header, rows in sections.get("컨센서스 추이") or []:
        named = {r[0].split("(")[0]: r for r in rows}
        for base, target, label, by_value in LABEL_RULES:
            if base in named and target in named:
                triples += [(f"{title[:10]} {header[n]} {target}", named[base][n], named[target][n], label, by_value)
                            for n in range(1, min(len(named[base]), len(named[target])))]
    issues = []
    for where, base_cell, cell, label, by_value in triples:
        base, labeled = _float(base_cell), cell.startswith(label)
        should = (base is not None and base < 0) or (by_value and (_float(_plain(cell)) or 0) < 0)
        if (by_value and (_float(cell) or 0) < 0) or labeled != should:
            issues.append(f"{where}: 기준 {base_cell}, 칸 {cell}")
    return issues


# WiseReport가 응답을 멈추거나 막는 실패 — GitHub 러너 IP에서 가끔 난다(2026-09-25 CI 5회 중 2회, 전부
# 타임아웃. 로컬·Render에선 재현 안 됨). 우리 코드 문제가 아니라 경고로 건너뛴다. 404·형식 오류는 주소나
# 응답 형식이 바뀐 것일 수 있어 그대로 FAIL이다. 2026-09-27 CI에선 연결을 도중에 끊는 SSL 오류(`[SSL:
# UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol`)도 나왔다 — 다시 불러도 같았다.
EXTERNAL = ("timed out", "HTTP Error 403", "HTTP Error 429", "HTTP Error 5", "Connection reset", "10054",
            "Remote end closed", "UNEXPECTED_EOF", "EOF occurred")
skipped = []


def external_failure(text):
    return any(marker in str(text) for marker in EXTERNAL)


def skip_if_external(label, output):
    """외부 실패면 경고를 찍고 True. 검사를 건너뛴 목록은 [9] 끝에 모아 보여 준다."""
    if not external_failure(output):
        return False
    reason = next((line for line in str(output).splitlines() if "실패" in line or "__error__" in line), str(output))
    print(f"    warn {label} — WiseReport 응답 없음, 건너뜀 ({reason[:70]})")
    skipped.append(label)
    return True


def call_consensus(label, *args):
    """stock_consensus를 부른다. 외부 원인이 아닌 조회 실패(빈 응답 등)가 섞이면 한 번 더 부른다 — 두 번 다
    실패해야 FAIL이 된다. 2026-09-25 CI에서 SK하이닉스 연간 첫 호출만 표가 없었고, 같은 실행의 다음 호출은
    정상이었다(타임아웃이 아니라 경고로 건너뛰지도 못했다). 주소가 바뀐 404 같은 문제는 다시 불러도 실패한다.
    """
    output = server.stock_consensus(*args)
    if "조회 실패" in output and not external_failure(output):
        reason = next((line for line in output.splitlines() if "실패" in line), "")
        print(f"    warn {label} — 조회 실패가 섞여 한 번 더 불렀다 ({reason[:70]})")
        output = server.stock_consensus(*args)
    return output


def note_problems(output):
    """stock_consensus 출력이 스스로 맞는가 — 각주가 붙을 조건과 각주가 함께 가야 한다.

    ① 투자의견 행만 있는 추이 표가 없어야 한다(추정이 없는 기간 — 저스템 2028년이 그렇게 나왔다) ② 영업이익 > 매출액
    행이 있으면 그 각주 ③ 서프라이즈 매출액 실적이 위 표 (A)와 5% 넘게 다르면 그 각주 ④ 각주의 기준 주가 ÷ EPS·BPS가
    (E) PER·PBR과 맞는지 — 안 맞는 칸이 있을 때만 '맞지 않습니다'. → (문제 목록, 검산한 칸 수)
    """
    sections = consensus_tables(output)
    main = (sections.get("실적·추정") or [None])[0]
    if main is None:
        return [], 0
    _, header, rows = main
    col = {label.split("(")[0]: index for index, label in enumerate(header)}
    issues = [f"투자의견만 있는 추이 표: {title[:10]}" for title, _, trows in sections.get("컨센서스 추이") or []
              if trows and all(t[0].startswith("투자의견") for t in trows)]
    op_over = [r[0] for r in rows if (_float(r[col["매출액"]]) or 0) > 0
               and (_float(r[col["영업이익"]]) or 0) > _float(r[col["매출액"]])]
    if bool(op_over) != ("영업이익이 매출액보다 큰 기간이 있습니다" in output):
        issues.append(f"영업이익 > 매출액 각주가 어긋났다(해당 행 {op_over})")
    gaps = []
    for _, _, srows in sections.get("어닝서프라이즈") or []:
        for s in srows:
            actual = _float(s[2].split(" (")[0])
            same = [r for r in rows if "(A)" in r[0] and r[0].startswith(s[0].replace("/", "."))]
            listed = _float(same[0][col["매출액"]]) if len(same) == 1 else None
            if s[1] == "매출액" and actual and listed and abs(listed / actual - 1) > 0.05:
                gaps.append(s[0])
    if bool(gaps) != ("매출액 실적이 위 표 (A)와 5% 넘게 다릅니다" in output):
        issues.append(f"서프라이즈 매출액 각주가 어긋났다(5% 넘게 다른 결산기 {gaps})")
    m = re.search(r"정규장 종가 ([\d,]+)원 기준", output)
    off, checked = [], 0
    if m:
        price = int(m.group(1).replace(",", ""))
        for r in rows:
            for multiple, per_share in (("PER", "EPS"), ("PBR", "BPS")):
                shown, value = _float(_plain(r[col[multiple]])), _float(r[col[per_share]])
                if "(E)" not in r[0] or shown is None or not value:
                    continue
                checked += 1
                if abs(shown - price / value) > 0.005 + price * 0.5 / value ** 2 + 1e-9:
                    off.append(f"{r[0]} {multiple} {r[col[multiple]]} vs {price:,} ÷ {r[col[per_share]]}")
    if bool(off) != ("맞지 않습니다" in output):
        issues.append(f"기준 주가 각주와 검산이 다르다(어긋난 칸 {off[:2]})")
    return issues, checked


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

    for code, name in [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("011170", "롯데케미칼")]:
        rows_with_opinion = opinion_rows(code)
        for period in ("annual", "quarter"):
            tag = f"{name} {period}"
            output = call_consensus(tag, code, period)
            if skip_if_external(tag, output):
                continue
            sections = consensus_tables(output)
            # 추정기관수 = 제공처별 투자의견 표의 투자의견 수. (E) PER·PBR = 각주의 기준일 정규장 종가 ÷ EPS·BPS —
            # 이 세 종목은 맞아야 한다(전제: 2026-09-27 109종목 중 108개. 어긋나면 FnGuide 계산이 바뀐 것이니 각주를 볼 것).
            # 도구나 이 검사의 페이지 조회가 외부 장애로 실패하면(러너 IP) 대조만 건너뛴다. 형식 오류 같은 실패는 FAIL.
            count = re.search(r"^추정기관수: (\d+)곳", output, re.M)
            lookup = re.search(r"^추정기관수: 조회 실패 \((.*)\)$", output, re.M)
            counted = "추정기관수 대조 건너뜀(WiseReport 조회 실패)"
            if not (lookup and external_failure(lookup.group(1))) and rows_with_opinion is not None:
                counted = f"추정기관수 {rows_with_opinion}곳 일치"
                if count is None or int(count.group(1)) != rows_with_opinion:
                    print(f"    FAIL {tag} 추정기관수 {count.group(1) if count else lookup and lookup.group(0)} "
                          f"≠ 투자의견 낸 증권사 {rows_with_opinion}곳")
                    problems.append(f"stock_consensus({tag}) 추정기관수가 최근 3개월 투자의견 수와 다르다")
            issues, priced = note_problems(output)
            # 기준 주가는 추이 응답의 기준일로 받는다 — 추이가 외부 장애로 다 빠지면 가격을 못 적으니 검산만 건너뛴다.
            if not priced and external_failure(output):
                print(f"    warn {tag} 기준 주가 검산 건너뜀 — 추이 조회 실패로 기준일을 모름")
            elif "맞지 않습니다" in output or not priced:
                issues.append(f"(E) PER·PBR이 기준일 정규장 종가 ÷ EPS·BPS로 검산되지 않는다(검산 {priced}칸)")
            if issues:
                print(f"    FAIL {tag} {issues[:3]}")
                problems.append(f"stock_consensus({tag}) 각주·검산이 어긋났다")
            else:
                print(f"    ok   {tag} {counted}, (E) PER·PBR = 기준 주가 ÷ EPS·BPS {priced}칸")
            # 분기 각주(연환산 아님)는 분기 표에만 붙어야 한다 — 연간 표에 붙은 회귀가 있었다(if/else 어긋남).
            if ("분기의 PER·ROE·EV/EBITDA" in output) != (period == "quarter"):
                print(f"    FAIL {tag} 분기 각주가 {'없다' if period == 'quarter' else '연간 표에 붙었다'}")
                problems.append(f"stock_consensus({tag}) 분기 각주가 어긋났다")
            main = (sections.get("실적·추정") or [None])[0]
            if main is None:
                print(f"    FAIL {tag} 실적·추정 표가 없다: {output.splitlines()[0][:90]}")
                problems.append(f"stock_consensus({tag}) 표가 없다")
                continue
            _, header, rows = main
            col = {label: index for index, label in enumerate(header)}
            by_period = {r[0][:7]: r for r in rows}
            # 표의 칸은 원본 그대로여야 한다 — 적자 기간 PER만 '적자(원래 값)', 결산 주기가 1년이 아닌 표의
            # YoY만 다시 계산한 값이다.
            raw = fetch(server._consensus_url(code, 2, 0 if period == "annual" else 1))
            if "__error__" in raw:
                skip_if_external(f"{tag} 표 원본", raw) or print(
                    f"    warn {tag} 표 원본 조회 실패 — 원본 대조만 건너뜀 ({raw['__error__']})")
            else:
                keys = [k for k, _ in server._CNS_COLUMNS]
                recomputed = "결산 주기가 1년이 아닙니다" in output
                off = []
                for r, source in zip(rows, raw.get("JsonData") or []):
                    for key, cell in zip(keys, r[1:]):
                        want = str(source.get(key) or "-")
                        if key == "PER" and (server._negative(source.get("PER")) or server._negative(source.get("EPS"))):
                            want = f"적자({want})" if want != "-" else "적자"
                        if (key == "PBR" and (server._negative(source.get("PBR")) or server._negative(source.get("BPS")))
                                or key == "ROE" and server._negative(source.get("BPS"))):
                            want = f"자본잠식({want})" if want != "-" else "자본잠식"
                        if key == "YOY" and recomputed:
                            continue
                        if cell != want:
                            off.append(f"{r[0]} {key} 표 {cell} vs 원본 {want}")
                if off or len(rows) != len(raw.get("JsonData") or []):
                    print(f"    FAIL {tag} 표가 원본과 다르다: {off[:3]} (행 {len(rows)} vs {len(raw.get('JsonData') or [])})")
                    problems.append(f"stock_consensus({tag}) 표 칸이 원본과 다르다")
            issues = loss_label_problems(sections)
            if issues:
                print(f"    FAIL {tag} 적자 PER 표시가 어긋났다: {issues[:3]}")
                problems.append(f"stock_consensus({tag}) 적자 PER 표시가 어긋났다")
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
                if "__error__" in raw:
                    skip_if_external(f"{tag} 추이 {title[:10]} 원본", raw) or print(
                        f"    warn {tag} 추이 {title[:10]} 원본 조회 실패 — 원본 대조만 건너뜀 ({raw['__error__']})")
                items = [] if "__error__" in raw else raw.get("JsonData") or []
                shown_rows = {t[0]: t for t in trend_rows}
                eps_values, bps_values = (next(([i.get(f"VAL{n}") for n in range(1, 6)] for i in items
                                                if str(i.get("ACC_NM", "")).startswith(base)), [None] * 5)
                                          for base in ("EPS", "BPS"))
                neg = server._negative
                for item in items:
                    acc_name, values = item.get("ACC_NM", ""), [item.get(f"VAL{n}") for n in range(1, 6)]
                    present = [isinstance(v, (int, float)) and v != 0 for v in values]
                    cells = shown_rows.get(acc_name)
                    digits = 1 if "(억원)" in acc_name else 0 if "(원)" in acc_name else 2
                    # 적자 시점 PER은 '적자(원래 값)', 자본잠식 시점 PBR·ROE는 '자본잠식(원래 값)' — 원래 값이
                    # 없으면 라벨만(LABEL_RULES와 같은 규칙).
                    if acc_name.startswith("PER"):
                        label, flags = "적자", [neg(v) or neg(e) for v, e in zip(values, eps_values)]
                    elif acc_name.startswith("PBR"):
                        label, flags = "자본잠식", [neg(v) or neg(b) for v, b in zip(values, bps_values)]
                    elif acc_name.startswith("ROE"):
                        label, flags = "자본잠식", [neg(b) for b in bps_values]
                    else:
                        label, flags = "", [False] * 5
                    if cells is None:
                        wrong = any(present) or any(flags)
                    else:
                        labels_wrong = any((cell != label if not ok else not cell.startswith(label + "(")) if flag
                                           else cell.startswith(("적자", "자본잠식"))
                                           for ok, cell, flag in zip(present, cells[1:], flags))
                        values_wrong = any(_plain(cell) != "-" if not ok else
                                           _float(_plain(cell)) is None
                                           or abs(_float(_plain(cell)) - v) > 0.5 * 10 ** -digits + 1e-9
                                           for v, ok, cell in zip(values, present, cells[1:]))
                        wrong = labels_wrong or values_wrong
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
                    a, b = (_float(_plain(cells[1])) if cells else None), _float(_plain(row[col[label]]))
                    if a is not None and b is not None and abs(a - b) > tolerance:
                        trend_off += 1
                        print(f"    FAIL {tag} 추이 {title[:10]} 기준일 {acc_name} {cells[1]} ≠ 표 {row[col[label]]}")
                        problems.append(f"stock_consensus({tag}) 추이 기준일 값이 표와 다르다: {title[:10]} {acc_name}")
            # 서프라이즈(%) = (실적 − 추정) ÷ |추정| — 추정이 음수여도(SK하이닉스 2023 영업이익) 이 식이다.
            # FnGuide가 주는 %는 음수 추정에서 부호가 뒤집히거나 표의 추정과 안 맞을 때가 있어 도구가
            # 직접 계산하고, 원본과 다른 칸에만 ✎를 단다. 원본 %를 따로 받아 ✎ 판정도 대조한다.
            shown, shown_ok = {}, True
            for acc, account in server._CNS_SURPRISE:
                data = fetch(server._consensus_url(code, 5, frq, acc_cd=acc))
                if "__error__" in data:
                    # 원본 %를 못 받으면 ✎ 판정만 건너뛴다(도구 쪽 검산은 그대로 한다).
                    shown_ok = False
                    skip_if_external(f"{tag} 서프라이즈 원본 {account}", data) or print(
                        f"    warn {tag} 서프라이즈 원본 {account} 조회 실패 — ✎ 대조만 건너뜀 ({data['__error__']})")
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
                        if shown_ok and marked != should:
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
    mismatched, failed, checked_names, noted = [], [], 0, []
    # SK스퀘어는 지금 영업이익 > 매출액·서프라이즈 매출액 차이·기준 주가 불일치 각주가 모두 붙는 종목이다(2026-09-27).
    for code, name in STOCKS + [("039440", "에스티아이"), ("310210", "보로노이"), ("402340", "SK스퀘어"),
                                ("417840", "저스템")]:
        output = call_consensus(name, code)
        if skip_if_external(f"{name} 추정 없음 안내", output):
            continue
        checked_names += 1
        main = (consensus_tables(output).get("실적·추정") or [None])[0]
        if main is None:
            failed.append(name)
            continue
        issues = loss_label_problems(consensus_tables(output)) + note_problems(output)[0]
        # 각주는 해당 칸이 있을 때만 — 자본잠식 라벨, 음수 EV/EBITDA(값은 두고 각주만).
        cells = [c for tables in consensus_tables(output).values() for _, _, rows in tables for r in rows for c in r[1:]]
        ev_negative = any((_float(r[10]) or 0) < 0 for r in main[2] if len(r) == 11)
        if any(c.startswith("자본잠식") for c in cells) != ("자본잠식)면" in output):
            issues.append("자본잠식 각주가 칸과 어긋남")
        if ev_negative != ("EV/EBITDA가 음수인 칸은" in output):
            issues.append(f"EV/EBITDA 각주가 어긋남(음수 칸 {ev_negative})")
        if issues:
            print(f"    FAIL {name} 표시·각주가 어긋났다: {issues[:3]}")
            noted.append(name)
            problems.append(f"stock_consensus({name}) 표시·각주가 어긋났다")
        estimates = [r for r in main[2] if "(E)" in r[0]]
        blank = all(c == "-" for r in estimates for c in r[1:])
        if blank != ("현재 컨센서스 추정치가 없습니다" in output):
            mismatched.append(name)
    if failed or mismatched:
        print(f"    FAIL 표가 없음 {failed} / 추정 없음 안내 불일치 {mismatched}")
        problems.append(f"stock_consensus 표 없음 {failed} 또는 추정 없음 안내 불일치 {mismatched}")
    elif checked_names and not noted:
        print(f"    ok   {checked_names}종목 모두 표가 나오고, 추정이 빈 종목에만 '추정치가 없습니다' — 적자·자본잠식 "
              "표시와 각주(영업이익 > 매출액·서프라이즈 매출액 차이·기준 주가 검산)가 조건과 일치, 투자의견만 있는 추이 표 없음")
    # 결산 주기가 6개월인 리츠는 원본 YoY에 직전 결산기 대비가 섞여 있다(한화리츠 2026.04: 원본 3.50%,
    # 1년 전 대비 7.53%). 도구의 YoY는 1년 전 같은 결산기 대비이거나, 그 결산기가 표에 없으면 비어야 한다.
    # 한화리츠가 결산 주기를 바꾸거나 상장폐지되면 다른 6개월 결산 리츠(롯데리츠 330590 등)로 바꿀 것.
    output = call_consensus("한화리츠", "451800")
    main = (consensus_tables(output).get("실적·추정") or [None])[0]
    if skip_if_external("한화리츠 6개월 결산 YoY", output):
        pass
    elif main is None:
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
        output = call_consensus(label, code)
        if skip_if_external(label, output):
            continue
        ok = "데이터가 없습니다" in output
        print(f"    {'ok  ' if ok else 'FAIL'} {label} → 데이터 없음 안내")
        if not ok:
            problems.append(f"stock_consensus {label} 안내가 어긋났다")

    # brief=True는 실적·추정 표가 전체 출력과 같고 추이·서프라이즈가 없어야 한다(출력 크기 리뷰 2026-09-27).
    full, brief = call_consensus("삼성전자 전체", "005930"), call_consensus("삼성전자 brief", "005930", "annual", True)
    if not (skip_if_external("삼성전자 전체", full) or skip_if_external("삼성전자 brief", brief)):
        same = consensus_tables(full).get("실적·추정") == consensus_tables(brief).get("실적·추정")
        extra = [s for s in ("[컨센서스 추이]", "[어닝서프라이즈]") if s in brief]
        # 추정기관수 줄은 두 호출 중 하나라도 외부 장애로 조회에 실패했으면 비교에서 뺀다(러너 IP). 전체 출력의 추이·
        # 서프라이즈가 조회 실패로 빠졌으면 분량 비교도 뜻이 없다.
        keep = not any(l.startswith("추정기관수: 조회 실패") and external_failure(l)
                       for text in (full, brief) for l in text.split("\n"))
        head = [[l for l in text.split("\n") if l.startswith("종목") or (keep and l.startswith("추정기관수"))]
                for text in (full, brief)]
        shorter = len(brief) < len(full) * 0.45 or "조회 실패" in full
        ok = same and not extra and head[0] == head[1] and "brief=True라" in brief and shorter
        print(f"    {'ok  ' if ok else 'FAIL'} brief — 표·머리 같음 {same and head[0] == head[1]}, 뺀 섹션 남음 {extra}, "
              f"{len(brief):,}자 / 전체 {len(full):,}자")
        if not ok:
            problems.append("stock_consensus brief 출력이 어긋났다")

    fake_consensus()
    if skipped:
        print(f"    warn WiseReport 응답이 없어 건너뛴 검사 {len(skipped)}개 — 로컬에서 python verify.py로 다시 볼 것")


def fake_consensus():
    """가짜 응답으로 본다 — 살아 있는 종목은 바뀌어 검사가 공회전할 수 있다(저스템에 2028년 추정이 생기는 등).

    투자의견만 있는 추이 기간(저스템 2028년), 영업이익 > 매출액(SK스퀘어), 서프라이즈 매출액이 위 표와 5% 넘게 다름,
    기준 주가 ÷ EPS와 맞지 않는 PER(SK스퀘어), 추정치는 있는데 추정기관수 0곳(케이씨)을 한 종목에 모았다.
    """
    def cells(**values):
        keys = ("SALES", "YOY", "OP", "NP", "EPS", "BPS", "PER", "PBR", "ROE", "EV")
        return {"MAIN": "IFRS연결", **{k: "" for k in keys}, **values}

    table = {"JsonData": [
        cells(YYMM="2024.12(A)", SALES="1,000.0", OP="100.0", NP="80.0", EPS="800", BPS="10,000", PER="10.00"),
        cells(YYMM="2025.12(A)", SALES="1,100.0", YOY="10.00", OP="120.0", NP="90.0", EPS="900", BPS="10,500",
              PER="11.00"),
        cells(YYMM="2026.12(E)", SALES="1,200.0", YOY="9.09", OP="1,500.0", NP="100.0", EPS="1,000", PER="10.00"),
        cells(YYMM="2027.12(E)", SALES="1,300.0", YOY="8.33", OP="150.0", NP="125.0", EPS="1,250", PER="8.10"),
        cells(YYMM="2028.12(E)")]}

    def trend(sales):
        return {"JsonData": [
            {"ACC_NM": "투자의견(점수)", "DT": "20260923", **{f"VAL{n}": 4.0 for n in range(1, 6)}},
            {"ACC_NM": "매출액(억원)", "DT": "20260923", **{f"VAL{n}": sales for n in range(1, 6)}}]}

    surprise = {"tableData": {"tableHeaderData": [{"CNS_FY_2": "2023", "CNS_FY_1": "2024", "CNS_FY0": "2025"}],
                              "tableData": [{"QTR": "연간실적(A)", "FY0": "2,000.0", "FY0_S": "2026/02/10"},
                                            {"QTR": "발표직전(E)", "FY0": "1,900.0", "FY0_S": 5.26}]}}

    def fake(url):
        query = dict(urllib.parse.parse_qsl(url.split("?", 1)[-1]))
        return {"2": table, "1": {"JsonData": [{"YYMM": "202612"}, {"YYMM": "202712"}, {"YYMM": "202812"}]},
                "4": trend(None if query.get("yymm") == "202812" else 1200.0),
                "5": surprise if query.get("acc_cd") == "121000" else {"tableData": {"tableData": []}}}[query["flag"]]

    saved = server._fetch, server._analyst_count, server._regular_close_on
    server._fetch, server._analyst_count, server._regular_close_on = fake, lambda code: (0, None), lambda code, d: 10000
    try:
        full, brief = server.stock_consensus("000000"), server.stock_consensus("000000", brief=True)
    finally:
        server._fetch, server._analyst_count, server._regular_close_on = saved
    want = {
        "추정기관수 0곳 → 기관 수 알 수 없음": "추정기관수: 0곳 — 최근 3개월 투자의견을 낸 증권사가 없어" in full,
        "추정 없는 2028년 추이 빠짐": "2028.12(E) —" not in full and "2027.12(E) —" in full,
        "영업이익 > 매출액 각주": "영업이익이 매출액보다 큰 기간이 있습니다(2026.12(E))" in full,
        "서프라이즈 매출액 차이 각주": "5% 넘게 다릅니다(2025 2,000.0 vs 1,100.0" in full,
        "기준 주가·불일치 각주": "정규장 종가 10,000원 기준입니다. 다만 이 종목은" in full and "(2027.12(E) PER 8.10" in full,
        "자기 검산 통과": not note_problems(full)[0] and not note_problems(brief)[0],
        "brief: 추이·서프라이즈·차이 각주 없음": "[컨센서스 추이]" not in brief and "[어닝서프라이즈]" not in brief
                                             and "5% 넘게 다릅니다" not in brief and "영업이익이 매출액보다" in brief,
    }
    wrong = [k for k, v in want.items() if not v]
    print(f"    {'ok  ' if not wrong else 'FAIL'} 가짜 응답 {len(want)}항목" + (f" — 어긋남: {wrong}" if wrong else ""))
    if wrong:
        problems.append(f"stock_consensus 가짜 응답 검사 실패: {wrong}")


def check_financials():
    print("\n" + "=" * 62)
    print("[10] stock_financials — 적자 PER 표시, 분기 PER·ROE는 최근 4분기 합산(TTM)")
    print("=" * 62)
    # 전제: 분기 표의 PER·ROE는 최근 4분기 합산이다 — 12월 분기 값이 그해 연간 값과 같다(2026-09-25 20종목
    # 모두, EPS·영업이익률은 0/20). 달라지면 분기 각주('최근 4분기 합산')와 PER 적자 판정 방식을 고칠 것.
    broken, compared = [], 0
    for code, name in STOCKS[:6] + [("011170", "롯데케미칼")]:
        annual, quarter = (fetch(f"https://m.stock.naver.com/api/stock/{code}/finance/{p}") for p in ("annual", "quarter"))
        if "__error__" in annual or "__error__" in quarter:
            continue
        a_info, q_info = annual.get("financeInfo") or {}, quarter.get("financeInfo") or {}
        a_keys = {t.get("key") for t in a_info.get("trTitleList") or [] if t.get("isConsensus") != "Y"}
        december = [t.get("key") for t in q_info.get("trTitleList") or []
                    if t.get("isConsensus") != "Y" and str(t.get("key", "")).endswith("12") and t.get("key") in a_keys]
        if not december:
            continue

        def value(info, title, key=december[-1]):
            row = next((r for r in info.get("rowList") or [] if r.get("title") == title), {})
            return ((row.get("columns") or {}).get(key) or {}).get("value")

        compared += 1
        for title in ("PER", "ROE"):
            if value(a_info, title) != value(q_info, title):
                broken.append(f"{name} {december[-1]} {title} 연간 {value(a_info, title)} vs 분기 {value(q_info, title)}")
    if broken or not compared:
        print(f"    FAIL 분기 PER·ROE가 최근 4분기 합산이 아닌 것 같다({compared}종목 비교): {broken[:3]}")
        problems.append("stock_financials 분기 PER·ROE 기준(최근 4분기 합산) 전제가 깨졌다 — 각주를 고칠 것")
    else:
        print(f"    ok   전제 — 12월 분기 PER·ROE = 그해 연간 값 ({compared}종목)")

    # 음수 PER은 '적자(원래 값)', 연간은 PER이 비어도 EPS가 음수면 '적자'(보로노이 2021). 분기는 PER 부호로만
    # 판정한다 — 분기 EPS가 흑자여도 최근 4분기가 적자면 PER이 음수다(롯데케미칼 2026.03).
    labeled = 0
    for code, name in [("011170", "롯데케미칼"), ("310210", "보로노이"), ("005930", "삼성전자")]:
        for period in ("annual", "quarter"):
            tag = f"{name} {period}"
            raw = fetch(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}")
            if "__error__" in raw:
                print(f"    warn {tag} 원본 조회 실패 — 건너뜀 ({raw['__error__']})")
                continue
            info = raw.get("financeInfo") or {}
            columns = {r.get("title"): r.get("columns") or {} for r in info.get("rowList") or []}
            want = []
            for key in [t.get("key") for t in info.get("trTitleList") or []]:
                per = str((columns.get("PER", {}).get(key) or {}).get("value", "-"))
                eps = (columns.get("EPS", {}).get(key) or {}).get("value")
                if server._negative(per) or (period == "annual" and server._negative(eps)):
                    per = f"적자({per})" if server._num(per) is not None else "적자"
                want.append(per)
            output = server.stock_financials(code, period)
            line = next((l for l in output.split("\n") if l.startswith("PER")), "")
            got = [c.strip() for c in line.split(": ", 1)[-1].split("|")]
            labeled += sum(c.startswith("적자") for c in got)
            wrong = []
            if got != want:
                wrong.append(f"PER {got} vs 기대 {want}")
            if ("최근 4분기 합산(TTM)" in output) != (period == "quarter"):
                wrong.append("분기 각주(최근 4분기 합산)가 어긋났다")
            if ("PER은 음수(적자)면" in output) != any(c.startswith("적자") for c in got):
                wrong.append("적자 각주가 어긋났다")
            if wrong:
                print(f"    FAIL {tag} {wrong[:2]}")
                problems.append(f"stock_financials({tag}) 적자 PER 표시·각주가 어긋났다")
    print(f"    ok   적자 PER 표시 {labeled}칸 확인 (흑자 종목은 숫자 그대로), 분기 각주·적자 각주 위치" if labeled else
          "    warn 적자 PER 칸이 하나도 없다 — 검사 대상 종목을 적자 종목으로 바꿀 것")


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
    check_financials()
    print("\n" + "=" * 62)
    if problems:
        print(f"문제 {len(problems)}건")
        for item in problems:
            print(f"  - {item}")
        sys.exit(1)
    print("문제 없음")


if __name__ == "__main__":
    main()
