#!/usr/bin/env python3
"""Korean Stock Remote MCP Server — 네이버 증권 기반 한국 주식 데이터 원격 MCP 서버

원본(stdio 방식, agent504330-ux/mcp-korean-stock)을 claude.ai 커스텀 커넥터에서
쓸 수 있도록 Streamable HTTP 트랜스포트로 개조한 버전입니다.

- 공개 인터넷에 배포한 뒤 claude.ai 설정 > 커넥터 > 커스텀 커넥터 추가에서
  URL(예: https://<your-host>/mcp)을 등록하면 모바일/데스크톱/웹에 동기화됩니다.
- 데이터 출처: 네이버 증권 모바일 API. 컨센서스는 네이버 증권 종목분석 탭이 쓰는 WiseReport(FnGuide).
  개인 용도로만 사용하세요.
- 인증 없음(authless), 읽기 전용(GET 요청만). 외부로 데이터를 보내지 않습니다.

도구:
  - stock_price          : 종목 현재가
  - stock_detail         : 상세 정보 (시가/고가/저가/거래량/시총/PER/PBR/컨센서스 등)
  - stock_search         : 종목명으로 종목코드 검색
  - market_index         : KOSPI/KOSDAQ 지수
  - stock_news           : 종목 관련 최신 뉴스
  - stock_investor_trend : 일별 투자자 수급 (외국인/기관/개인 순매수)
  - stock_financials     : 실적 추이 (분기/연간, 가장 가까운 1개 기간의 컨센서스 추정 포함)
  - stock_consensus      : 컨센서스 (추정 3개 기간, 추정치 추이, 어닝서프라이즈)
  - stock_compare        : 여러 종목 밸류에이션·수익성 비교 (스크리닝용, 최대 50종목)
"""

import html
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
# 네이버 증권 종목분석 → 컨센서스 탭이 iframe으로 띄우는 WiseReport 페이지(데이터 FnGuide)의 JSON.
WISEREPORT_CONSENSUS_API = "https://navercomp.wisereport.co.kr/v3/company/ajax/c1050001_data.aspx"
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


