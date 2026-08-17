#!/usr/bin/env python3
"""
hsmoa 편성 트래커 — 엔트리포인트.

  python run.py                      # 어제·오늘·내일 수집 후 대시보드 갱신
  python run.py --days 0 1 2 3       # 오늘부터 3일 뒤까지
  python run.py --date 2026-08-17    # 특정 날짜만
  python run.py --dashboard-only     # 수집 없이 대시보드만 다시 그림
  python run.py --headed             # 브라우저를 띄워서 디버깅
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import classify as C                                  # noqa: E402
from src.crawler import crawl_dates                            # noqa: E402
from src.dashboard import generate as generate_dashboard       # noqa: E402
from src.store import Store                                    # noqa: E402

KST = timezone(timedelta(hours=9))
DATA = ROOT / "data"
DOCS = ROOT / "docs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run")


def normalize_items(raw_items, taxonomy: C.Taxonomy, chmap: C.ChannelMap,
                    brand_dict: set[str]) -> list[dict]:
    """크롤러가 뱉은 RawItem 을 저장 스키마로 변환한다."""
    out: list[dict] = []
    for it in raw_items:
        name_clean, promo_tags = C.clean_product_name(it.product_name, taxonomy)
        if not name_clean:
            continue

        ch = chmap.normalize(it.channel)
        start_iso, hhmm = C.parse_air_time(it.start_time, it.air_date)

        brand = C.normalize_text(it.brand)
        if not brand:
            brand = C.extract_brand(name_clean, taxonomy, brand_dict)

        out.append({
            "air_date": it.air_date,
            "start_iso": start_iso,
            "start_hhmm": hhmm,
            "time_slot": C.time_slot(hhmm),
            "channel": ch["canonical"] or "미상",
            "channel_type": ch["type"],
            "channel_group": ch["group"],
            "brand": brand,
            "product_name": name_clean,
            "product_name_raw": C.normalize_text(it.product_name),
            "category": taxonomy.classify(name_clean, it.category),
            "source_category": C.normalize_text(it.category),
            "price": C.parse_price(it.price),
            "promo_tags": ",".join(promo_tags),
            "url": it.url,
            "image": it.image,
            "source": it.source,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", type=int, default=[-1, 0, 1],
                    help="오늘 기준 오프셋 (기본: 어제·오늘·내일)")
    ap.add_argument("--date", action="append", default=[],
                    help="YYYY-MM-DD 특정 날짜 (반복 지정 가능)")
    ap.add_argument("--dashboard-only", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--recent-days", type=int, default=90,
                    help="대시보드에 내장할 최근 일수 (그 이전 데이터는 CSV/SQLite 로 조회)")
    args = ap.parse_args()

    store = Store(DATA / "hsmoa.sqlite")
    taxonomy = C.Taxonomy()
    chmap = C.ChannelMap()

    if not args.dashboard_only:
        today = datetime.now(KST).date()
        targets: list[date] = ([datetime.strptime(d, "%Y-%m-%d").date()
                                for d in args.date]
                               or [today + timedelta(days=n) for n in args.days])
        log.info("수집 대상: %s", ", ".join(str(d) for d in targets))

        results = crawl_dates(targets, DATA / "raw", headless=not args.headed)

        # 브랜드 사전은 "기존 누적 + 이번 수집"을 합쳐서 학습한다
        fresh_names = [i.product_name for r in results for i in r.items]
        corpus = store.all_product_names() + fresh_names
        brand_dict = C.learn_brand_dictionary(corpus, taxonomy)
        log.info("브랜드 사전: %d개 학습 (코퍼스 %d건)", len(brand_dict), len(corpus))

        total_found = total_new = 0
        methods = set()
        for r in results:
            rows = normalize_items(r.items, taxonomy, chmap, brand_dict)
            found, new = store.upsert_many(rows)
            total_found += found
            total_new += new
            methods.add(r.method)
            log.info("  %s: %d건 저장 (신규 %d) [%s]",
                     r.air_date, found, new, r.method)

        # 사전이 커졌으니, 예전에 브랜드를 못 찾았던 행을 다시 채운다
        refilled = store.rebrand_all(
            lambda n: C.extract_brand(n, taxonomy, brand_dict))
        if refilled:
            log.info("기존 %d건에 브랜드 소급 반영", refilled)

        store.log_run([str(d) for d in targets], "/".join(sorted(methods)),
                      total_found, total_new)

        # API 엔드포인트 발견 기록 (다음 개선의 단서)
        endpoints = sorted({e["url"].split("?")[0]
                            for r in results for e in r.endpoints})
        (DATA / "discovery.json").write_text(
            json.dumps({"checked_at": datetime.now(KST).isoformat(timespec="seconds"),
                        "method": "/".join(sorted(methods)),
                        "json_endpoints": endpoints},
                       ensure_ascii=False, indent=2), encoding="utf-8")

        if chmap.unknown:
            (DATA / "unknown_channels.txt").write_text(
                "\n".join(f"{k}\t{v}" for k, v in chmap.unknown.most_common()),
                encoding="utf-8")
            log.warning("사전에 없는 채널 표기 %d종 — data/unknown_channels.txt 확인",
                        len(chmap.unknown))

        if total_found == 0:
            log.error("수집 0건. data/raw/ 의 원본 HTML·payload 를 확인하세요.")

    n = store.export_csv(DATA / "schedule.csv")
    out = generate_dashboard(store, DOCS / "index.html", args.recent_days)
    log.info("CSV %d행 · 대시보드 → %s", n, out)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
