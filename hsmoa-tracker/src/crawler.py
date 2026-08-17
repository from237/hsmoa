"""
hsmoa.com 편성표 수집기.

설계 의도
---------
hsmoa 는 클라이언트 렌더링 SPA 라서 HTML 만 받아오면 편성표가 비어 있다.
그렇다고 내부 API 주소를 하드코딩하면 사이트가 개편되는 순간 조용히 죽는다.

그래서 이 크롤러는 **엔드포인트를 몰라도 동작**하도록 만들었다.

  1) Playwright 로 실제 페이지를 띄우고, 오가는 모든 XHR/fetch 응답을 가로챈다.
  2) 가로챈 JSON 들을 재귀적으로 훑어, "편성표 레코드 배열처럼 생긴" 구조를
     점수화해서 자동으로 찾아낸다. (schema sniffing)
  3) JSON 에서 못 찾으면 DOM 을 직접 긁는 폴백으로 넘어간다.
  4) 무슨 일이 있었든 원본 payload 와 HTML 을 data/raw/ 에 남긴다.
     -> 파싱이 실패해도 데이터는 잃지 않고, 다음 실행 때 파서만 고치면 된다.
  5) 발견한 API 후보를 data/discovery.json 에 기록한다.
     -> 실제 엔드포인트가 확인되면 이후엔 그걸 직접 때려서 훨씬 빠르게 돌 수 있다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

BASE_URL = "https://hsmoa.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 편성 레코드에서 기대하는 필드들. 실제 키 이름은 사이트마다 다르므로 후보를 넓게 잡는다.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "product_name": ("name", "title", "productname", "prodname", "goodsname",
                     "itemname", "product_name", "prod_nm", "goods_nm", "subject"),
    "brand": ("brand", "brandname", "brand_name", "maker", "manufacturer",
              "brand_nm", "vendor"),
    "channel": ("site", "sitename", "channel", "channelname", "shop", "shopname",
                "mall", "mallname", "site_name", "channel_name", "company"),
    "price": ("price", "saleprice", "sale_price", "dcprice", "discountprice",
              "finalprice", "amount", "cost"),
    "start_time": ("starttime", "start_time", "start", "airtime", "air_time",
                   "broadcasttime", "time", "start_dt", "startdate", "sdate"),
    "end_time": ("endtime", "end_time", "end", "end_dt", "enddate", "edate"),
    "category": ("category", "categoryname", "cate", "catename", "cate_nm",
                 "category_name", "genre", "depth1", "cate1"),
    "url": ("url", "link", "producturl", "product_url", "detailurl", "href"),
    "image": ("img", "image", "imgurl", "image_url", "thumbnail", "thumb"),
}

# JSON 배열이 "편성표"인지 판단할 때 쓰는 필수 신호
REQUIRED_SIGNALS = ("product_name",)
SCORING_SIGNALS = ("product_name", "price", "start_time", "channel", "brand", "image")

TIME_RE = re.compile(r"\b([01]?\d|2[0-3])\s*[:시]\s*([0-5]\d)\b")
PRICE_RE = re.compile(r"[\d,]{3,}\s*원")


# --------------------------------------------------------------------------- #
# 자료구조
# --------------------------------------------------------------------------- #
@dataclass
class RawItem:
    """정규화 전, 소스에서 갓 뽑아낸 한 건."""
    product_name: str = ""
    brand: str = ""
    channel: str = ""
    price: str = ""
    start_time: str = ""
    end_time: str = ""
    category: str = ""
    url: str = ""
    image: str = ""
    air_date: str = ""
    source: str = ""          # 'json' | 'dom'
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("extra", None)
        return d


@dataclass
class CrawlResult:
    items: list[RawItem]
    endpoints: list[dict]
    method: str               # 'json' | 'dom' | 'none'
    air_date: str


# --------------------------------------------------------------------------- #
# JSON 스키마 스니핑
# --------------------------------------------------------------------------- #
def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _match_field(keys_norm: dict[str, str], logical: str) -> str | None:
    """정규화된 키맵에서 logical 필드에 해당하는 원본 키를 찾는다."""
    aliases = FIELD_ALIASES[logical]
    # 1순위: 완전 일치
    for alias in aliases:
        if alias in keys_norm:
            return keys_norm[alias]
    # 2순위: 부분 포함 (예: 'goodsNameKor' -> 'goodsname' 포함)
    for alias in aliases:
        for nk, orig in keys_norm.items():
            if alias in nk:
                return orig
    return None


def _score_records(records: list[dict]) -> tuple[int, dict[str, str]]:
    """dict 배열이 편성 레코드처럼 생겼는지 점수화. (점수, 필드매핑) 반환."""
    sample = [r for r in records[:25] if isinstance(r, dict)]
    if not sample:
        return 0, {}

    # 표본 전체에서 등장하는 키를 모아 매핑을 만든다 (레코드마다 키가 조금씩 다를 수 있음)
    keys_norm: dict[str, str] = {}
    for rec in sample:
        for k in rec.keys():
            keys_norm.setdefault(_norm_key(k), k)

    mapping = {}
    for logical in FIELD_ALIASES:
        hit = _match_field(keys_norm, logical)
        if hit:
            mapping[logical] = hit

    if not all(sig in mapping for sig in REQUIRED_SIGNALS):
        return 0, {}

    score = sum(3 for sig in SCORING_SIGNALS if sig in mapping)
    # 값이 실제로 채워져 있는지도 본다 (키만 있고 전부 빈 값이면 감점)
    filled = 0
    for rec in sample:
        val = rec.get(mapping.get("product_name", ""), "")
        if isinstance(val, str) and val.strip():
            filled += 1
    if filled == 0:
        return 0, {}
    score += min(len(records) // 10, 20)      # 레코드가 많을수록 진짜 편성표일 확률↑
    score += int(10 * filled / len(sample))
    return score, mapping


def _walk_for_arrays(node: Any, path: str = "$", depth: int = 0
                     ) -> Iterable[tuple[str, list]]:
    """JSON 트리를 훑으며 dict 들로 채워진 배열을 모두 찾아낸다."""
    if depth > 8:
        return
    if isinstance(node, list):
        if node and sum(isinstance(x, dict) for x in node) >= max(1, len(node) // 2):
            yield path, node
        for i, child in enumerate(node[:5]):
            yield from _walk_for_arrays(child, f"{path}[{i}]", depth + 1)
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_for_arrays(v, f"{path}.{k}", depth + 1)


def extract_from_payloads(payloads: list[dict]) -> tuple[list[RawItem], list[dict]]:
    """가로챈 JSON 응답들에서 편성 레코드를 뽑아낸다."""
    candidates: list[tuple[int, dict[str, str], list, str]] = []

    for p in payloads:
        try:
            for path, arr in _walk_for_arrays(p["json"]):
                score, mapping = _score_records(arr)
                if score > 0:
                    candidates.append((score, mapping, arr, f'{p["url"]} {path}'))
        except Exception as exc:               # noqa: BLE001
            log.debug("payload walk 실패 %s: %s", p.get("url"), exc)

    if not candidates:
        return [], []

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_score = candidates[0][0]
    # 최고점의 60% 이상인 후보는 모두 채택 (채널별로 응답이 쪼개져 오는 경우 대비)
    chosen = [c for c in candidates if c[0] >= best_score * 0.6]

    items: list[RawItem] = []
    used: list[dict] = []
    seen_ids: set[int] = set()

    for score, mapping, arr, origin in chosen:
        if id(arr) in seen_ids:
            continue
        seen_ids.add(id(arr))
        used.append({"origin": origin, "score": score,
                     "mapping": mapping, "records": len(arr)})
        for rec in arr:
            if not isinstance(rec, dict):
                continue
            item = RawItem(source="json")
            for logical, key in mapping.items():
                val = rec.get(key, "")
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)
                setattr(item, logical, "" if val is None else str(val).strip())
            if item.product_name:
                items.append(item)

    return items, used


# --------------------------------------------------------------------------- #
# DOM 폴백
# --------------------------------------------------------------------------- #
DOM_SCRIPT = r"""
() => {
  // 편성표 카드는 "시간 + 상품명 + 가격"이 한 컨테이너에 모여 있다는 점을 이용한다.
  const timeRe = /\b([01]?\d|2[0-3])\s*[:시]\s*([0-5]\d)\b/;
  const priceRe = /[\d,]{3,}\s*원/;
  const out = [];
  const seen = new Set();

  // 잎에 가까운 요소부터 올라가며, 시간과 가격을 동시에 품은 최소 컨테이너를 찾는다.
  const all = Array.from(document.querySelectorAll('li, article, .item, [class*="item"], [class*="card"], [class*="prod"], [class*="goods"]'));

  for (const el of all) {
    const text = (el.innerText || '').trim();
    if (!text || text.length > 600) continue;
    if (!timeRe.test(text)) continue;
    if (!priceRe.test(text)) continue;

    // 더 작은 자식이 이미 조건을 만족하면 부모는 건너뛴다 (중복 방지)
    const childHit = Array.from(el.querySelectorAll('*')).some(c => {
      const t = (c.innerText || '').trim();
      return t && t.length < text.length && timeRe.test(t) && priceRe.test(t);
    });
    if (childHit) continue;

    const key = text.slice(0, 120);
    if (seen.has(key)) continue;
    seen.add(key);

    const a = el.querySelector('a[href]');
    const img = el.querySelector('img');
    out.push({
      text,
      url: a ? a.href : '',
      image: img ? (img.src || img.getAttribute('data-src') || '') : '',
      alt: img ? (img.alt || '') : '',
      html: el.className || ''
    });
  }
  return out;
}
"""


def parse_dom_blocks(blocks: list[dict]) -> list[RawItem]:
    """DOM 에서 긁은 텍스트 덩어리를 필드로 쪼갠다."""
    items: list[RawItem] = []
    for b in blocks:
        text: str = b.get("text", "")
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue

        tm = TIME_RE.search(text)
        start_time = f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else ""

        pm = PRICE_RE.search(text)
        price = pm.group(0) if pm else ""

        # 상품명 후보: 시간/가격/짧은 라벨을 뺀 가장 긴 줄
        def is_noise(ln: str) -> bool:
            if TIME_RE.fullmatch(ln.strip()):
                return True
            if PRICE_RE.fullmatch(ln.strip()):
                return True
            return len(ln) < 4

        name_pool = [ln for ln in lines if not is_noise(ln)]
        # 가격/시간 문자열이 통째로 들어간 줄은 이름일 가능성이 낮음
        name_pool.sort(key=lambda ln: (PRICE_RE.search(ln) is not None, -len(ln)))
        product_name = name_pool[0] if name_pool else (b.get("alt") or "")

        if not product_name:
            continue

        items.append(RawItem(
            product_name=product_name,
            price=price,
            start_time=start_time,
            url=b.get("url", ""),
            image=b.get("image", ""),
            source="dom",
        ))
    return items


# --------------------------------------------------------------------------- #
# 메인 수집 루틴
# --------------------------------------------------------------------------- #
async def crawl_date(air_date: _date, raw_dir: Path, headless: bool = True,
                     scroll_rounds: int = 40, settle_ms: int = 1200) -> CrawlResult:
    ymd = air_date.strftime("%Y%m%d")
    url = f"{BASE_URL}?date={ymd}"
    payloads: list[dict] = []
    endpoints: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--lang=ko-KR"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 2400},
        )
        page = await ctx.new_page()

        async def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct.lower():
                    return
                if resp.status >= 400:
                    return
                body = await resp.json()
            except Exception:                   # noqa: BLE001
                return
            payloads.append({"url": resp.url, "json": body})
            endpoints.append({"url": resp.url, "status": resp.status,
                              "content_type": ct})

        page.on("response", on_response)

        log.info("페이지 로드: %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:                       # noqa: BLE001
            log.debug("networkidle 대기 timeout — 계속 진행")

        # 무한 스크롤: 높이가 더 이상 안 늘어날 때까지 내린다
        prev_height = 0
        stagnant = 0
        for i in range(scroll_rounds):
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(settle_ms)
            height = await page.evaluate("document.body.scrollHeight")
            if height <= prev_height:
                stagnant += 1
                if stagnant >= 3:
                    log.info("스크롤 종료 (round=%d, height=%d)", i + 1, height)
                    break
            else:
                stagnant = 0
            prev_height = height

        html = await page.content()
        dom_blocks = await page.evaluate(DOM_SCRIPT)

        await ctx.close()
        await browser.close()

    # ---- 원본 보존 (파싱이 깨져도 데이터는 남는다) ----
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{ymd}.html").write_text(html, encoding="utf-8")
    if payloads:
        slim = [{"url": p["url"], "json": p["json"]} for p in payloads]
        (raw_dir / f"{ymd}.payloads.json").write_text(
            json.dumps(slim, ensure_ascii=False)[:60_000_000], encoding="utf-8")

    # ---- 1순위: JSON ----
    items, used = extract_from_payloads(payloads)
    method = "json"

    # ---- 2순위: DOM ----
    if len(items) < 5:
        log.warning("JSON 에서 %d건만 추출 — DOM 폴백으로 전환", len(items))
        dom_items = parse_dom_blocks(dom_blocks)
        if len(dom_items) > len(items):
            items, method = dom_items, "dom"

    if not items:
        method = "none"

    for it in items:
        it.air_date = air_date.isoformat()

    log.info("%s: %d건 수집 (method=%s, 응답 %d개 관측)",
             ymd, len(items), method, len(endpoints))

    return CrawlResult(items=items, endpoints=endpoints,
                       method=method, air_date=air_date.isoformat())


def crawl_dates(dates: list[_date], raw_dir: Path, headless: bool = True
                ) -> list[CrawlResult]:
    async def _run():
        results = []
        for d in dates:
            try:
                results.append(await crawl_date(d, raw_dir, headless=headless))
            except Exception as exc:            # noqa: BLE001
                log.error("%s 수집 실패: %s", d, exc, exc_info=True)
            await asyncio.sleep(3)              # 서버 배려: 날짜 사이 3초 간격
        return results

    return asyncio.run(_run())