def _fmt_traded(value):
    """시세의 localTradedAt '2026-09-23T20:00:00+09:00' → '2026-09-23 20:00'. 없으면 ''.

    조회 시각만 적으면 휴장일·주말에 받은 값이 오늘 시세처럼 읽힌다 — 2026-09-24(추석 연휴)
    조회의 현재가·전일대비는 9/23 20:00(애프터마켓 마감) 체결이었다.
    """
    text = str(value or "")
    return f"{text[:10]} {text[11:16]}" if len(text) >= 16 and text[10] == "T" else text


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
    소문자 코드(0126z0)도 빠지므로 대문자로 보낸다 — 응답 키도 대문자다. basic API는 소문자도 받았다.
    """
    joined = urllib.parse.quote(",".join(c.upper() for c in codes), safe=",")
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


_SESSION_END = {}


def _regular_session_end(date):
    """그날 정규장이 끝난 시각 'HHMM'. 평소 15:30, 수능일처럼 장이 한 시간 늦게 열리고 닫히는 날은 16:30.

    평소 15:20~15:30은 종가 단일가 매매라 분봉이 없다(2026-09-17~23, 4종목 모두 15:19 다음 15:30).
    그 사이에 늘 거래되는 삼성전자 분봉이 있으면 연속매매가 이어진 날이다. 15:30으로 고정하면
    수능일(2026-11-19 예정)엔 장중 15:30 가격을 정규장 종가로 적게 된다.
    """
    if date not in _SESSION_END:
        bars = _fetch(f"{NAVER_CHART_API}/005930/minute?startDateTime={date}1521&endDateTime={date}1529")
        if not isinstance(bars, list):
            return "1530"  # 조회 실패는 저장하지 않고 평소 시각으로 본다
        _SESSION_END[date] = "1630" if bars else "1530"
    return _SESSION_END[date]


def _regular_close_on(code, date):
    """그날 정규장 종가(정수). 분봉은 KRX 단독이라 정규장 마감 시각 이하 마지막 봉이 정규장 종가다.

    현재가·일봉 종가는 애프터마켓 종가라 쓸 수 없다. 2026-09-23 삼성전자 285,500원·SK하이닉스
    1,862,000원으로 뉴스의 정규장 종가와 일치했다(현재가는 286,500원·1,863,000원).
    """
    end = _regular_session_end(date)
    bars = _fetch(f"{NAVER_CHART_API}/{code}/minute?startDateTime={date}0900&endDateTime={date}{end}")
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
    data = quotes.get(code.upper())  # 응답 코드는 대문자다(0126z0 → 0126Z0)
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
    traded = _fmt_traded(data.get("localTradedAt"))
    when = f"마지막 체결 {traded} / 조회 {fetched_at}" if traded else f"조회 {fetched_at}"
    lines.append(f"가격 기준: {_with_caveat(basis)} ({when})")
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
    quote = quotes.get(code.upper())
    if quote:
        traded = _fmt_traded(quote.get("localTradedAt"))
        lines.append("")
        lines.append(f"[시세 기준 — 조회 {fetched_at}]")
        lines.append(f"시총·PER·PBR 기준 가격: {_with_caveat(_price_basis(quote))}"
                     + (f" (마지막 체결 {traded})" if traded else ""))
        lines.append("시가·고가·저가·거래량·대금: KRX+NXT 통합 / 전일: 정규장 종가")
        # NXT 거래 종목은 52주 밴드가 KRX 일봉 범위보다 넓다(2026-09-24: 삼성전자 380,000 vs
        # KRX 일봉 최고 374,500, 리노공업·이오테크닉스·SK하이닉스도). NXT 없는 4종목은 같았다.
        lines.append("52주 최고·최저: NXT 거래 종목은 KRX+NXT 통합이라 KRX 단독 범위보다 넓을 수 있음")
        # 외인소진율은 한도 대비 비율이라 한도 있는 종목은 보유율과 다르고(한국전력 51.49% = 보유율
        # 20.60% ÷ 한도 40%), 하루 늦게 갱신된다(9/24 조회 값이 9/22 기준, 수급 표는 9/23까지).
        lines.append("외인소진율: 외국인 한도 대비 비율(한도 없는 종목은 보유율)이고 하루 늦은 값일 수 있음 — "
                     "최신 보유율은 stock_investor_trend")

    return "\n".join(lines)


@mcp.tool()
def stock_search(query: str) -> str:
    """종목명으로 종목코드를 검색합니다. 한글 종목명(예: 삼성전자, 에스티아이)을 입력하세요."""
    encoded = urllib.parse.quote(query)
    data = _fetch(f"{NAVER_SEARCH_API}?q={encoded}&target=stock&st=ac")
    if not data or "error" in data:
        return f"'{query}' 검색 실패"

    # 검색은 해외 종목(애플 AAPL 등)도 돌려주지만 이 서버의 다른 도구는 국내 시세만 조회한다.
    everything = data.get("items", [])
    items = [i for i in everything if i.get("nationCode", "KOR") == "KOR"]
    foreign = len(everything) - len(items)
    if not items:
        extra = f" (해외 종목 {foreign}개는 제외 — 이 서버는 국내 종목만 조회합니다)" if foreign else ""
        return f"'{query}'에 해당하는 국내 종목을 찾지 못했습니다{extra}"

    lines = [f"'{query}' 검색 결과:", ""]
    for item in items[:10]:
        code = item.get("code", "")
        name = item.get("name", "")
        market = item.get("typeName", "")
        lines.append(f"  {name} ({code}) [{market}]")
    if foreign:
        lines.append("")
        lines.append(f"(해외 종목 {foreign}개는 제외했습니다 — 이 서버는 국내 종목만 조회합니다)")

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
    # 날짜가 없으면 휴장일·주말에 받은 지수가 오늘 값처럼 읽힌다(2026-09-24 추석 연휴: 9/23 값).
    traded = _fmt_traded(data.get("localTradedAt"))
    status = {"OPEN": "장중", "CLOSE": "장 마감"}.get(data.get("marketStatus"), data.get("marketStatus") or "")

    return (
        f"{name}\n"
        f"현재: {price}\n"
        f"전일대비: {change} ({ratio}%) {direction}"
        + (f"\n기준: {traded}" + (f" ({status})" if status else "") if traded else "")
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
            # 제목은 HTML 엔티티째 온다(&quot;박스피에 지쳤다&quot;). 긴 제목의 '...' 절단은 네이버가
            # 한 것이라(titleFull도 같은 값) 복원할 수 없다.
            title = html.unescape(item.get("title", item.get("tit", "")))
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


_PERIOD_MAP = {
    "QUARTER": "quarter", "분기": "quarter", "Q": "quarter",
    "ANNUAL": "annual", "연간": "annual", "YEAR": "annual", "A": "annual",
}


@mcp.tool()
def stock_financials(code: str, period: str = "quarter") -> str:
    """종목의 실적 추이를 조회합니다 (매출액/영업이익/순이익/이익률/ROE/EPS 등). period는 quarter(분기) 또는 annual(연간). 기간 뒤 (E)는 컨센서스 추정치인데 가장 가까운 1개 기간뿐입니다 — 그 뒤 연도·분기의 추정과 추정치 변화는 stock_consensus로 보세요."""
    period_code = _PERIOD_MAP.get(period.upper(), period.lower())
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
        # 모바일 API는 추정 열을 1개만 준다(2026-09-25, 15종목 모두 연간·분기 각 1개).
        "(E) = 컨센서스 추정치 — 가장 가까운 1개 기간만 있음, 그 뒤 추정은 stock_consensus / '-' = 데이터 없음",
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


# ── 컨센서스 ─────────────────────────────────────────────────────
# 모바일 API(finance/annual·quarter)는 추정 열을 가장 가까운 1개 기간만 준다. 네이버 증권 종목분석 →
# 컨센서스 탭은 WiseReport 페이지를 iframe으로 띄우는데, 그 페이지의 JSON엔 추정 3개 기간이 있다.
# 같은 컨센서스다 — 2026-09-25 삼성전자 2026E 매출액 7,370,706억·영업이익 3,876,962억·EPS 47,922원이
# 모바일 API와 일치. 토큰·Referer 없이 받아진다. flag 2 = 실적·추정 표, 1 = 추이를 볼 수 있는 기간,
# 4 = 추이, 5 = 어닝서프라이즈. 우선주·ETF·상장폐지·없는 코드는 빈 목록이다(우선주에 보통주 값을 주는
# 모바일 API와 다르다).
_CNS_COLUMNS = [
    ("SALES", "매출액"), ("YOY", "YoY(%)"), ("OP", "영업이익"), ("NP", "순이익"), ("EPS", "EPS"),
    ("BPS", "BPS"), ("PER", "PER"), ("PBR", "PBR"), ("ROE", "ROE(%)"), ("EV", "EV/EBITDA"),
]
# 어닝서프라이즈 계정. 순이익은 위 표의 순이익과 같은 기준이다(삼성전자 2025 442,610억 = 지배주주).
_CNS_SURPRISE = [("121000", "매출액"), ("121500", "영업이익"), ("122700", "순이익")]
_CNS_SURPRISE_KEYS = ("FY_2", "FY_1", "FY0")  # 실적이 나온 최근 3개 결산기, 오래된 순
_CNS_TREND_COLUMNS = ("1주전", "1개월전", "3개월전", "1년전")


def _consensus_url(code, flag, frq, **params):
    query = urllib.parse.urlencode({"flag": flag, "cmp_cd": code, "finGubun": "MAIN", "frq": frq, **params})
    return f"{WISEREPORT_CONSENSUS_API}?{query}"


def _cns_number(value, name):
    """추이 표 숫자 → 컨센서스 탭과 같은 자릿수(억원 소수 한 자리, 원 정수, 배·%·점수 소수 둘째 자리).

    값이 없으면 '-'. 1년 전 추정이 없던 기간은 null이 아니라 0.0으로 온다(삼성전자 2027.06 매출액).
    """
    if not isinstance(value, (int, float)) or value == 0:
        return "-"
    digits = 1 if "(억원)" in name else 0 if "(원)" in name else 2
    return f"{value:,.{digits}f}"


def _fmt_yyyymmdd(text):
    text = str(text or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def _why(data):
    """조회 실패 이유 — 'timed out'(응답 없음, 잠시 뒤 다시)과 'HTTP Error 404'(주소 변경)를 가려 읽게 한다.

    WiseReport는 GitHub Actions 러너 IP에서 가끔 응답을 멈춘다(2026-09-25 CI 5회 중 2회, 타임아웃만).
    """
    return str(data.get("error"))[:80] if isinstance(data, dict) and "error" in data else "응답 형식이 다름"


def _negative(value):
    """표의 문자열('-14.34', '-4,199')이든 추이의 숫자(-14.337)든 음수인가."""
    number = value if isinstance(value, (int, float)) else _num(value or "-")
    return number is not None and number < 0


def _loss_per(cell):
    """적자 기간의 PER 칸 → '적자(-14.34)'. 원래 값이 없으면 '적자'."""
    return f"적자({cell})" if cell and cell != "-" else "적자"


def _yoy_by_year(rows):
    """연간 표 행 간격이 12개월이 아니면(6개월 결산 리츠 등) YoY를 1년 전 같은 결산기 대비로 다시 계산한다.

    원본 YoY엔 직전 결산기 대비가 섞여 있다(2026-09-25 6개월 결산 리츠 5종목 모두 — 한화리츠 2025.10은
    1년 전 대비 56.41%, 2026.04는 직전 결산기 대비 3.50%이고 1년 전 대비는 7.53%). 12개월 간격이면
    직전 = 1년 전이라 원본이 맞는다. → (행별 YoY 문자열, 화면과 다른 칸) 또는 간격이 12개월이면 None.
    """
    try:
        months = [int(r["YYMM"][:4]) * 12 + int(r["YYMM"][5:7]) for r in rows]
    except (KeyError, TypeError, ValueError):
        return None
    if all(b - a == 12 for a, b in zip(months, months[1:])):
        return None
    sales = {m: _num(r.get("SALES") or "-") for m, r in zip(months, rows)}
    values, differ = [], []
    for m, r in zip(months, rows):
        now, before, shown = sales[m], sales.get(m - 12), _num(r.get("YOY") or "-")
        if now is None or not before:
            values.append("-")  # 1년 전 결산기가 표에 없다
            continue
        ratio = (now / before - 1) * 100
        tolerance = (0.05 / before + now * 0.05 / before ** 2) * 100 + 0.006  # 표의 매출액이 소수 한 자리
        if shown is not None and abs(shown - ratio) <= tolerance:
            values.append(r["YOY"])
        else:
            values.append(f"{ratio:.2f}✎")
            if shown is not None:
                differ.append(f"{r.get('YYMM', '')} {shown:.2f}%")
    return values, differ


@mcp.tool()
def stock_consensus(code: str, period: str = "annual") -> str:
    """종목의 애널리스트 컨센서스(FnGuide)를 조회합니다 — 네이버 증권 종목분석 → 컨센서스 탭과 같은 데이터.
    period는 annual(연간, 기본) 또는 quarter(분기).
    ① 실적·추정 표: 최근 실적 4개 기간 + 추정 3개 기간(연간이면 예: 2026·2027·2028년) — 매출액·YoY·
       영업이익·순이익·EPS·BPS·PER·PBR·ROE·EV/EBITDA
    ② 컨센서스 추이: 추정 기간마다 기준일·1주·1개월·3개월·1년 전의 추정치(상향·하향 확인)
    ③ 어닝서프라이즈: 최근 3개 결산기의 실적과 발표 전 추정치(매출액·영업이익·순이익)
    stock_financials의 추정은 가장 가까운 1개 기간뿐이라 그 뒤 추정은 이 도구로 보세요.
    목표주가·투자의견 평균은 stock_detail에 있습니다."""
    period_code = _PERIOD_MAP.get(period.upper(), period.lower())
    if period_code not in ("quarter", "annual"):
        return f"period는 annual 또는 quarter여야 합니다 (입력: {period})"
    frq = 0 if period_code == "annual" else 1

    table = _fetch(_consensus_url(code, 2, frq))
    if not isinstance(table, dict) or "error" in table:
        return f"종목코드 {code} 컨센서스 조회 실패 ({_why(table)})"
    rows = table.get("JsonData") or []
    if not rows:
        return (f"종목 {code} 컨센서스 데이터가 없습니다 — 우선주·ETF·상장폐지·없는 코드는 제공되지 않습니다"
                " (우선주는 보통주 코드로 조회하세요)")

    # 추이는 실적이 나온 기간 뒤만 본다. 기간 목록엔 표보다 먼 추정이 있을 때가 있다(삼성전자 분기 표는
    # 2027.03까지, 추이는 2027.06까지).
    last_actual = max((r.get("YYMM", "")[:7].replace(".", "") for r in rows if "(A)" in r.get("YYMM", "")),
                      default="")
    listed = _fetch(_consensus_url(code, 1, frq))
    listed_ok = isinstance(listed, dict) and "error" not in listed
    periods = [p["YYMM"] for p in (listed.get("JsonData") or []) if isinstance(p, dict)
               and p.get("YYMM") and p["YYMM"] > last_actual] if listed_ok else []
    with ThreadPoolExecutor(max_workers=8) as pool:
        trend_jobs = [pool.submit(_fetch, _consensus_url(code, 4, frq, yymm=p)) for p in periods]
        surprise_jobs = [pool.submit(_fetch, _consensus_url(code, 5, frq, acc_cd=acc)) for acc, _ in _CNS_SURPRISE]
    trends = [job.result() for job in trend_jobs]
    surprises = [job.result() for job in surprise_jobs]

    label = "연간" if frq == 0 else "분기"
    dates = [t["JsonData"][0].get("DT") for t in trends if isinstance(t, dict) and t.get("JsonData")]
    lines = [
        f"종목 {code} 컨센서스 ({label}) — FnGuide" + (f", 기준일 {_fmt_yyyymmdd(dates[0])}" if dates else ""),
        "출처: 네이버 증권 종목분석 → 컨센서스 (WiseReport). (A) = 실적, (E) = 컨센서스 추정치, '-' = 값 없음",
    ]
    bases = sorted({r.get("MAIN") for r in rows if r.get("MAIN")})
    if len(bases) == 1:
        lines.append(f"재무 기준: {bases[0]}")

    lines += ["", "[실적·추정]", "| 기간 | " + " | ".join(n for _, n in _CNS_COLUMNS) + " |",
              "|" + "------|" * (len(_CNS_COLUMNS) + 1)]
    cycle = _yoy_by_year(rows) if frq == 0 else None
    per_at = [k for k, _ in _CNS_COLUMNS].index("PER")
    losses = False
    for index, r in enumerate(rows):
        # 재무 기준이 기간마다 다르면(별도 → 연결 전환 등) 기간 옆에 적는다.
        basis = f" {r['MAIN']}" if len(bases) > 1 and r.get("MAIN") else ""
        cells = [cycle[0][index] if cycle and k == "YOY" else str(r.get(k) or "-") for k, _ in _CNS_COLUMNS]
        # 적자면 PER을 '적자(원래 값)'으로 쓴다 — 사용자 결정(2026-09-25). 음수 PER은 배수로서 뜻이 없고 크기가
        # 적자 규모에 반비례해 거꾸로 읽힌다(롯데케미칼 2027E: EPS −1,366 → −5,638로 적자가 4배 커지는 동안
        # PER은 −46.04 → −10.68로 0에 가까워졌다). 괄호의 원래 값은 네이버 화면 대조용이다.
        if _negative(r.get("PER")) or _negative(r.get("EPS")):
            cells[per_at], losses = _loss_per(cells[per_at]), True
        lines.append(f"| {r.get('YYMM', '')}{basis} | " + " | ".join(cells) + " |")
    estimates = [r for r in rows if "(E)" in r.get("YYMM", "")]
    if not any(r.get(k) for r in estimates for k, _ in _CNS_COLUMNS):
        lines += ["", "현재 컨센서스 추정치가 없습니다."]

    trend_lines = []
    for p, t in zip(periods, trends):
        name = f"{p[:4]}.{p[4:]}(E)"
        items = t.get("JsonData") if isinstance(t, dict) and "error" not in t else None
        if items is None:
            trend_lines += ["", f"{name} 조회 실패 ({_why(t)})"]
            continue
        if all(_cns_number(i.get("VAL1"), "") == "-" for i in items):
            continue  # 기준일 추정치가 없는 기간
        trend_lines += ["", f"{name} — 기준일 {_fmt_yyyymmdd(items[0].get('DT'))}",
                        f"| 항목 | {_fmt_yyyymmdd(items[0].get('DT'))} | " + " | ".join(_CNS_TREND_COLUMNS) + " |",
                        "|" + "------|" * (len(_CNS_TREND_COLUMNS) + 2)]
        eps = next((i for i in items if str(i.get("ACC_NM", "")).startswith("EPS")), {})
        for i in items:
            acc = i.get("ACC_NM", "")
            cells = [_cns_number(i.get(f"VAL{n}"), acc) for n in range(1, 6)]
            if acc.startswith("PER"):  # 표와 같이 그 시점 EPS가 음수면 '적자(원래 값)'
                for n in range(1, 6):
                    if _negative(i.get(f"VAL{n}")) or _negative(eps.get(f"VAL{n}")):
                        cells[n - 1], losses = _loss_per(cells[n - 1]), True
            if any(c != "-" for c in cells):  # 분기 추이의 ROE처럼 전부 빈 항목은 뺀다
                trend_lines.append(f"| {acc} | " + " | ".join(cells) + " |")
    if not listed_ok:
        trend_lines += ["", f"추이 기간 목록 조회 실패 ({_why(listed)})"]
    if trend_lines:
        lines += ["", "[컨센서스 추이] 기준일 추정치와 그 전 시점의 추정치 — 상향·하향 확인용"] + trend_lines

    parsed, failed = [], []
    for (_, account), s in zip(_CNS_SURPRISE, surprises):
        data = s.get("tableData") if isinstance(s, dict) and "error" not in s else None
        if not isinstance(data, dict):
            failed.append(f"{account} ({_why(s)})")
            continue
        found = data.get("tableData") or []
        parsed.append((account, (data.get("tableHeaderData") or [{}])[0],
                       next((x for x in found if "(A)" in str(x.get("QTR"))), {}),
                       {x.get("QTR"): x for x in found if "(E)" in str(x.get("QTR"))}))
    # 발표직전·3개월전… (분기는 발표직전·1개월전…) — 응답 순서대로 열을 만들고 계정마다 같은 이름으로 채운다.
    heads = next((list(e) for *_, e in parsed if e), [])
    # 서프라이즈 %는 표의 실적·추정으로 직접 계산한다. FnGuide가 주는 %는 세 가지로 어긋난다(2026-09-25,
    # 45종목 2,951칸): ① 추정이 음수인 칸은 발표직전 외 열이 실적 ÷ 추정 − 1이라 부호가 반대다(150칸 —
    # LG에너지솔루션 2025/12 분기 영업이익 적자 −214 → −1,220이 +469%) ② 같은 칸의 발표직전 열은
    # (실적 − 추정) ÷ |추정|이다(56칸) ③ 발표직전 %가 표의 추정과 안 맞는다(9칸 — 삼성바이오로직스 2025
    # 매출액 실적 45,570 > 추정 44,363인데 −9.44%). 화면과 다른 칸은 ✎로 표시하고 ③은 화면 값을 남긴다.
    surprise_lines, differ, flipped = [], [], False
    for key in _CNS_SURPRISE_KEYS:
        for account, header, actual, est in parsed:
            if not any(e.get(key) for e in est.values()):
                continue  # 발표 전 추정이 없던 결산기는 비교할 게 없다
            period_name = header.get(f"CNS_{key}", key)
            date = actual.get(f"{key}_S")
            real = _num(actual.get(key) or "-")
            cells = [(actual.get(key) or "-") + (f" ({date})" if actual.get(key) and date else "")]
            for head in heads:
                e = est.get(head) or {}
                value, shown = e.get(key), e.get(f"{key}_S")
                guess = _num(value or "-")
                if real is None or not guess:
                    cells.append(value or "-")
                    continue
                ratio = (real - guess) / abs(guess) * 100
                # 표의 값이 소수 한 자리(억원)라 생기는 오차 + %의 반올림.
                tolerance = (0.05 / abs(guess) + abs(real) * 0.05 / guess ** 2) * 100 + 0.006
                mark = ""
                if isinstance(shown, (int, float)) and abs(shown - ratio) > tolerance:
                    mark = "✎"
                    if abs(shown + ratio) <= tolerance:
                        flipped = True
                    else:  # 부호만 반대인 게 아니면 화면 값을 적어 둔다
                        differ.append(f"{period_name} {account} {head} {shown:+.2f}%")
                cells.append(f"{value} ({ratio:+.2f}%{mark})")
            surprise_lines.append(f"| {period_name} | {account} | " + " | ".join(cells) + " |")
    if surprise_lines:
        lines += ["", "[어닝서프라이즈] 실적과 발표 전 추정치 — 괄호는 서프라이즈(%) = (실적 − 추정) ÷ |추정|",
                  "| 결산기 | 항목 | 실적 (발표일) | " + " | ".join(heads) + " |",
                  "|" + "------|" * (len(heads) + 3)] + surprise_lines
    if failed:
        lines += ["", f"어닝서프라이즈 조회 실패: {', '.join(failed)}"]

    notes = [
        "단위 — 매출액·영업이익·순이익: 억원, EPS·BPS: 원, PER·PBR·EV/EBITDA: 배, YoY·ROE: %. "
        "IFRS 연결 회사의 순이익·BPS·ROE는 지배주주 기준입니다.",
    ]
    if losses:
        notes.append("PER은 EPS가 음수(적자)면 `적자`로 적고 괄호에 원래 값(네이버 화면의 음수 PER)을 남깁니다. "
                     "괄호 숫자는 화면 대조용일 뿐입니다 — 음수 PER의 크기는 적자 규모에 반비례하고 주가 변동도 섞여서, "
                     "그 크기나 변화로 적자폭을 판단하면 거꾸로 읽힙니다. 적자폭은 EPS·순이익으로 보세요.")
    # (E)의 PER은 기준일 정규장 종가 ÷ EPS(E)다 — 2026-09-23 삼성전자 285,500 ÷ 47,922 = 5.96(애프터마켓
    # 종가 286,500이면 5.98). (A)는 결산기 말 종가다 — 2024년 53,200 ÷ 4,950 = 10.75, 2023년 78,500 ÷ 2,131 = 36.84.
    if frq == 0:
        notes.append("PER·PBR — (A)는 그 결산기 말 주가, (E)는 기준일 정규장 종가 기준입니다. "
                     "현재가 기준 선행PER은 stock_compare를 쓰세요.")
    else:
        # 분기 값은 분기 하나로 계산한다 — 2026.09(E) PER 20.97 = 285,500 ÷ 분기 EPS 13,616,
        # 2026.06(A) ROE 13.72% = 분기 순이익 712,695억 ÷ 평균 지배주주 자본 5,195,148억.
        notes.append("분기의 PER·ROE·EV/EBITDA는 그 분기 실적 하나로 계산한 값이라(연환산 아님) 연간 값과 "
                     "비교하면 안 됩니다. (E)의 PER은 기준일 정규장 종가 기준입니다.")
    if cycle:
        # 6개월 결산이면 PER = 결산기 말 종가 ÷ 6개월 EPS다 — 한화리츠 2026.04 5,970 ÷ 97 = 61.55(표 61.59).
        notes.append("결산 주기가 1년이 아닙니다(6개월 결산 리츠 등) — 표의 한 행이 한 결산기라 PER·ROE는 그 결산기 "
                     "실적 기준(연환산 아님)입니다. YoY는 1년 전 같은 결산기 대비로 다시 계산했습니다: 네이버 화면의 "
                     "YoY엔 직전 결산기 대비가 섞여 있어 다른 칸은 ✎"
                     + (f"(화면 값: {', '.join(cycle[1])})" if cycle[1] else "")
                     + ", 1년 전 결산기가 표에 없는 행은 비웠습니다.")
    # 삼성전자 2025 영업이익: 서프라이즈 실적 435,300억(2026/01/08 잠정 발표) vs 위 표 436,010.5억(확정).
    notes.append("어닝서프라이즈의 실적은 발표일 당시 값이라(잠정실적을 내는 회사는 잠정치) 위 표의 확정치와 "
                 "다를 수 있습니다.")
    if flipped or differ:
        notes.append("✎ = 네이버 화면과 서프라이즈 %가 다른 칸."
                     + (" 추정이 음수(적자)인 칸의 발표직전 외 열은 화면이 실적 ÷ 추정 − 1로 계산해 부호가 반대입니다."
                        if flipped else "")
                     + (f" 발표직전 %가 표의 추정과 맞지 않는 칸의 화면 값: {', '.join(differ)}" if differ else ""))
    notes.append("목표주가·투자의견 평균은 stock_detail에 있습니다.")
    return "\n".join(lines) + "\n\n" + "\n".join(f"> {n}" for n in notes)


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
    """현재가 ÷ EPS. 적자(EPS 음수)면 '적자(-14.48)'처럼 계산값을 괄호에 남기고, 값이 없으면 '-'.

    음수 PER은 "10배 이하" 같은 필터를 숫자로는 통과하고, 크기 순서도 뜻이 없다
    (적자가 클수록 0에 가깝다 — 카카오게임즈 -7.55가 엘앤에프 -80.25보다 적자가 크다).
    괄호 값은 stock_consensus와 같은 표시다(사용자 결정 2026-09-25) — 정렬에선 숫자가 아니라 맨 아래로 간다.
    """
    price_val, eps_val = _to_int(price), _to_int(eps)
    if eps_val is not None and eps_val < 0:
        return f"적자({price_val / eps_val:.2f})" if price_val else "적자"
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
    name = quote.get("stockName", code)

    return {
        "code": code,
        "failed": False,
        "name": name,
        # 우선주는 네이버가 EPS·추정EPS·BPS를 보통주 값으로 준다(2026-09-23 삼성전자우·현대차2우B 등
        # 10쌍 모두 일치). 응답에 우선주 표시가 없어 KRX 코드 규칙(보통주는 끝자리 0)과 이름의 '우'로
        # 가린다 — 이름만 보면 우리금융지주·대우건설 같은 보통주가 걸린다.
        "preferred": not code.endswith("0") and "우" in name,
        "price": price,
        "basis": _price_basis(quote),
        "traded": _fmt_traded(quote.get("localTradedAt")),
        "cap": _fmt_cap(cap_won),
        "cap_won": cap_won,
        "per": per,
        "fwd_per": fwd_per,
        "fwd_eps": fwd_eps,
        # 선행PER이 숫자가 아니면(적자·값 없음) `*`를 달지 않는다.
        "fallback": fallback and fwd_per != "-" and not fwd_per.startswith("적자"),
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
    codes: 종목코드를 콤마로 구분 (예: "005930,000660,058470"). 최대 50개, 같은 코드는 한 번만 조회합니다.
    sort_by: 정렬 기준 열 (선택). 선행PER·PER·PBR은 낮은 순, 시총·ROE(E)·OPM추정·OPM확정은 높은 순이고
        적자·값 없음은 맨 아래로 갑니다. 비우면 입력 순서.
    반환 항목: 현재가·시총·PER·선행PER·EPS(E)·PBR·영업이익률(최근 확정/추정)·ROE(E).
    시총·PER·선행PER·PBR은 모두 표의 현재가로 계산합니다(PER = 현재가 ÷ EPS, 선행PER = 현재가 ÷ EPS(E)).
    표 위에 현재가 기준(정규장 실시간 / 애프터마켓 / 장 마감)과 조회 시각을 표시합니다.
    PER·선행PER은 적자면 '적자(계산값)', 값이 없으면 '-'. 우선주(†)는 EPS·BPS가 보통주 값이라 배수가 낮게 나옵니다.
    """
    # 응답 코드가 대문자라 입력도 대문자로 맞춘다. 같은 코드는 한 번만 조회한다(두 줄로 나오고 한도만 먹었다).
    entered = [c.strip().upper() for c in codes.replace("\n", ",").replace(" ", ",").split(",") if c.strip()]
    requested = list(dict.fromkeys(entered))
    duplicates = [c for c in requested if entered.count(c) > 1]
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
    # 조회 시각만으로는 휴장일에 받은 표가 오늘 시세처럼 읽힌다. 체결일을 적고, 다른 날에 마지막으로
    # 체결된 종목(거래정지 등)은 따로 단다.
    dates = {}
    for r in ok:
        if r["traded"]:
            dates.setdefault(r["traded"][:10], []).append(r["name"])
    if dates:
        main_date = max(dates, key=lambda d: (len(dates[d]), d))
        lines.append(f"마지막 체결일: {main_date}" + "".join(
            f". 단 {d}: {', '.join(dates[d])}" for d in sorted(dates) if d != main_date))
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
        pref = "†" if r["preferred"] else ""
        lines.append(
            f"| {r['name']}{pref} ({r['code']}) | {r['price']} | {r['cap']} | {r['per']} | "
            f"{r['fwd_per']}{mark} | {r['fwd_eps']} | {r['pbr']} | {r['opm_fixed']} | {r['opm_est']} | {r['roe']} |"
        )

    notes = [
        "단위 — 현재가·EPS(E): 원, PER·PBR: 배, OPM·ROE: %. 시총·PER·선행PER·PBR은 모두 표의 "
        "현재가로 계산했습니다(선행PER = 현재가 ÷ EPS(E)). PER·선행PER은 적자면 `적자`, 값이 없으면 `-`입니다."
    ]
    if any(str(r.get(key, "")).startswith("적자(") for r in ok for key in ("per", "fwd_per")):
        notes.append("`적자(…)`의 괄호 값은 현재가 ÷ EPS 계산값을 남긴 것뿐입니다 — 음수 PER은 크기가 적자 규모에 "
                     "반비례하고 주가 변동도 섞여서, 그 크기나 변화로 적자폭을 판단하면 거꾸로 읽힙니다. "
                     "적자폭은 `EPS(E)`로 보세요. 정렬에선 적자 행이 맨 아래로 갑니다.")

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

    if any(r["preferred"] for r in ok):
        notes.append(
            "`†` 표시는 **우선주**입니다. EPS·EPS(E)·BPS가 보통주 값이라(네이버도 같은 방식) PER·선행PER·PBR에 "
            "우선주 할인이 들어가 낮게 나옵니다. 보통주나 다른 종목과 배수를 그대로 비교하지 마세요."
        )

    notes.append(
        "PER·EPS의 **후행 실적 반영 시점은 종목마다 다릅니다.** 종목 간 PER을 비교하기 전에 "
        "최근 분기 반영 여부를 확인하세요 — 미반영 종목의 낮은 PER은 이익 개선이 아니라 갱신 지연일 수 있습니다."
    )
    if duplicates:
        notes.append(f"두 번 이상 넣은 {len(duplicates)}종목({', '.join(duplicates)})은 한 번만 조회했습니다.")
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
