#!/usr/bin/env python3
"""Korean Stock Remote MCP Server — 네이버 증권 기반 한국 주식 데이터 원격 MCP 서버

원본(stdio 방식, agent504330-ux/mcp-korean-stock)을 claude.ai 커스텀 커넥터에서
쓸 수 있도록 Streamable HTTP 트랜스포트로 개조한 버전입니다.

- 공개 인터넷에 배포한 뒤 claude.ai 설정 > 커넥터 > 커스텀 커넥터 추가에서
  URL(예: https://<your-host>/mcp)을 등록하면 모바일/데스크톱/웹에 동기화됩니다.
- 데이터 출처: 네이버 증권 모바일 API. 개인 용도로만 사용하세요.
- 인증 없음(authless), 읽기 전용(GET 요청만). 외부로 데이터를 보내지 않습니다.

도구:
  - stock_price   : 종목 현재가
  - stock_detail  : 상세 정보 (시가/고가/저가/거래량/시총/PER/PBR 등)
  - stock_search  : 종목명으로 종목코드 검색
  - market_index  : KOSPI/KOSDAQ 지수
  - stock_news    : 종목 관련 최신 뉴스
"""

import json
import os
import urllib.parse
import urllib.request

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
        "PER", "EPS", "PBR", "BPS", "배당수익률",
    ]
    for key in field_order:
        if key in infos:
            lines.append(f"{key}: {infos[key]}")

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
            date = item.get("datetime", item.get("dt", ""))
            lines.append(f"  - {title} ({source}, {date})")

    return "\n".join(lines) if len(lines) > 2 else f"종목 {code} 뉴스 파싱 실패"


# 호스팅 플랫폼의 헬스체크용 엔드포인트. MCP 자체는 /mcp 에서 동작합니다.
@mcp.custom_route("/", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "korean-stock-mcp", "mcp": "/mcp"})


if __name__ == "__main__":
    # Streamable HTTP 트랜스포트로 실행. MCP 엔드포인트: http://<host>:<port>/mcp
    mcp.run(transport="streamable-http")
