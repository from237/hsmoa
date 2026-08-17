"""
저장소 — SQLite 를 원본(source of truth)으로 두고 CSV 는 파생물로 뽑는다.

중복 처리
---------
같은 방송은 여러 번 수집된다(어제/오늘/내일을 매일 겹쳐서 긁으므로).
(방송일, 시작시각, 회사명, 상품명) 해시를 기본키로 잡아 UPSERT 하고,
first_seen_at 은 보존, last_seen_at 과 나머지 필드는 최신값으로 갱신한다.
-> 편성이 나중에 바뀌면 그 변화도 잡힌다.
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS broadcasts (
    id                TEXT PRIMARY KEY,
    air_date          TEXT NOT NULL,
    start_iso         TEXT,
    start_hhmm        TEXT,
    time_slot         TEXT,
    weekday           TEXT,
    channel           TEXT NOT NULL,
    channel_type      TEXT,
    channel_group     TEXT,
    brand             TEXT,
    product_name      TEXT NOT NULL,
    product_name_raw  TEXT,
    category          TEXT,
    source_category   TEXT,
    price             INTEGER,
    promo_tags        TEXT,
    url               TEXT,
    image             TEXT,
    source            TEXT,
    first_seen_at     TEXT,
    last_seen_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_air_date  ON broadcasts(air_date);
CREATE INDEX IF NOT EXISTS idx_channel   ON broadcasts(channel);
CREATE INDEX IF NOT EXISTS idx_category  ON broadcasts(category);
CREATE INDEX IF NOT EXISTS idx_brand     ON broadcasts(brand);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_at        TEXT PRIMARY KEY,
    air_dates     TEXT,
    method        TEXT,
    items_found   INTEGER,
    items_new     INTEGER,
    notes         TEXT
);
"""

CSV_COLUMNS = [
    "air_date", "weekday", "start_hhmm", "time_slot", "channel", "channel_type",
    "brand", "product_name", "category", "price", "url",
]

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def make_id(air_date: str, start_hhmm: str, channel: str, product_name: str) -> str:
    key = f"{air_date}|{start_hhmm}|{channel}|{product_name}".lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def weekday_of(air_date: str) -> str:
    try:
        return WEEKDAYS[datetime.strptime(air_date, "%Y-%m-%d").weekday()]
    except ValueError:
        return ""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- write #
    def upsert_many(self, rows: Iterable[dict]) -> tuple[int, int]:
        """(총 처리건수, 신규건수) 반환."""
        now = datetime.now(KST).isoformat(timespec="seconds")
        total = new = 0
        cur = self.conn.cursor()

        for r in rows:
            rid = make_id(r["air_date"], r.get("start_hhmm", ""),
                          r["channel"], r["product_name"])
            exists = cur.execute(
                "SELECT 1 FROM broadcasts WHERE id = ?", (rid,)).fetchone()
            if exists:
                cur.execute("""
                    UPDATE broadcasts SET
                        start_iso=?, start_hhmm=?, time_slot=?, weekday=?,
                        channel_type=?, channel_group=?, brand=?, product_name_raw=?,
                        category=?, source_category=?, price=?, promo_tags=?,
                        url=?, image=?, source=?, last_seen_at=?
                    WHERE id=?
                """, (
                    r.get("start_iso"), r.get("start_hhmm"), r.get("time_slot"),
                    weekday_of(r["air_date"]), r.get("channel_type"),
                    r.get("channel_group"), r.get("brand"), r.get("product_name_raw"),
                    r.get("category"), r.get("source_category"), r.get("price"),
                    r.get("promo_tags"), r.get("url"), r.get("image"),
                    r.get("source"), now, rid,
                ))
            else:
                cur.execute("""
                    INSERT INTO broadcasts (
                        id, air_date, start_iso, start_hhmm, time_slot, weekday,
                        channel, channel_type, channel_group, brand,
                        product_name, product_name_raw, category, source_category,
                        price, promo_tags, url, image, source,
                        first_seen_at, last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    rid, r["air_date"], r.get("start_iso"), r.get("start_hhmm"),
                    r.get("time_slot"), weekday_of(r["air_date"]),
                    r["channel"], r.get("channel_type"), r.get("channel_group"),
                    r.get("brand"), r["product_name"], r.get("product_name_raw"),
                    r.get("category"), r.get("source_category"), r.get("price"),
                    r.get("promo_tags"), r.get("url"), r.get("image"),
                    r.get("source"), now, now,
                ))
                new += 1
            total += 1

        self.conn.commit()
        return total, new

    def log_run(self, air_dates: list[str], method: str,
                found: int, new: int, notes: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO crawl_runs VALUES (?,?,?,?,?,?)",
            (datetime.now(KST).isoformat(timespec="seconds"),
             ",".join(air_dates), method, found, new, notes),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- read #
    def all_product_names(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT product_name_raw FROM broadcasts "
            "WHERE product_name_raw IS NOT NULL")]

    def fetch_all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM broadcasts ORDER BY air_date DESC, start_hhmm").fetchall()

    def rebrand_all(self, brand_fn) -> int:
        """브랜드 사전이 커진 뒤, 브랜드가 비어 있던 기존 행을 다시 채운다."""
        rows = self.conn.execute(
            "SELECT id, product_name_raw FROM broadcasts "
            "WHERE brand IS NULL OR brand = ''").fetchall()
        updated = 0
        for row in rows:
            brand = brand_fn(row["product_name_raw"] or "")
            if brand:
                self.conn.execute("UPDATE broadcasts SET brand=? WHERE id=?",
                                  (brand, row["id"]))
                updated += 1
        self.conn.commit()
        return updated

    def export_csv(self, csv_path: Path) -> int:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.fetch_all()
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in CSV_COLUMNS})
        return len(rows)

    def close(self):
        self.conn.close()
