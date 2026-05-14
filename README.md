# Korean Stock Remote MCP Server

네이버 증권 API 기반 한국 주식 데이터 MCP 서버를 **claude.ai 커스텀 커넥터**로 쓸 수
있도록 만든 **원격(HTTP) 버전**입니다. 원본(`agent504330-ux/mcp-korean-stock`)은
로컬 stdio 방식이라 모바일에서 못 쓰지만, 이 버전은 공개 인터넷에 배포한 뒤 URL만
등록하면 **모바일·데스크톱·웹 모든 클로드 앱에서** 동작합니다.

## 제공 도구

| 도구 | 설명 |
| --- | --- |
| `stock_price` | 종목 현재가 |
| `stock_detail` | 상세 정보 (시가/고가/저가/거래량/시총/PER/PBR/배당수익률 등) |
| `stock_search` | 종목명으로 종목코드 검색 |
| `market_index` | KOSPI/KOSDAQ 지수 |
| `stock_news` | 종목 관련 최신 뉴스 |

데이터 출처: 네이버 증권 모바일 API. **개인 용도로만 사용하세요.**

---

## 배포 방법 (Render 기준, 무료)

Render가 비개발자에게 가장 쉽습니다. GitHub 계정과 Render 계정(둘 다 무료)이 필요합니다.

### 1단계 — GitHub에 올리기

이 폴더(`server.py`, `requirements.txt`, `render.yaml`, `.gitignore`)를 본인 GitHub
계정의 새 저장소에 올립니다.

```bash
cd korean-stock-remote
git init
git add server.py requirements.txt render.yaml .gitignore README.md
git commit -m "Korean stock remote MCP server"
git branch -M main
git remote add origin https://github.com/<본인계정>/korean-stock-remote.git
git push -u origin main
```

### 2단계 — Render에 배포

1. https://render.com 가입 후 로그인
2. 대시보드에서 **New +** → **Web Service**
3. 1단계에서 만든 GitHub 저장소를 연결
4. 설정은 `render.yaml`이 자동으로 채웁니다. 비어 있으면 수동 입력:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
   - **Plan**: Free
5. **Create Web Service** 클릭 → 빌드가 끝나면 `https://<서비스명>.onrender.com` 형태의
   URL이 발급됩니다.
6. 브라우저에서 `https://<서비스명>.onrender.com/` 을 열어
   `{"status":"ok",...}` 가 보이면 정상 배포된 것입니다.

### 3단계 — claude.ai에 커넥터 등록

> ⚠️ 모바일 앱에서는 커넥터를 **추가**할 수 없습니다. 반드시 **claude.ai 웹사이트**에서
> 등록해야 하며, 등록하면 모바일에 자동 동기화됩니다.

1. 브라우저에서 claude.ai 접속 → **설정(Settings)** → **커넥터(Connectors)**
2. **커스텀 커넥터 추가(Add custom connector)** 클릭
3. 입력:
   - **이름**: `Korean Stock` (자유)
   - **URL**: `https://<서비스명>.onrender.com/mcp`  ← 끝에 **`/mcp`** 꼭 붙이기
4. 추가 후 연결되면, 모바일 앱에도 곧 반영됩니다.

### 4단계 — 테스트

클로드와의 대화에서 도구 사용을 허용한 뒤:

- "에스티아이 현재가 알려줘" → `stock_price` 또는 먼저 `stock_search`로 코드 확인
- "039440 상세 정보 보여줘" → `stock_detail`
- "코스닥 지수 얼마야?" → `market_index`

---

## 알아둘 한계와 주의사항

- **네이버 API 차단 가능성 (미검증):** 클라우드 호스트 IP에서 네이버 모바일 API가
  정상 응답할지는 배포해서 호출해봐야 확실합니다. 차단되거나 속도 제한이 걸릴 수
  있습니다. 작동을 보장할 수 없습니다.
- **무료 플랜 콜드 스타트:** Render 무료 플랜은 일정 시간 미사용 시 잠들고, 다음 첫
  요청에서 깨어나는 데 30~60초가 걸립니다. 첫 조회가 느릴 수 있습니다.
- **인증 없음(authless):** URL을 아는 사람은 누구나 호출할 수 있습니다. 읽기 전용
  공개 시세 데이터라 위험은 낮지만, URL을 함부로 공유하지 마세요. 필요하면 나중에
  공유 비밀키 검사 같은 보호를 추가할 수 있습니다.
- **읽기 전용:** 이 서버는 네이버에 GET 요청만 하며, 데이터를 외부로 보내지 않습니다.
- **네이버 약관:** 비공식 API 사용이므로 개인 용도로만 사용하세요.

## 문제 해결

- **claude.ai에서 "Disconnected" 표시:** URL 끝에 `/mcp`가 빠졌는지 확인. 브라우저로
  `/` 헬스체크가 응답하는지 먼저 확인.
- **조회는 되는데 "조회 실패" 메시지:** 네이버 API가 해당 호스트를 차단/제한하는
  경우일 수 있습니다. Render 로그(대시보드 → Logs)에서 오류 확인.
- **종목코드를 모를 때:** `stock_search`로 한글 종목명을 먼저 검색하세요.

## 로컬 테스트 (선택)

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
python server.py        # http://localhost:8000/mcp 에서 동작
```
