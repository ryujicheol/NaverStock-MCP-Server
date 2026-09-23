#!/usr/bin/env python3
"""Korean Stock Remote MCP Server — 네이버 증권 기반 한국 주식 데이터 원격 MCP 서버

원본(stdio 방식, agent504330-ux/mcp-korean-stock)을 claude.ai 커스텀 커넥터에서
쓸 수 있도록 Streamable HTTP 트랜스포트로 개조한 버전입니다.

- 공개 인터넷에 배포한 뒤 claude.ai 설정 > 커넥터 > 커스텀 커넥터 추가에서
  URL(예: https://<your-host>/mcp)을 등록하면 모바일/데스크톱/웹에 동기화됩니다.
- 데이터 출처: 네이버 증권 모바일 API. 개인 용도로만 사용하세요.
- 인증 없음(authless), 읽기 전용(GET 요청만). 외부로 데이터를 보내지 않습니다.

도구:
  - stock_price          : 종목 현재가
  - stock_detail         : 상세 정보 (시가/고가/저가/거래량/시총/PER/PBR/컨센서스 등)
  - stock_search         : 종목명으로 종목코드 검색
  - market_index         : KOSPI/KOSDAQ 지수
  - stock_news           : 종목 관련 최신 뉴스
  - stock_investor_trend : 일별 투자자 수급 (외국인/기관/개인 순매수)
  - stock_financials     : 실적 추이 (분기/연간, 컨센서스 추정 포함)
  - stock_compare        : 여러 종목 밸류에이션·수익성 비교 (스크리닝용, 최대 50종목)
"""

import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

NAVER_STOCK_API = "https://m.stock.naver.com/api"
NAVER_POLLING_API = "https://polling.finance.naver.com/api/realtime/domestic/stock"
NAVER_CHART_API = "https://api.stock.naver.com/chart/domestic/item"
NAVER_SEARCH_API = "https://ac.stock.naver.com/ac"
KST = timezone(timedelta(hours=9))
USER_AGENT = "Mozilla/5.0 (Macintosh; Apple Silicon) MCP-Korean-Stock/1.0"

# 호스팅 환경(Render, Cloud Run 등)은 PORT 환경변수로 포트를 지정합니다.
PORT = int(os.environ.get("PORT", "8000"))

# stateless_http=True: 각 요청을 독립 처리 → 서버리스/다중 인스턴스 호스팅에 안정적.
mcp = FastMCP("korean-stock", host="0.0.0.0", port=PORT, stateless_http=True)


