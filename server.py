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
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

NAVER_STOCK_API = "https://m.stock.naver.com/api"
NAVER_SEARCH_API = "https://ac.stock.naver.com/ac"
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


@mcp.tool()
def stock_price(code: str) -> str:
    """한국 주식 현재가를 조회합니다. 종목코드(예: 005930=삼성전자, 039440=에스티아이)를 입력하세요."""
    data = _fetch(f"{NAVER_STOCK_API}/stock/{code}/basic")
    if not data or "error" in data:
        return f"종목코드 {code} 조회 실패"

    name = data.get("stockName", code)
    price = data.get("closePrice", "N/A")
    change = data.get("compareToPreviousClosePrice", "N/A")
    ratio = data.get("fluctuationsRatio", "N/A")
    status = data.get("marketStatus", "")
    direction = data.get("compareToPreviousPrice", {}).get("text", "")

    return (
        f"{name} ({code})\n"
        f"현재가: {price}원\n"
        f"전일대비: {change}원 ({ratio}%) {direction}\n"
        f"시장상태: {status}"
    )


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
# 스크리닝은 표 하나로 끝나야 하므로 basic/integration/finance를 묶어 병렬로 받는다.
_COMPARE_MAX = 50


def _strip_unit(text):
    """'12.29배' / '46.56%' → '12.29' / '46.56'. 값이 없으면 '-'."""
    if not isinstance(text, str):
        return "-"
    cleaned = text.replace("배", "").replace("%", "").replace("원", "").strip()
    return cleaned if cleaned and cleaned != "N/A" else "-"


def _short_cap(text):
    """'1,601조 8,803억' → '1,601조', '5조 6,930억' → '5.7조'."""
    if not isinstance(text, str):
        return "-"
    jo = re.search(r"([\d,]+)\s*조", text)
    eok = re.search(r"([\d,]+)\s*억", text)
    jo_val = float(jo.group(1).replace(",", "")) if jo else 0.0
    eok_val = float(eok.group(1).replace(",", "")) if eok else 0.0
    if jo_val:
        total = jo_val + eok_val / 10000
        return f"{total:,.1f}조" if total < 100 else f"{total:,.0f}조"
    if eok_val:
        return f"{eok_val:,.0f}억"
    return text.strip() or "-"


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


def _fwd_per(price, eps):
    """현재가 ÷ 추정EPS. 숫자가 아니거나 적자 추정(EPS ≤ 0)이면 '-'.

    음수 PER은 "10배 이하" 같은 필터를 숫자로는 통과해 버린다. 후행 PER도 적자면
    네이버가 N/A를 주므로 같은 처리다. 적자 추정인지는 EPS(E) 열의 부호로 보인다.
    """
    price_val, eps_val = _to_int(price), _to_int(eps)
    if not price_val or eps_val is None or eps_val <= 0:
        return "-"
    return f"{price_val / eps_val:.2f}"


