# hsmoa 편성 트래커

[hsmoa.com](https://hsmoa.com/) 의 홈쇼핑 편성표를 매일 새벽 자동 수집해서
**회사명 · 방송시간 · 브랜드 · 상품명 · 품목카테고리** 다섯 축으로 정리하고,
시장의 상품 흐름을 HTML 대시보드로 보여줍니다.

GitHub Actions 에서 돌아가므로 PC 를 켜둘 필요가 없습니다.

---

## 1. 5분 세팅

### ① 저장소 만들기

GitHub 에서 새 저장소를 만들고(**Private 권장**), 이 폴더를 통째로 올립니다.

```bash
cd hsmoa-tracker
git init
git add .
git commit -m "init: hsmoa 편성 트래커"
git branch -M main
git remote add origin https://github.com/<사용자명>/<저장소명>.git
git push -u origin main
```

### ② Actions 쓰기 권한 켜기

`Settings → Actions → General → Workflow permissions`
→ **Read and write permissions** 선택 후 Save.

> 이걸 안 켜면 수집은 되지만 데이터를 저장소에 커밋하지 못하고 실패합니다.

### ③ 대시보드 공개 (선택)

`Settings → Pages → Source: Deploy from a branch`
→ Branch `main` / 폴더 `/docs` → Save.

몇 분 뒤 `https://<사용자명>.github.io/<저장소명>/` 에서 대시보드가 열립니다.
Private 저장소라면 Pages 대신 `docs/index.html` 을 내려받아 브라우저로 열면 됩니다.

### ④ 첫 실행

`Actions → hsmoa 편성 수집 → Run workflow` 를 눌러 수동으로 한 번 돌립니다.
이후로는 **매일 새벽 5시 10분(KST)** 에 자동 실행됩니다.

---

## 2. 결과물

| 경로 | 내용 |
|---|---|
| `docs/index.html` | 대시보드 (self-contained, 외부 라이브러리 없음) |
| `data/schedule.csv` | 누적 편성 전체 — Excel 에서 바로 열림 (UTF-8 BOM) |
| `data/hsmoa.sqlite` | 원본 DB. 시계열 분석은 여기에 쿼리 |
| `data/discovery.json` | 관측된 API 엔드포인트 목록 (수집 방식 진단용) |

CSV 컬럼:

```
air_date, weekday, start_hhmm, time_slot, channel, channel_type,
brand, product_name, category, price, url
```

대시보드는 5개 탭 구성입니다 (S사 방송상품 운영요약 대시보드의 프론트 구성을 따름):

| 탭 | 내용 |
|---|---|
| **Trend 분석** | 최근 5주 트렌드 Top3, 카테고리별 편성 비중, Top10 브랜드·상품 (방송분 순) |
| **브랜드/상품 분석** | 브랜드 순위 → 상품 카드 → 방송 이력 드릴다운 |
| **검색 및 다운로드** | 브랜드·상품명 통합 검색 + 결과 CSV 다운로드 |
| **주간 편성표** | Time × 요일 그리드, 회사 선택, 카테고리 하이라이트 |
| **일자별 편성표** | 날짜별 타임라인 카드 (상품 이미지·가격·hsmoa 링크) |

공통 필터: 분석 주기(월/주/일) + 세부 기간 다중 선택 + 회사명 + 채널 유형 + 품목카테고리.

**방송분(추정)에 대해**: hsmoa 편성표에는 방송 종료 시각이 없는 경우가 많아,
같은 채널의 **다음 방송 시작까지의 간격**으로 방송분을 추정합니다 (240분 초과 간격은
그날 그 채널의 중앙값으로 대체). 절대값보다는 상대 비교용으로 보시면 됩니다.
Chart.js 등은 CDN 에서 로드하며, 오프라인에서 열면 차트 대신 텍스트 순위표로 폴백합니다.

---

## 3. 로컬에서 돌려보기

```bash
pip install -r requirements.txt
python -m playwright install chromium

python run.py                    # 어제·오늘·내일 수집
python run.py --date 2026-08-17  # 특정 날짜만
python run.py --headed           # 브라우저 띄워서 눈으로 확인 (디버깅)
python run.py --dashboard-only   # 수집 없이 대시보드만 다시 그림

python tools/seed_demo.py        # 가짜 데이터로 대시보드 미리보기
python -m pytest tests/ -q       # 분류기 테스트
```

---

## 4. 수집이 0건으로 나온다면

hsmoa 는 클라이언트 렌더링 SPA 라서 HTML 만으로는 편성표가 비어 있습니다.
그래서 이 크롤러는 **API 주소를 하드코딩하지 않습니다.** 대신:

1. Playwright 로 실제 페이지를 띄우고 오가는 **모든 JSON 응답을 가로챕니다.**
2. 가로챈 JSON 을 재귀 탐색해 "편성 레코드 배열처럼 생긴" 구조를 점수화해 자동 선택합니다.
3. 그래도 못 찾으면 **DOM 을 직접 긁는 폴백**으로 넘어갑니다.
4. 무슨 일이 있었든 원본 HTML·payload 를 `data/raw/` 에 남깁니다.

사이트가 개편돼 파싱이 깨져도 **데이터 자체는 잃지 않는** 구조입니다.

수집이 0건이면 이렇게 하세요:

- 해당 Actions 실행의 **`raw-snapshot-N` 아티팩트**를 내려받습니다 (7일 보관).
- `discovery.json` 의 `json_endpoints` 에 편성 API 로 보이는 주소가 있는지 확인합니다.
- `<날짜>.payloads.json` 에서 상품명이 실제로 들어 있는지 확인합니다.
  - 들어 있다면 → `src/crawler.py` 의 `FIELD_ALIASES` 에 그 키 이름을 추가하면 끝입니다.
  - 안 들어 있다면 → `<날짜>.html` 을 보고 `DOM_SCRIPT` 의 선택자를 조정합니다.

`data/unknown_channels.txt` 에 사전에 없는 채널 표기가 쌓이면
`config/channels.yml` 의 `aliases` 에 추가해 주세요.

---

## 5. 분류 규칙 손보기

`config/taxonomy.yml` 한 파일만 고치면 됩니다. 코드 수정 불필요.

```yaml
- name: 건강기능식품
  priority: 10          # 낮을수록 먼저 검사 (첫 매치 승리)
  keywords:
    - 유산균, 프로바이오틱스, 오메가3
```

**우선순위가 곧 분류 규칙입니다.** "홍삼정"이 식품이 아니라 건강기능식품으로 가는 건
건강기능식품(10)이 식품(100)보다 먼저 검사되기 때문입니다.

> ⚠️ **1음절 한글 키워드는 넣지 마세요.**
> `국` → 국내산·한국·국산, `마` → 수많은 단어, `게` → 하게·크게,
> `배` → 배송·배터리 를 전부 오분류시킵니다.
> 로드 시점에 `ValueError` 로 막아두었으니, 필요하면 `국거리, 사골국` 처럼
> 구체적인 복합어로 바꿔 주세요.

브랜드 사전은 따로 관리할 필요가 없습니다. 누적 상품명에서
**같은 선두 토큰이 서로 다른 상품 3건 이상에 등장하면** 자동으로 브랜드로 승격됩니다.
(같은 방송이 100번 반복돼도 지지도는 1로 셉니다.) 데이터가 쌓일수록 정확해지고,
새로 학습된 브랜드는 과거 레코드에 소급 반영됩니다.

---

## 6. 알아두실 점

**수집 주기.** 하루 1회입니다. 편성표는 대개 전날 확정되므로 흐름 분석에는 충분하지만,
당일 긴급 편성 변경까지 잡으려면 `.github/workflows/crawl.yml` 의 cron 을 늘리세요.
(예: `10 20,2,8 * * *` → 하루 3회)

**서버 배려.** 날짜 사이 3초 간격을 두고, 하루 3일치(어제·오늘·내일)만 받습니다.
robots.txt 는 `/redirect` 만 막고 있어 편성표 경로는 대상이 아닙니다.
다만 hsmoa 이용약관은 별도이니, 수집 주기를 크게 늘리는 건 권하지 않습니다.

**공식 대안.** hsmoa 는 같은 데이터를 [DataHub](https://datahub.hsmoa.com/) 로
직접 판매합니다(무료 티어 있음, 유료 월 39,000원~). **매출·판매수 추정치**는
크롤링으로는 얻을 수 없는 항목이라, 시장 흐름을 넘어 성과까지 보려면 그쪽이 맞습니다.
사이트 개편에 안 깨지고 약관 리스크도 없습니다.
API 로 갈아탈 경우 `src/crawler.py` 만 교체하면 나머지 파이프라인은 그대로 씁니다.

---

## 7. 구조

```
run.py                      엔트리포인트
config/channels.yml         18개 채널 정규화 사전
config/taxonomy.yml         품목 카테고리 규칙  ← 주로 여기만 고치게 됩니다
src/crawler.py              Playwright 수집 (네트워크 인터셉트 + DOM 폴백)
src/classify.py             정규화 · 브랜드 추출 · 카테고리 분류
src/store.py                SQLite 저장 · 중복 제거 · CSV 내보내기
src/dashboard.py            HTML 대시보드 생성
tools/seed_demo.py          데모 데이터 주입기
tests/test_pipeline.py      단위 테스트
.github/workflows/crawl.yml 매일 새벽 자동 실행
```