def _fetch(url: str):
    """네이버 API에 GET 요청을 보내고 JSON을 파싱합니다. 실패 시 {'error': ...} 반환."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 - 어떤 실패든 호출자에게 메시지로 전달
        return {"error": str(e)}


def _fmt_datetime(raw):
    """'202609221911' -> '2026-09-22 19:11'. 형식이 다르면 원본을 그대로 둔다."""
    text = str(raw or "").strip()
    if text.isdigit() and len(text) in (12, 14):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
    return text


def _to_int(text):
    """'-1,088,039' / '+1,379,866' 형태의 문자열을 정수로. 변환 불가면 None."""
    if not isinstance(text, str):
        return None
    try:
        return int(text.replace(",", "").replace("+", "").strip())
    except ValueError:
        return None


def _fetch_quotes(codes):
    """여러 종목의 실시간 시세를 한 요청으로 받는다 → ({종목코드: 시세}, 조회 시각).

    한 응답이라 모든 종목이 같은 시점 가격이고, 시총도 그 가격으로 계산돼 있다.
    없는 코드·상장폐지 코드는 응답에서 조용히 빠지므로 호출자가 누락을 확인해야 한다.
    """
    joined = urllib.parse.quote(",".join(codes), safe=",")
    data = _fetch(f"{NAVER_POLLING_API}/{joined}")
    if not isinstance(data, dict) or "error" in data:
        return {}, ""
    quotes = {q.get("itemCode"): q for q in data.get("datas") or []}
    return quotes, _fmt_datetime(data.get("time"))


# 네이버 현재가는 KRX 시세다. 2026-09-14 KRX 애프터마켓(16:00~20:00 실시간 매매)이 열린 뒤로는
# 정규장이 끝나도 20:00까지 움직이고, 20:00 이후의 '종가'도 애프터마켓 종가다 — 2026-09-22
# 삼성전자는 정규장 종가 276,500원, 네이버 종가 277,500원(애프터마켓 종가). 전일대비는 다음 날
# 기준가인 정규장 종가 대비다. ETF·ETN은 애프터마켓에서 거래되지 않아 그 시간에 장 마감으로 나온다.
# 세션 값은 네이버 프런트엔드 코드와 같은 이름이다.
_BASIS_CAVEAT = {
    "정규장 실시간": "",
    "애프터마켓 실시간": "20:00까지 바뀌며 정규장 종가(15:30)와 다를 수 있습니다",
    "장 마감 후 마지막 체결가": "애프터마켓에서 거래된 종목은 애프터마켓 종가(20:00)라 정규장 종가(15:30)와 다를 수 있습니다",
}


def _price_basis(quote):
    """현재가가 어느 시세인지 — _BASIS_CAVEAT의 키, 모르는 상태면 원래 값을 그대로."""
    session, status = quote.get("marketSessionType"), quote.get("marketStatus")
    if status == "OPEN" and session == "regularMarket":
        return "정규장 실시간"
    if status == "OPEN" and session == "afterMarket":
        return "애프터마켓 실시간"
    if status in ("CLOSE", "PREOPEN") or session == "preMarket":
        return "장 마감 후 마지막 체결가"
    return f"확인되지 않은 시장 상태 (marketStatus={status}, marketSessionType={session})"


def _with_caveat(basis):
    caveat = _BASIS_CAVEAT.get(basis)
    return f"{basis} — {caveat}" if caveat else basis


def _trading_dates(code):
    """최근 20일 안의 거래일 목록('YYYYMMDD', 오래된 순). 연휴가 길어도 직전 거래일이 들어온다."""
    today = datetime.now(KST)
    days = _fetch(f"{NAVER_CHART_API}/{code}/day?startDateTime={today - timedelta(days=20):%Y%m%d}0000"
                  f"&endDateTime={today:%Y%m%d}2359")
    return [d.get("localDate", "") for d in days] if isinstance(days, list) else []


def _regular_close_on(code, date):
    """그날 정규장 종가(정수). 분봉은 KRX 단독이라 15:30 이하 마지막 봉이 정규장 종가다.

    현재가·일봉 종가는 애프터마켓 종가라 쓸 수 없다. 2026-09-23 삼성전자 285,500원·SK하이닉스
    1,862,000원으로 뉴스의 정규장 종가와 일치했다(현재가는 286,500원·1,863,000원).
    """
    bars = _fetch(f"{NAVER_CHART_API}/{code}/minute?startDateTime={date}0900&endDateTime={date}1530")
    if not isinstance(bars, list) or not bars:
        return None
    price = bars[-1].get("currentPrice")
    return int(price) if isinstance(price, (int, float)) else None


def _regular_close(code):
    """가장 최근 거래일의 정규장 종가 → (가격, 'YYYY-MM-DD'), 없으면 None.

    개장 전에 그날 봉이 먼저 생겨도 정규장 봉이 없으면 직전 거래일로 넘어간다.
    """
    for date in reversed(_trading_dates(code)[-2:]):
        price = _regular_close_on(code, date)
        if price:
            return price, f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return None


@mcp.tool()
def stock_price(code: str) -> str:
    """한국 주식 현재가를 조회합니다. 종목코드(예: 005930=삼성전자, 039440=에스티아이)를 입력하세요.
    정규장이 끝나도 KRX 애프터마켓(16:00~20:00) 동안 현재가가 움직이므로 가격 기준을 함께 표시하고,
    정규장 밖에서는 정규장 종가도 따로 보여줍니다."""
    quotes, fetched_at = _fetch_quotes([code])
    data = quotes.get(code)
    if not data:
        return f"종목코드 {code} 조회 실패"

    name = data.get("stockName", code)
    price = data.get("closePrice", "N/A")
    change = data.get("compareToPreviousClosePrice", "N/A")
    ratio = data.get("fluctuationsRatio", "N/A")
    direction = data.get("compareToPreviousPrice", {}).get("text", "")

    lines = [
        f"{name} ({code})",
        f"현재가: {price}원",
        f"전일대비: {change}원 ({ratio}%) {direction}",
    ]
    basis = _price_basis(data)
    # 장중엔 그날 종가가 아직 없다. 그 밖의 시간엔 현재가가 애프터마켓 가격이라 정규장 종가를 따로 준다.
    if basis != "정규장 실시간":
        regular = _regular_close(code)
        if regular:
            lines.append(f"정규장 종가: {regular[0]:,}원 ({regular[1]})")
    lines.append(f"가격 기준: {_with_caveat(basis)} (조회 {fetched_at})")
    return "\n".join(lines)


@mcp.tool()
def stock_detail(code: str) -> str:
    """한국 주식 상세 정보를 조회합니다 (시가/고가/저가/거래량/시총/PER/PBR/배당수익률 등)."""
    data = _fetch(f"{NAVER_STOCK_API}/stock/{code}/integration")
    if not data or "error" in data:
        return f"종목코드 {code} 상세 조회 실패"

    name = data.get("stockName", code)
    infos = {item["key"]: item["value"] for item in data.get("totalInfos", [])}

    lines = [f"{name} ({code}) 상세 정보", ""]
    field_order = [
        "전일", "시가", "고가", "저가", "거래량", "대금",
        "시총", "외인소진율", "52주 최고", "52주 최저",
        "PER", "EPS", "추정PER", "추정EPS", "PBR", "BPS",
        "배당수익률", "주당배당금",
    ]
    for key in field_order:
        if key in infos:
            lines.append(f"{key}: {infos[key]}")

    # 컨센서스는 애널리스트 커버리지가 있는 종목만 제공된다(소형주는 null).
    consensus = data.get("consensusInfo")
    if consensus:
        target = consensus.get("priceTargetMean")
        recomm = consensus.get("recommMean")
        created = consensus.get("createDate", "")
        lines.append("")
        lines.append(f"[컨센서스 {created} 기준]")
        if target:
            lines.append(f"목표주가 평균: {target}원")
        if recomm:
            lines.append(f"투자의견 평균: {recomm}")

    # 이 표의 시세는 기준이 섞여 있다(2026-09-23 삼성전자: 시가 282,500·저가 280,750은 NXT 체결,
    # KRX는 284,500·281,000). 시총·PER·PBR은 조회 시점 가격이라 저녁엔 애프터마켓 가격 기준이다.
    quotes, fetched_at = _fetch_quotes([code])
    if quotes.get(code):
        lines.append("")
        lines.append(f"[시세 기준 — 조회 {fetched_at}]")
        lines.append(f"시총·PER·PBR 기준 가격: {_with_caveat(_price_basis(quotes[code]))}")
        lines.append("시가·고가·저가·거래량·대금: KRX+NXT 통합 / 전일: 정규장 종가")

    return "\n".join(lines)


@mcp.tool()
def stock_search(query: str) -> str:
    """종목명으로 종목코드를 검색합니다. 한글 종목명(예: 삼성전자, 에스티아이)을 입력하세요."""
    encoded = urllib.parse.quote(query)
    data = _fetch(f"{NAVER_SEARCH_API}?q={encoded}&target=stock&st=ac")
    if not data or "error" in data:
        return f"'{query}' 검색 실패"

    items = data.get("items", [])
    if not items:
        return f"'{query}'에 해당하는 종목을 찾지 못했습니다"

    lines = [f"'{query}' 검색 결과:", ""]
    for item in items[:10]:
        code = item.get("code", "")
        name = item.get("name", "")
        market = item.get("typeName", "")
        lines.append(f"  {name} ({code}) [{market}]")

    return "\n".join(lines)


@mcp.tool()
def market_index(market: str = "KOSPI") -> str:
    """KOSPI 또는 KOSDAQ 지수를 조회합니다."""
    market_map = {
        "KOSPI": "KOSPI",
        "KOSDAQ": "KOSDAQ",
        "코스피": "KOSPI",
        "코스닥": "KOSDAQ",
    }
    market_code = market_map.get(market.upper(), market.upper())

    data = _fetch(f"{NAVER_STOCK_API}/index/{market_code}/basic")
    if not data or "error" in data:
        return f"{market} 지수 조회 실패"

    name = data.get("stockName", market_code)
    price = data.get("closePrice", "N/A")
    change = data.get("compareToPreviousClosePrice", "N/A")
    ratio = data.get("fluctuationsRatio", "N/A")
    direction = data.get("compareToPreviousPrice", {}).get("text", "")

    return (
        f"{name}\n"
        f"현재: {price}\n"
        f"전일대비: {change} ({ratio}%) {direction}"
    )


@mcp.tool()
def stock_news(code: str) -> str:
    """특정 종목의 최신 뉴스를 조회합니다. 종목코드를 입력하세요."""
    data = _fetch(f"{NAVER_STOCK_API}/news/stock/{code}?page=1&pageSize=5")
    if not data or (isinstance(data, dict) and "error" in data):
        return f"종목코드 {code} 뉴스 조회 실패"

    all_items = []
    if isinstance(data, list):
        for group in data:
            if isinstance(group, dict):
                all_items.extend(group.get("items", []))
    else:
        all_items = data.get("items", [])

    if not all_items:
        return f"종목 {code} 관련 뉴스가 없습니다"

    lines = [f"종목 {code} 최신 뉴스:", ""]
    for item in all_items[:5]:
        if isinstance(item, dict):
            title = item.get("title", item.get("tit", ""))
            source = item.get("officeName", item.get("office", ""))
            date = _fmt_datetime(item.get("datetime", item.get("dt", "")))
            lines.append(f"  - {title} ({source}, {date})")

    return "\n".join(lines) if len(lines) > 2 else f"종목 {code} 뉴스 파싱 실패"


@mcp.tool()
def stock_investor_trend(code: str, days: int = 10) -> str:
    """종목의 일별 투자자 수급(외국인/기관/개인 순매수 수량)을 조회합니다. days는 조회할 거래일 수(기본 10, 최대 60)."""
    days = max(1, min(days, 60))
    data = _fetch(f"{NAVER_STOCK_API}/stock/{code}/trend?pageSize={days}")
    if not data or (isinstance(data, dict) and "error" in data):
        return f"종목코드 {code} 수급 조회 실패"
    if not isinstance(data, list) or not data:
        return f"종목 {code} 수급 데이터가 없습니다"

    lines = [
        f"종목 {code} 투자자 수급 — 최근 {len(data)}거래일",
        "단위: 주 (+순매수 / -순매도)",
        # 네이버 일별 종가는 그날 마지막 체결가다 — 2026-09-22 삼성전자 277,500원(애프터마켓 종가),
        # 정규장 종가 276,500원.
        "종가: 그날 마지막 체결가 — 애프터마켓(16:00~20:00)에서 거래된 날은 애프터마켓 종가라 정규장 종가와 다를 수 있습니다",
        "",
    ]
    totals = {"foreignerPureBuyQuant": 0, "organPureBuyQuant": 0, "individualPureBuyQuant": 0}
    labels = (
        ("foreignerPureBuyQuant", "외국인"),
        ("organPureBuyQuant", "기관"),
        ("individualPureBuyQuant", "개인"),
    )
    for item in data:
        date = item.get("bizdate", "")
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
        # 한글/숫자 혼합 표는 폭이 어긋나 열을 오독할 수 있어 레이블 방식으로 출력한다.
        parts = [f"{date}  종가 {item.get('closePrice', '-')}"]
        for key, label in labels:
            raw = item.get(key, "-")
            parts.append(f"{label} {raw}")
            value = _to_int(raw)
            if value is not None:
                totals[key] += value
        lines.append(" | ".join(parts))

    lines.append("")
    lines.append(
        f"기간 합계 — 외국인: {totals['foreignerPureBuyQuant']:+,} / "
        f"기관: {totals['organPureBuyQuant']:+,} / "
        f"개인: {totals['individualPureBuyQuant']:+,}"
    )
    hold_ratio = data[0].get("foreignerHoldRatio")
    if hold_ratio:
        lines.append(f"외국인 보유율: {hold_ratio} ({data[0].get('bizdate', '')} 기준)")

    return "\n".join(lines)


# 네이버 응답에는 단위 메타가 없어 지표별 단위를 여기서 명시한다.
# (검증: 삼성전자 2026.03 매출액 1,338,734 = 133.9조 → 억원)
_FINANCE_UNITS = {
    "매출액": "억원", "영업이익": "억원", "당기순이익": "억원",
    "지배주주순이익": "억원", "비지배주주순이익": "억원",
    "영업이익률": "%", "순이익률": "%", "ROE": "%",
    "부채비율": "%", "당좌비율": "%", "유보율": "%",
    "EPS": "원", "BPS": "원", "주당배당금": "원",
    "PER": "배", "PBR": "배",
}


@mcp.tool()
def stock_financials(code: str, period: str = "quarter") -> str:
    """종목의 실적 추이를 조회합니다 (매출액/영업이익/순이익/이익률/ROE/EPS 등). period는 quarter(분기) 또는 annual(연간). 기간 뒤 (E)는 컨센서스 추정치입니다."""
    period_map = {
        "QUARTER": "quarter", "분기": "quarter", "Q": "quarter",
        "ANNUAL": "annual", "연간": "annual", "YEAR": "annual", "A": "annual",
    }
    period_code = period_map.get(period.upper(), period.lower())
    if period_code not in ("quarter", "annual"):
        return f"period는 quarter 또는 annual이어야 합니다 (입력: {period})"

    data = _fetch(f"{NAVER_STOCK_API}/stock/{code}/finance/{period_code}")
    if not data or "error" in data:
        return f"종목코드 {code} 실적 조회 실패"

    finance = data.get("financeInfo") or {}
    titles = finance.get("trTitleList") or []
    rows = finance.get("rowList") or []
    if not titles or not rows:
        return f"종목 {code} 실적 데이터가 없습니다"

    label = "분기" if period_code == "quarter" else "연간"
    headers = [f"{t.get('title', '')}{'(E)' if t.get('isConsensus') == 'Y' else ''}" for t in titles]
    keys = [t.get("key") for t in titles]

    lines = [
        f"종목 {code} 실적 추이 ({label})",
        "(E) = 컨센서스 추정치, '-' = 데이터 없음",
        "",
        "기간: " + " | ".join(headers),
        "",
    ]
    for row in rows:
        title = row.get("title", "")
        columns = row.get("columns") or {}
        # columns는 키 순서가 뒤섞여 있으므로 반드시 trTitleList 순서로 읽는다.
        values = [str((columns.get(k) or {}).get("value", "-")) for k in keys]
        unit = _FINANCE_UNITS.get(title)
        suffix = f" ({unit})" if unit else ""
        lines.append(f"{title}{suffix}: " + " | ".join(values))

    return "\n".join(lines)


# ── 멀티 종목 비교 ────────────────────────────────────────────────
# 종목당 1회씩 부르면 호출 수가 종목 수만큼 늘고 컨텍스트도 그만큼 먹는다.
# 스크리닝은 표 하나로 끝나야 하므로 시세는 한 요청으로, integration/finance는 종목별로 병렬로 받는다.
_COMPARE_MAX = 50

# 정렬 기준 열 → (결과 키, 높은 순인가). 밸류에이션 배수는 낮은 순, 규모·수익성은 높은 순.
# 필터는 두지 않는다 — 행을 숨기면 조회 실패 행을 남기는 이유(누락을 알아채기)가 무너진다.
_SORT_KEYS = {
    "선행PER": ("fwd_per", False), "PER": ("per", False), "PBR": ("pbr", False),
    "시총": ("cap_won", True), "ROE(E)": ("roe", True), "OPM추정": ("opm_est", True), "OPM확정": ("opm_fixed", True),
}


def _num(value):
    """정렬용 숫자. '적자'·'-'처럼 숫자가 아니면 None."""
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _strip_unit(text):
    """'12.29배' / '46.56%' → '12.29' / '46.56'. 값이 없으면 '-'."""
    if not isinstance(text, str):
        return "-"
    cleaned = text.replace("배", "").replace("%", "").replace("원", "").strip()
    return cleaned if cleaned and cleaned != "N/A" else "-"


def _fmt_cap(won):
    """시총(원) → '1,669조', '5.7조', '8,420억'. 값이 없으면 '-'."""
    if not won:
        return "-"
    jo = won / 1e12
    if jo >= 100:
        return f"{jo:,.0f}조"
    if jo >= 1:
        return f"{jo:,.1f}조"
    return f"{won / 1e8:,.0f}억"


def _annual_frame(finance):
    """finance/annual에서 (최근 확정 열, 추정 열, 열 목록, 행 맵)을 뽑는다."""
    info = (finance or {}).get("financeInfo") or {}
    titles = info.get("trTitleList") or []
    if not titles:
        return None, None, [], {}
    rows = {r.get("title"): (r.get("columns") or {}) for r in (info.get("rowList") or [])}
    est = [n for n, t in enumerate(titles) if t.get("isConsensus") == "Y"]
    est_idx = est[0] if est else None
    fixed_idx = (est[0] - 1) if est else len(titles) - 1
    return (fixed_idx if fixed_idx is not None and fixed_idx >= 0 else None), est_idx, titles, rows


def _frame_cell(rows, titles, idx, name):
    if idx is None or name not in rows:
        return "-"
    value = (rows[name].get(titles[idx].get("key")) or {}).get("value")
    return str(value) if value not in (None, "") else "-"


def _per(price, eps):
    """현재가 ÷ EPS. 적자(EPS 음수)면 '적자', 값이 없으면 '-'.

    음수 PER은 "10배 이하" 같은 필터를 숫자로는 통과하고, 크기 순서도 뜻이 없다
    (적자가 클수록 0에 가깝다 — 카카오게임즈 -7.55가 엘앤에프 -80.25보다 적자가 크다).
    """
    price_val, eps_val = _to_int(price), _to_int(eps)
    if eps_val is not None and eps_val < 0:
        return "적자"
    if not price_val or not eps_val:
        return "-"
    return f"{price_val / eps_val:.2f}"


def _compare_one(code, quote):
    """한 종목의 비교용 지표. 시세가 없어도(없는·상장폐지 코드) 행은 남겨 스크리닝에서 누락을 알아채게 한다."""
    if not quote:
        return {"code": code, "failed": True}
    integration = _fetch(f"{NAVER_STOCK_API}/stock/{code}/integration")
    finance = _fetch(f"{NAVER_STOCK_API}/stock/{code}/finance/annual")

    infos = {i["key"]: i["value"] for i in (integration.get("totalInfos") or [])} \
        if isinstance(integration, dict) else {}
    fixed_idx, est_idx, titles, rows = _annual_frame(finance if isinstance(finance, dict) else {})

    # 가격이 들어가는 열(시총·PER·선행PER·PBR)은 모두 표에 찍는 현재가로 계산한다. 종목 상세의
    # PER·PBR·시총도 현재가 기준이지만 요청이 따로라 가격이 어긋난다 — 2026-09-23 19시 애프터마켓
    # 중 시총 상위 140종목 중 24개가 1~2틱(GS는 0.9%) 다른 가격 기준이었고, 삼성전자는 같은 현재가
    # 285,500원에 시총이 1,666조/1,669조로 갈렸다. 가격이 같을 땐 네이버 PER = 현재가 ÷ EPS
    # (99/99), PBR = 현재가 ÷ BPS(128/128)로 정확히 일치했다. 시총은 시세 응답의 값이 그 현재가 ×
    # 상장주식수다. finance 추정 열의 PER은 배치 시점 값(대개 전일 종가 기준)이라 옮겨 쓰면
    # 틀린다(케이씨텍: 12.46, 현재가 기준 13.48). 추정EPS는 integration에 없는 종목이 있어 finance
    # 추정 열로 보완하고 표시한다 — 두 출처가 다 있는 시총 상위 129종목에서 127개 일치, 2개는 1원 차.
    fwd_eps = _strip_unit(infos.get("추정EPS"))
    fallback = False
    if fwd_eps == "-":
        fwd_eps = _frame_cell(rows, titles, est_idx, "EPS")
        fallback = fwd_eps != "-"
    price = quote.get("closePrice", "-")
    fwd_per = _per(price, fwd_eps)
    # 후행 PER도 적자면 '적자'로 쓴다. 네이버는 적자면 PER을 N/A로 줘서 값 없음과 구분이 안 된다.
    per = _per(price, _strip_unit(infos.get("EPS")))
    price_val, bps = _to_int(price), _to_int(_strip_unit(infos.get("BPS")))
    pbr = f"{price_val / bps:.2f}" if price_val and bps and bps > 0 else _strip_unit(infos.get("PBR"))
    cap_won = _to_int(quote.get("marketValueFull"))

    return {
        "code": code,
        "failed": False,
        "name": quote.get("stockName", code),
        "price": price,
        "basis": _price_basis(quote),
        "cap": _fmt_cap(cap_won),
        "cap_won": cap_won,
        "per": per,
        "fwd_per": fwd_per,
        "fwd_eps": fwd_eps,
        # 선행PER이 숫자가 아니면(적자·값 없음) `*`를 달지 않는다.
        "fallback": fallback and fwd_per not in ("-", "적자"),
        "pbr": pbr,
        "opm_fixed": _frame_cell(rows, titles, fixed_idx, "영업이익률"),
        "opm_est": _frame_cell(rows, titles, est_idx, "영업이익률"),
        "roe": _frame_cell(rows, titles, est_idx, "ROE"),
        "fixed_label": titles[fixed_idx].get("title", "") if fixed_idx is not None else "",
        "est_label": titles[est_idx].get("title", "") if est_idx is not None else "",
    }


@mcp.tool()
def stock_compare(codes: str, sort_by: str = "") -> str:
    """여러 종목의 밸류에이션·수익성을 한 표로 비교합니다 (스크리닝용).

    종목마다 stock_detail/stock_financials를 따로 부르는 대신 한 번에 받아옵니다.
    codes: 종목코드를 콤마로 구분 (예: "005930,000660,058470"). 최대 50개.
    sort_by: 정렬 기준 열 (선택). 선행PER·PER·PBR은 낮은 순, 시총·ROE(E)·OPM추정·OPM확정은 높은 순이고
        적자·값 없음은 맨 아래로 갑니다. 비우면 입력 순서.
    반환 항목: 현재가·시총·PER·선행PER·EPS(E)·PBR·영업이익률(최근 확정/추정)·ROE(E).
    시총·PER·선행PER·PBR은 모두 표의 현재가로 계산합니다(PER = 현재가 ÷ EPS, 선행PER = 현재가 ÷ EPS(E)).
    표 위에 현재가 기준(정규장 실시간 / 애프터마켓 / 장 마감)과 조회 시각을 표시합니다.
    PER·선행PER은 적자면 '적자', 값이 없으면 '-'.
    """
    requested = [c.strip() for c in codes.replace("\n", ",").replace(" ", ",").split(",") if c.strip()]
    if not requested:
        return '종목코드를 하나 이상 입력하세요 (예: "005930,000660")'
    sort = sort_by.replace(" ", "").upper()
    sort = "ROE(E)" if sort == "ROE" else sort
    if sort and sort not in _SORT_KEYS:
        return f"sort_by는 {', '.join(_SORT_KEYS)} 중 하나여야 합니다 (입력: {sort_by})"

    targets = requested[:_COMPARE_MAX]
    omitted = requested[_COMPARE_MAX:]

    # 시세는 한 요청으로 받아 모든 행이 같은 시점 가격이 되게 한다.
    quotes, fetched_at = _fetch_quotes(targets)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_compare_one, targets, [quotes.get(c) for c in targets]))

    ok = [r for r in results if not r["failed"]]
    if sort:
        field, descending = _SORT_KEYS[sort]
        numeric = sorted((r for r in ok if _num(r[field]) is not None),
                         key=lambda r: _num(r[field]), reverse=descending)
        results = numeric + [r for r in ok if _num(r[field]) is None] + [r for r in results if r["failed"]]
    lines = [f"종목 비교 — {len(targets)}종목" + (f" (조회 {fetched_at})" if fetched_at else "")]
    # 기준은 대개 모든 행이 같다. 다르면(애프터마켓 중의 ETF 등) 소수 쪽만 종목명을 단다.
    bases = {}
    for r in ok:
        bases.setdefault(r["basis"], []).append(r["name"])
    if bases:
        main, *others = sorted(bases, key=lambda b: -len(bases[b]))
        lines.append(f"현재가 기준: {_with_caveat(main)}" + "".join(
            f". 단 {basis}: {', '.join(bases[basis])}" for basis in others))
    if sort:
        lines.append(f"정렬: {sort} {'높은' if _SORT_KEYS[sort][1] else '낮은'} 순 — 적자·값 없음·조회 실패는 맨 아래")
    lines += [
        "",
        "| 종목 | 현재가 | 시총 | PER | 선행PER | EPS(E) | PBR | OPM확정 | OPM추정 | ROE(E) |",
        "|------|------|------|------|------|------|------|------|------|------|",
    ]
    for r in results:
        if r["failed"]:
            lines.append(f"| ({r['code']}) 조회 실패 | - | - | - | - | - | - | - | - | - |")
            continue
        mark = "*" if r["fallback"] else ""
        lines.append(
            f"| {r['name']} ({r['code']}) | {r['price']} | {r['cap']} | {r['per']} | "
            f"{r['fwd_per']}{mark} | {r['fwd_eps']} | {r['pbr']} | {r['opm_fixed']} | {r['opm_est']} | {r['roe']} |"
        )

    notes = [
        "단위 — 현재가·EPS(E): 원, PER·PBR: 배, OPM·ROE: %. 시총·PER·선행PER·PBR은 모두 표의 "
        "현재가로 계산했습니다(선행PER = 현재가 ÷ EPS(E)). PER·선행PER은 적자면 `적자`, 값이 없으면 `-`입니다."
    ]

    fixed_labels = {r["fixed_label"] for r in ok if r["fixed_label"]}
    est_labels = {r["est_label"] for r in ok if r["est_label"]}
    if len(fixed_labels) == 1 and len(est_labels) == 1:
        notes.append(
            f"`OPM확정`은 {fixed_labels.pop()} 확정치, `OPM추정`·`EPS(E)`·`ROE(E)`는 {est_labels.pop()} "
            "**컨센서스 추정치**입니다. 확정치와 섞어 인용하지 마세요."
        )
    else:
        notes.append(
            "**결산기가 다른 종목이 섞여 있습니다** — `OPM확정`/`OPM추정`의 기준 연도가 종목마다 다르므로, "
            "비교 전에 stock_financials로 각 종목의 기준 연도를 확인하세요. "
            "`OPM추정`·`EPS(E)`·`ROE(E)`는 컨센서스 추정치입니다."
        )

    if any(r["fallback"] for r in ok):
        notes.append(
            "`*` 표시 종목은 종목 상세에 추정EPS가 없어 **실적표 추정 열의 EPS(E)**를 썼습니다. "
            "선행PER은 다른 행과 똑같이 현재가 ÷ EPS(E)입니다."
        )

    notes.append(
        "PER·EPS의 **후행 실적 반영 시점은 종목마다 다릅니다.** 종목 간 PER을 비교하기 전에 "
        "최근 분기 반영 여부를 확인하세요 — 미반영 종목의 낮은 PER은 이익 개선이 아니라 갱신 지연일 수 있습니다."
    )
    if omitted:
        notes.append(
            f"입력한 {len(requested)}종목 중 **앞 {_COMPARE_MAX}개만 조회**했습니다. "
            f"생략된 {len(omitted)}종목: {', '.join(omitted)} — 따로 조회하세요."
        )

    return "\n".join(lines) + "\n\n" + "\n".join(f"> {n}" for n in notes)

# 호스팅 플랫폼의 헬스체크용 엔드포인트. MCP 자체는 /mcp 에서 동작합니다.
@mcp.custom_route("/", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "korean-stock-mcp", "mcp": "/mcp"})


if __name__ == "__main__":
    # Streamable HTTP 트랜스포트로 실행. MCP 엔드포인트: http://<host>:<port>/mcp
    mcp.run(transport="streamable-http")
