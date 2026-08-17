"""
정규화 & 분류.

원시 레코드를 요청하신 5개 축으로 떨어뜨린다.
    회사명 / 방송시간 / 브랜드 / 상품명 / 품목카테고리

브랜드 추출은 3단계로 간다.
    1) 소스가 brand 필드를 주면 그대로 신뢰
    2) 대괄호·괄호 등 표기 규칙에서 추출  예) "[락앤락] 밀폐용기 20종"
    3) 누적 데이터에서 빈도로 학습한 브랜드 사전과 대조
       -> 같은 선두 토큰이 서로 다른 상품 여러 건에 반복 등장하면 브랜드로 승격.
          홈쇼핑 상품명은 "브랜드 + 상품설명" 구조가 압도적이라 이 신호가 잘 먹는다.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
KST_TZ = timezone(timedelta(hours=9))


# --------------------------------------------------------------------------- #
# 설정 로드
# --------------------------------------------------------------------------- #
def _flatten_keywords(raw) -> list[str]:
    """YAML 에서 'a, b, c' 형태로 적힌 줄을 개별 키워드로 펼친다."""
    out: list[str] = []
    for entry in raw or []:
        for token in str(entry).split(","):
            token = token.strip()
            if token:
                out.append(token)
    return out


# 분류 전에 제거할 잡음. "TV홈쇼핑 최초" 같은 문구가 가전('TV')으로 오분류되는 걸 막는다.
CLASSIFY_NOISE = ("TV홈쇼핑", "티비홈쇼핑", "홈쇼핑", "방송최초", "생방송", "온에어", "ONAIR")

# 1음절 한글 키워드는 대부분 오분류를 만든다.
# ('국'->국내산, '마'->수많은 단어, '게'->하게/크게, '배'->배송/배터리)
# 설정에 실수로 다시 들어오는 걸 로드 시점에 막되,
# 아래는 실제 상품명 문맥에서 오탐이 없다고 검증된 예외다.
_HANGUL = re.compile(r"[가-힣]")
SAFE_SINGLE_SYLLABLE = {
    "쌀", "콩", "떡", "빵", "즙", "죽", "꿀", "펫", "잼",
    "괌",   # 지명 — 다른 한국어 단어에 부분 문자열로 등장하지 않음
}


class Taxonomy:
    def __init__(self, path: Path | None = None, strict: bool = True):
        data = yaml.safe_load((path or CONFIG_DIR / "taxonomy.yml").read_text("utf-8"))
        cats = sorted(data["categories"], key=lambda c: c.get("priority", 500))
        self.rules: list[tuple[str, list[str]]] = [
            (c["name"], _flatten_keywords(c.get("keywords"))) for c in cats
        ]
        if strict:
            self._reject_single_syllable()
        self.category_names = [c["name"] for c in cats]
        be = data.get("brand_extraction", {})
        self.noise_prefixes = set(_flatten_keywords(be.get("noise_prefixes")))
        self.stopwords = set(_flatten_keywords(be.get("stopwords")))

    def _reject_single_syllable(self) -> None:
        bad = [(cat, kw) for cat, kws in self.rules for kw in kws
               if len(kw) == 1 and _HANGUL.fullmatch(kw)
               and kw not in SAFE_SINGLE_SYLLABLE]
        if bad:
            detail = ", ".join(f"{c}:'{k}'" for c, k in bad)
            raise ValueError(
                f"1음절 한글 키워드는 오분류를 유발하므로 금지합니다 -> {detail}. "
                "구체적인 복합어로 바꾸세요 (예: '국' -> '국거리, 사골국')."
            )

    def classify(self, product_name: str, source_category: str = "") -> str:
        """상품명(+ 소스가 준 카테고리)으로 품목 카테고리를 정한다."""
        haystack = normalize_text(f"{product_name} {source_category}")
        for noise in CLASSIFY_NOISE:
            haystack = haystack.replace(noise, " ")
        for name, keywords in self.rules:
            for kw in keywords:
                if kw and kw in haystack:
                    return name
        return "기타"


class ChannelMap:
    def __init__(self, path: Path | None = None):
        data = yaml.safe_load((path or CONFIG_DIR / "channels.yml").read_text("utf-8"))
        self.lookup: dict[str, dict] = {}
        self.meta: dict[str, dict] = {}
        for ch in data["channels"]:
            canon = ch["canonical"]
            self.meta[canon] = {"type": ch.get("type", ""), "group": ch.get("group", "")}
            for alias in ch.get("aliases", []) + [canon]:
                self.lookup[_squash(alias)] = {"canonical": canon, **self.meta[canon]}
        self.unknown: Counter = Counter()

    def normalize(self, raw: str) -> dict:
        key = _squash(raw)
        if not key:
            return {"canonical": "", "type": "", "group": ""}
        if key in self.lookup:
            return self.lookup[key]
        # 부분 일치 (예: "GS샵(모바일)" -> GS샵)
        for alias_key, meta in self.lookup.items():
            if len(alias_key) >= 3 and alias_key in key:
                return meta
        self.unknown[raw] += 1
        return {"canonical": raw.strip(), "type": "미분류", "group": ""}


# --------------------------------------------------------------------------- #
# 텍스트 유틸
# --------------------------------------------------------------------------- #
def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _squash(s: str) -> str:
    """비교용: 공백/기호 제거 + 소문자."""
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    return re.sub(r"[^0-9a-z가-힣]", "", s)


BRACKET_RE = re.compile(r"^\s*[\[\(<【（]\s*([^\]\)>】）]{1,20})\s*[\]\)>】）]\s*")
TRAILING_NOISE_RE = re.compile(
    r"(무이자|일시불|１\+１|1\+1|사은품|무료배송|최대\s*\d+%|\d+%\s*할인|카드할인)"
)


def clean_product_name(raw: str, taxonomy: Taxonomy) -> tuple[str, list[str]]:
    """상품명에서 프로모션 머리말을 떼어내고, 떼어낸 태그를 함께 돌려준다."""
    name = normalize_text(raw)
    tags: list[str] = []

    # 앞머리의 [단독], (방송최초) 같은 프로모션 브래킷을 최대 3개까지 벗긴다
    for _ in range(3):
        m = BRACKET_RE.match(name)
        if not m:
            break
        inner = m.group(1).strip()
        if _squash(inner) in {_squash(p) for p in taxonomy.noise_prefixes}:
            tags.append(inner)
            name = name[m.end():]
        else:
            break  # 브랜드일 수 있으니 남겨둔다

    name = TRAILING_NOISE_RE.sub("", name)
    return normalize_text(name), tags


def extract_brand_rule_based(product_name: str, taxonomy: Taxonomy) -> str:
    """표기 규칙만으로 브랜드를 뽑아본다. 확신이 없으면 빈 문자열."""
    name = normalize_text(product_name)

    # 1) 남아 있는 선두 브래킷은 브랜드로 본다  "[락앤락] 밀폐용기"
    m = BRACKET_RE.match(name)
    if m:
        cand = m.group(1).strip()
        if _is_plausible_brand(cand, taxonomy):
            return cand

    # 2) "브랜드 - 상품명" / "브랜드_상품명" 구분자
    for sep in (" - ", " – ", " / ", "_"):
        if sep in name:
            cand = name.split(sep, 1)[0].strip()
            if _is_plausible_brand(cand, taxonomy) and len(cand.split()) <= 2:
                return cand

    return ""


def _is_plausible_brand(cand: str, taxonomy: Taxonomy) -> bool:
    c = cand.strip()
    if not (2 <= len(c) <= 20):
        return False
    if _squash(c) in {_squash(w) for w in taxonomy.stopwords}:
        return False
    if _squash(c) in {_squash(w) for w in taxonomy.noise_prefixes}:
        return False
    if re.fullmatch(r"[\d\s,._%-]+", c):        # 숫자/기호뿐
        return False
    return True


def learn_brand_dictionary(product_names: list[str], taxonomy: Taxonomy,
                           min_support: int = 3) -> set[str]:
    """
    상품명 선두 토큰의 빈도로 브랜드 사전을 만든다.

    같은 선두 토큰(1~2어절)이 서로 다른 상품 min_support 건 이상에 등장하면
    브랜드로 인정한다. '서로 다른 상품'을 세는 게 핵심 — 같은 방송이 반복 편성돼
    같은 상품명이 100번 나와도 지지도는 1이다.
    """
    support: dict[str, set[str]] = defaultdict(set)

    for raw in product_names:
        name = normalize_text(raw)
        if not name:
            continue
        tokens = name.split()
        for n in (1, 2):
            if len(tokens) <= n:
                continue
            cand = " ".join(tokens[:n])
            if _is_plausible_brand(cand, taxonomy):
                support[cand].add(name)

    learned = {c for c, names in support.items() if len(names) >= min_support}

    # 2어절 후보가 채택됐는데 그 앞 1어절도 채택됐다면, 더 긴 쪽만 남긴다
    # 예) "락앤락" 과 "락앤락 클래식" 이 둘 다 있으면 "락앤락" 을 유지 (더 일반적인 브랜드)
    refined = set()
    for cand in learned:
        parts = cand.split()
        if len(parts) == 2 and parts[0] in learned:
            continue     # 앞 1어절이 이미 브랜드면 그걸로 충분
        refined.add(cand)
    return refined


def extract_brand(product_name: str, taxonomy: Taxonomy,
                  brand_dict: set[str] | None = None) -> str:
    """규칙 -> 사전 순으로 브랜드를 찾는다."""
    rule_hit = extract_brand_rule_based(product_name, taxonomy)
    if rule_hit:
        return rule_hit

    if brand_dict:
        name = normalize_text(product_name)
        tokens = name.split()
        # 긴 후보(2어절)를 먼저 본다
        for n in (2, 1):
            if len(tokens) >= n:
                cand = " ".join(tokens[:n])
                if cand in brand_dict:
                    return cand
    return ""


# --------------------------------------------------------------------------- #
# 방송시간 정규화
# --------------------------------------------------------------------------- #
EPOCH_RE = re.compile(r"^\d{10}$|^\d{13}$")
TIME_TOKEN_RE = re.compile(r"([01]?\d|2[0-3])\s*[:시]\s*([0-5]\d)")


def parse_air_time(raw: str, air_date: str) -> tuple[str, str]:
    """
    다양한 형태의 시간 표현을 (ISO datetime, HH:MM) 로 만든다.
    입력 예: '1755400800', '2026-08-17T10:00:00', '10:00', '오전 10시 00분'
    """
    s = normalize_text(raw)
    if not s:
        return "", ""

    # epoch (초/밀리초) -> KST
    if EPOCH_RE.match(s):
        ts = int(s)
        if len(s) == 13:
            ts //= 1000
        dt = datetime.fromtimestamp(ts, tz=KST_TZ).replace(tzinfo=None)
        return dt.isoformat(timespec="minutes"), dt.strftime("%H:%M")

    # ISO 계열
    iso_try = s.replace("Z", "").replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(iso_try[:len(datetime.now().strftime(fmt))], fmt)
            return dt.isoformat(timespec="minutes"), dt.strftime("%H:%M")
        except ValueError:
            continue

    # HHMM / HHMMSS 순수 숫자
    if re.fullmatch(r"\d{4}", s):
        hh, mm = int(s[:2]), int(s[2:])
        if hh < 24 and mm < 60:
            return f"{air_date}T{hh:02d}:{mm:02d}", f"{hh:02d}:{mm:02d}"

    # 'HH:MM' 또는 'HH시 MM분'
    m = TIME_TOKEN_RE.search(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if "오후" in s and hh < 12:
            hh += 12
        return f"{air_date}T{hh:02d}:{mm:02d}", f"{hh:02d}:{mm:02d}"

    return "", ""


# 모든 버킷은 정확히 3시간 폭이다.
# (폭이 다르면 히트맵에서 넓은 버킷만 진하게 나와 '그 시간대에 편성이 몰린다'는
#  착시를 만든다 — 실제로는 그냥 시간이 두 배였을 뿐이다.)
TIME_SLOTS = [
    (0, "심야 00-03"), (3, "새벽 03-06"), (6, "아침 06-09"), (9, "오전 09-12"),
    (12, "점심 12-15"), (15, "오후 15-18"), (18, "저녁 18-21"), (21, "밤 21-24"),
]


def time_slot(hhmm: str) -> str:
    """3시간 단위 시간대 버킷 — 시(hour) 단위보다 흐름이 읽힌다."""
    if not hhmm:
        return "미상"
    try:
        hour = int(hhmm.split(":")[0])
    except (ValueError, IndexError):
        return "미상"
    label = TIME_SLOTS[0][1]
    for start, name in TIME_SLOTS:
        if hour >= start:
            label = name
    return label


PRICE_DIGITS_RE = re.compile(r"[\d,]+")


def parse_price(raw: str) -> int | None:
    s = normalize_text(raw)
    if not s:
        return None
    m = PRICE_DIGITS_RE.search(s)
    if not m:
        return None
    try:
        val = int(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return val if 0 < val < 100_000_000 else None
