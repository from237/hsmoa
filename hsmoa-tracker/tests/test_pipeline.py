"""파서·분류기 단위 테스트. 실행: python -m pytest tests/ -q"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import classify as C                     # noqa: E402
from src.crawler import (RawItem, _score_records,  # noqa: E402
                         extract_from_payloads, parse_dom_blocks)

TAX = C.Taxonomy()
CHMAP = C.ChannelMap()


# ------------------------------------------------------------------ 카테고리 #
def test_category_rules():
    cases = {
        "락앤락 스텐 밀폐용기 20종 세트": "생활용품",
        "정관장 홍삼정 에브리타임 30포": "건강기능식품",
        "설화수 자음생크림 60ml 기획세트": "뷰티",
        "제주 은갈치 손질 10팩": "식품",
        "LG 트롬 오브제컬렉션 건조기 20kg": "가전",
        "코오롱스포츠 경량 패딩 자켓": "패션의류",
        "쿠쿠 6인용 압력밥솥": "가전",
        "시몬스 뷰티레스트 매트리스 퀸": "가구인테리어",
        "휘슬러 구스 차렵이불 세트": "침구",
        "삼성 갤럭시탭 S10 128GB": "디지털",
        "테일러메이드 스텔스 드라이버": "스포츠레저",
        "하기스 매직컴포트 기저귀 4팩": "유아동",
        "로얄캐닌 인도어 고양이 사료 4kg": "반려동물",
        "베트남 다낭 4박5일 패키지": "여행서비스",
        "MLB 빅로고 볼캡 모자": "패션잡화",
    }
    for name, expected in cases.items():
        got = TAX.classify(name)
        assert got == expected, f"{name!r} -> {got} (기대: {expected})"


def test_health_food_beats_food():
    # '홍삼정'은 식품(인삼)보다 건강기능식품이 먼저 잡혀야 한다
    assert TAX.classify("6년근 홍삼정 스틱") == "건강기능식품"


# ---------------------------------------------------------------- 상품명 정제 #
def test_clean_promo_prefix():
    name, tags = C.clean_product_name("[단독][방송최초] 락앤락 밀폐용기 20종", TAX)
    assert "단독" in tags and "방송최초" in tags
    assert name.startswith("락앤락")


def test_keeps_brand_bracket():
    # 프로모션 머리말이 아닌 브래킷은 브랜드일 수 있으므로 남긴다
    name, tags = C.clean_product_name("[락앤락] 밀폐용기 20종", TAX)
    assert name.startswith("[락앤락]")
    assert tags == []


# -------------------------------------------------------------------- 브랜드 #
def test_brand_from_bracket():
    assert C.extract_brand_rule_based("[락앤락] 밀폐용기 20종", TAX) == "락앤락"


def test_brand_rejects_stopword():
    assert C.extract_brand_rule_based("[세트] 밀폐용기 20종", TAX) == ""


def test_brand_dictionary_learning():
    corpus = [
        "락앤락 밀폐용기 20종", "락앤락 스텐 보온병", "락앤락 텀블러 3종",
        "설화수 자음생크림", "설화수 윤조에센스", "설화수 순행클렌징",
        "일회성상품 특이한거 하나",
    ]
    learned = C.learn_brand_dictionary(corpus, TAX, min_support=3)
    assert "락앤락" in learned
    assert "설화수" in learned
    assert "일회성상품" not in learned
    assert C.extract_brand("락앤락 신제품 도마", TAX, learned) == "락앤락"


def test_brand_support_counts_distinct_products():
    # 같은 상품명이 100번 반복돼도 지지도는 1 -> 브랜드로 승격되면 안 된다
    corpus = ["반복브랜드 같은상품"] * 100
    learned = C.learn_brand_dictionary(corpus, TAX, min_support=3)
    assert "반복브랜드" not in learned


# -------------------------------------------------------------------- 시간 #
def test_parse_air_time_variants():
    d = "2026-08-17"
    assert C.parse_air_time("10:30", d)[1] == "10:30"
    assert C.parse_air_time("2026-08-17T14:05:00", d)[1] == "14:05"
    assert C.parse_air_time("1430", d)[1] == "14:30"
    assert C.parse_air_time("오후 3시 20분", d)[1] == "15:20"
    assert C.parse_air_time("", d) == ("", "")


def test_epoch_to_kst():
    """epoch 는 KST 로 변환돼야 한다 (러너가 UTC 여도)."""
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    target = datetime(2026, 8, 17, 10, 30, tzinfo=kst)
    iso, hhmm = C.parse_air_time(str(int(target.timestamp())), "2026-08-17")
    assert hhmm == "10:30"
    assert iso.startswith("2026-08-17T10:30")
    # 밀리초 epoch 도 같은 결과
    assert C.parse_air_time(str(int(target.timestamp()) * 1000), "2026-08-17")[1] == "10:30"


def test_single_syllable_keyword_is_rejected(tmp_path):
    """설정에 1음절 한글 키워드가 다시 들어오면 로드 시점에 터져야 한다."""
    import pytest
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "categories:\n"
        "  - name: 식품\n    priority: 10\n    keywords:\n      - 국, 마, 게\n"
        "brand_extraction: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="1음절"):
        C.Taxonomy(bad)


def test_classify_ignores_channel_noise():
    # 'TV홈쇼핑' 문구가 가전으로 오분류되면 안 된다
    assert TAX.classify("TV홈쇼핑 방송최초 정관장 홍삼정") == "건강기능식품"


def test_no_false_positive_on_common_words():
    """과거 버그 재발 방지: 흔한 단어가 엉뚱한 카테고리로 가면 안 된다."""
    assert TAX.classify("국내산 무료배송 요가매트") == "스포츠레저"
    assert TAX.classify("농심 신라면 컵라면 30개") == "식품"
    assert TAX.classify("풀무원 칼국수 6인분") == "식품"
    assert TAX.classify("시몬스 매트리스 퀸") == "가구인테리어"


def test_time_slot_buckets():
    assert C.time_slot("07:30") == "아침 06-09"
    assert C.time_slot("23:10") == "밤 21-24"
    assert C.time_slot("03:00") == "새벽 03-06"
    assert C.time_slot("00:05") == "심야 00-03"
    assert C.time_slot("") == "미상"
    assert C.time_slot("이상한값") == "미상"


def test_time_slots_are_uniform_width():
    """버킷 폭이 다르면 히트맵이 '편성이 몰렸다'는 착시를 만든다."""
    starts = [h for h, _ in C.TIME_SLOTS]
    widths = [b - a for a, b in zip(starts, starts[1:] + [24])]
    assert set(widths) == {3}, f"버킷 폭이 균일하지 않음: {widths}"
    # 0~23시가 빠짐없이 하나의 버킷에 배정되는지
    assert len({C.time_slot(f"{h:02d}:00") for h in range(24)}) == len(C.TIME_SLOTS)


def test_parse_price():
    assert C.parse_price("139,000원") == 139000
    assert C.parse_price("￦89,900") == 89900
    assert C.parse_price("") is None
    assert C.parse_price("품절") is None


# -------------------------------------------------------------------- 채널 #
def test_channel_normalization():
    assert CHMAP.normalize("gsshop")["canonical"] == "GS샵"
    assert CHMAP.normalize("CJ오쇼핑")["canonical"] == "CJ온스타일"
    assert CHMAP.normalize("SK스토아")["type"] == "T커머스"
    assert CHMAP.normalize("현대홈쇼핑")["type"] == "TV홈쇼핑"


def test_unknown_channel_is_preserved():
    out = CHMAP.normalize("듣도보도못한쇼핑")
    assert out["canonical"] == "듣도보도못한쇼핑"
    assert out["type"] == "미분류"


# ------------------------------------------------------- JSON 스키마 스니핑 #
def test_score_records_detects_schedule_shape():
    records = [
        {"goodsName": "락앤락 밀폐용기", "salePrice": 39000,
         "startTime": "1755400800", "siteName": "gsshop", "brandName": "락앤락"},
    ] * 12
    score, mapping = _score_records(records)
    assert score > 0
    assert mapping["product_name"] == "goodsName"
    assert mapping["price"] == "salePrice"
    assert mapping["channel"] == "siteName"


def test_score_records_rejects_unrelated_array():
    score, _ = _score_records([{"lat": 1.0, "lng": 2.0}] * 10)
    assert score == 0


def test_extract_from_nested_payload():
    payload = [{
        "url": "https://hsmoa.com/api/whatever",
        "json": {"data": {"result": {"list": [
            {"title": "설화수 자음생크림", "price": 120000,
             "start": "1030", "site": "hmall"},
            {"title": "락앤락 밀폐용기", "price": 39000,
             "start": "1130", "site": "gsshop"},
        ] * 6}}},
    }]
    items, used = extract_from_payloads(payload)
    assert len(items) == 12
    assert used and "list" in used[0]["origin"]
    assert items[0].product_name == "설화수 자음생크림"


# ---------------------------------------------------------------- DOM 폴백 #
def test_parse_dom_blocks():
    blocks = [{
        "text": "10:30\n락앤락 스텐 밀폐용기 20종 세트\n39,000원\n무이자",
        "url": "https://hsmoa.com/x", "image": "", "alt": "",
    }]
    items = parse_dom_blocks(blocks)
    assert len(items) == 1
    assert items[0].start_time == "10:30"
    assert items[0].price == "39,000원"
    assert "락앤락" in items[0].product_name


# ------------------------------------------------------------------ 저장소 #
def test_store_upsert_dedupes(tmp_path):
    from src.store import Store
    s = Store(tmp_path / "t.sqlite")
    row = {"air_date": "2026-08-17", "start_hhmm": "10:30", "channel": "GS샵",
           "product_name": "락앤락 밀폐용기", "category": "생활용품",
           "brand": "락앤락", "price": 39000}
    total, new = s.upsert_many([row])
    assert (total, new) == (1, 1)
    total, new = s.upsert_many([row])       # 같은 방송 재수집
    assert (total, new) == (1, 0)           # 신규 아님
    assert len(s.fetch_all()) == 1
    s.close()


def test_store_weekday_and_csv(tmp_path):
    from src.store import Store
    s = Store(tmp_path / "t.sqlite")
    s.upsert_many([{"air_date": "2026-08-17", "start_hhmm": "10:30",
                    "channel": "GS샵", "product_name": "테스트상품",
                    "category": "기타"}])
    assert s.fetch_all()[0]["weekday"] == "월"   # 2026-08-17 은 월요일
    n = s.export_csv(tmp_path / "out.csv")
    assert n == 1
    assert "테스트상품" in (tmp_path / "out.csv").read_text(encoding="utf-8-sig")
    s.close()