def _compare_one(code):
    """한 종목의 비교용 지표. 실패해도 행은 남겨 스크리닝에서 누락을 알아채게 한다."""
    basic = _fetch(f"{NAVER_STOCK_API}/stock/{code}/basic")
    integration = _fetch(f"{NAVER_STOCK_API}/stock/{code}/integration")
    finance = _fetch(f"{NAVER_STOCK_API}/stock/{code}/finance/annual")

    if not isinstance(basic, dict) or "error" in basic or not basic.get("stockName"):
        return {"code": code, "failed": True}

    infos = {i["key"]: i["value"] for i in (integration.get("totalInfos") or [])} \
        if isinstance(integration, dict) else {}
    fixed_idx, est_idx, titles, rows = _annual_frame(finance if isinstance(finance, dict) else {})

    # 선행PER은 표에 찍는 현재가 ÷ 추정EPS로 직접 계산한다. finance 추정 열의 PER은
    # 현재가가 아닌 배치 시점 값(대개 전일 종가 기준)이라 옮겨 쓰면 틀린다(2026-09-23
    # 케이씨텍: 12.46, 현재가 기준 13.48). integration 추정PER은 현재가 기준이지만 basic과
    # 따로 받아 호가가 어긋날 수 있다. 추정EPS는 integration에 없는 종목이 있어 finance
    # 추정 열로 보완하고 표시한다 — 두 출처가 다 있는 시총 상위 129종목에서 127개 일치, 2개는 1원 차.
    fwd_eps = _strip_unit(infos.get("추정EPS"))
    fallback = False
    if fwd_eps == "-":
        fwd_eps = _frame_cell(rows, titles, est_idx, "EPS")
        fallback = fwd_eps != "-"
    price = basic.get("closePrice", "-")
    fwd_per = _fwd_per(price, fwd_eps)

    return {
        "code": code,
        "failed": False,
        "name": basic.get("stockName", code),
        "price": price,
        "status": basic.get("marketStatus", ""),
        "cap": _short_cap(infos.get("시총")),
        "per": _strip_unit(infos.get("PER")),
        "fwd_per": fwd_per,
        "fwd_eps": fwd_eps,
        # 적자 추정으로 선행PER이 '-'면 `*`를 달지 않는다("-*"는 뜻이 모호하다).
        "fallback": fallback and fwd_per != "-",
        "pbr": _strip_unit(infos.get("PBR")),
        "opm_fixed": _frame_cell(rows, titles, fixed_idx, "영업이익률"),
        "opm_est": _frame_cell(rows, titles, est_idx, "영업이익률"),
        "roe": _frame_cell(rows, titles, est_idx, "ROE"),
        "fixed_label": titles[fixed_idx].get("title", "") if fixed_idx is not None else "",
        "est_label": titles[est_idx].get("title", "") if est_idx is not None else "",
    }


@mcp.tool()
def stock_compare(codes: str) -> str:
    """여러 종목의 밸류에이션·수익성을 한 표로 비교합니다 (스크리닝용).

    종목마다 stock_detail/stock_financials를 따로 부르는 대신 한 번에 받아옵니다.
    codes: 종목코드를 콤마로 구분 (예: "005930,000660,058470"). 최대 50개.
    반환 항목: 현재가·시총·PER·선행PER(현재가 ÷ EPS(E), 적자 추정이면 '-')·EPS(E)·PBR·영업이익률(최근 확정/추정)·ROE(E).
    """
    requested = [c.strip() for c in codes.replace("\n", ",").replace(" ", ",").split(",") if c.strip()]
    if not requested:
        return '종목코드를 하나 이상 입력하세요 (예: "005930,000660")'

    targets = requested[:_COMPARE_MAX]
    overflow = len(requested) - len(targets)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_compare_one, targets))

    lines = [
        f"종목 비교 — {len(targets)}종목",
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

    ok = [r for r in results if not r["failed"]]
    notes = [
        "단위 — 현재가·EPS(E): 원, PER·PBR: 배, OPM·ROE: %. 선행PER = 현재가 ÷ EPS(E)이며, "
        "EPS(E)가 0 이하(적자 추정)면 `-`입니다."
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
    if overflow > 0:
        notes.append(f"입력한 {len(requested)}종목 중 **앞 {_COMPARE_MAX}개만 조회**했습니다(나머지 {overflow}개 생략).")

    closed = [r["name"] for r in ok if r["status"] and r["status"] != "OPEN"]
    if closed and len(closed) == len(ok):
        lines.append("")
        lines.append("※ 장 마감 상태이므로 현재가는 종가입니다.")

    return "\n".join(lines) + "\n\n" + "\n".join(f"> {n}" for n in notes)

# 호스팅 플랫폼의 헬스체크용 엔드포인트. MCP 자체는 /mcp 에서 동작합니다.
@mcp.custom_route("/", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "korean-stock-mcp", "mcp": "/mcp"})


if __name__ == "__main__":
    # Streamable HTTP 트랜스포트로 실행. MCP 엔드포인트: http://<host>:<port>/mcp
    mcp.run(transport="streamable-http")
