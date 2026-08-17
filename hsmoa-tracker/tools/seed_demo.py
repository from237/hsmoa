#!/usr/bin/env python3
"""
데모 데이터 주입기 — 실제 수집 전에 대시보드가 어떻게 보이는지 미리 확인용.

  python tools/seed_demo.py          # 14일치 가짜 편성 생성 -> docs/index.html
  python tools/seed_demo.py --clean  # 데모 DB 삭제

실제 수집 DB(data/hsmoa.sqlite)를 건드리지 않고 data/demo.sqlite 에 따로 쌓는다.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import classify as C           # noqa: E402
from src.dashboard import generate      # noqa: E402
from src.store import Store             # noqa: E402

CATALOG = [
    ("[락앤락] 스텐 밀폐용기 20종 세트", 89000),
    ("정관장 홍삼정 에브리타임 30포 기획", 159000),
    ("설화수 자음생크림 60ml 2개 기획세트", 320000),
    ("제주 은갈치 손질 10팩 냉동", 79000),
    ("LG 트롬 오브제컬렉션 건조기 20kg", 1290000),
    ("코오롱스포츠 경량 구스 패딩 자켓", 249000),
    ("쿠쿠 트윈프레셔 6인용 압력밥솥", 399000),
    ("시몬스 뷰티레스트 매트리스 퀸", 890000),
    ("휘슬러 구스 차렵이불 세트 퀸", 189000),
    ("삼성 갤럭시탭 S10 128GB 와이파이", 649000),
    ("테일러메이드 스텔스2 드라이버", 549000),
    ("하기스 매직컴포트 기저귀 4팩", 59000),
    ("로얄캐닌 인도어 고양이 사료 4kg", 78000),
    ("베트남 다낭 4박5일 자유여행 패키지", 699000),
    ("MLB 빅로고 볼캡 모자 2개 세트", 69000),
    ("[한국야쿠르트] 프리미엄 유산균 60포", 99000),
    ("일리 캡슐커피 100캡슐 기획", 89000),
    ("한우 1++ 등심 선물세트 1.2kg", 289000),
    ("다이슨 에어랩 컴플리트 롱", 699000),
    ("아디다스 러닝화 울트라부스트", 159000),
    ("풀무원 칼국수 6인분 밀키트", 29900),
    ("에이스침대 원매트리스 슈퍼싱글", 690000),
    ("[비비고] 왕교자 만두 1.05kg 5봉", 39900),
    ("코렐 винтаж 접시 12P 홈세트", 119000),
    ("닥터지 레드블레미쉬 크림 70ml", 45000),
    ("네파 등산화 고어텍스 경량", 189000),
    ("SK매직 직수형 정수기 렌탈", 29900),
    ("종근당 락토핏 골드 3박스", 49000),
    ("현대카드 제휴 상조 서비스", 3900000),
    ("삼성 비스포크 무풍 에어컨 17평", 1890000),
]

CHANNELS = [
    "GS샵", "CJ온스타일", "현대홈쇼핑", "롯데홈쇼핑", "NS홈쇼핑",
    "홈앤쇼핑", "공영쇼핑", "GS마이샵", "CJ온스타일플러스",
    "현대홈쇼핑플러스샵", "롯데원티비", "NS샵플러스", "신세계쇼핑",
    "K쇼핑", "SK스토아", "W쇼핑", "쇼핑엔티", "아이디지털홈쇼핑",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    db = ROOT / "data" / "demo.sqlite"
    if args.clean:
        db.unlink(missing_ok=True)
        print("데모 DB 삭제 완료")
        return 0

    rng = random.Random(20260817)
    tax, chmap = C.Taxonomy(), C.ChannelMap()
    store = Store(db)

    names = [n for n, _ in CATALOG]
    brand_dict = C.learn_brand_dictionary(names * 3, tax, min_support=3)

    rows = []
    today = date.today()
    for d in range(args.days):
        day = today - timedelta(days=args.days - 1 - d)
        for ch in CHANNELS:
            # 채널마다 하루 12~26개 슬롯
            for _ in range(rng.randint(12, 26)):
                raw_name, base_price = rng.choice(CATALOG)
                hh, mm = rng.randint(0, 23), rng.choice([0, 10, 20, 30, 40, 50])
                hhmm = f"{hh:02d}:{mm:02d}"
                clean, tags = C.clean_product_name(raw_name, tax)
                rows.append({
                    "air_date": day.isoformat(),
                    "start_iso": f"{day.isoformat()}T{hhmm}",
                    "start_hhmm": hhmm,
                    "time_slot": C.time_slot(hhmm),
                    "channel": ch,
                    "channel_type": chmap.normalize(ch)["type"],
                    "channel_group": chmap.normalize(ch)["group"],
                    "brand": C.extract_brand(clean, tax, brand_dict),
                    "product_name": clean,
                    "product_name_raw": raw_name,
                    "category": tax.classify(clean),
                    "source_category": "",
                    "price": int(base_price * rng.uniform(0.85, 1.15)),
                    "promo_tags": ",".join(tags),
                    "url": "https://hsmoa.com/",
                    "image": "",
                    "source": "demo",
                })

    total, new = store.upsert_many(rows)
    store.export_csv(ROOT / "data" / "demo_schedule.csv")
    out = generate(store, ROOT / "docs" / "index.html")
    print(f"데모 {total}건 생성 (신규 {new}) -> {out}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
