#!/usr/bin/env python3
"""
実データ未取得のための代替サンプルデータ生成スクリプト。
「工事見積書OCR→経費仕訳自動分類→会計システム連携」アプリを想定した仮想スキーマに
サンプル行を投入したSQLite DBファイルを作成する。
実データ入手後は本スクリプトを使わず、実際の .db ファイルをそのまま検証対象とすること。

なお total_amount 等の金額列はREALで保持しているが、これはSQLite→PostgreSQL移行時の
型変換（REAL→numeric）の挙動を検証する目的であり、実システムでREALを金額の型として
推奨する意図はない（丸め誤差の観点からnumeric/decimal相当の型を使うべき）。
"""
import argparse
import random
import sqlite3
import string
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL  -- ISO8601文字列
);

CREATE TABLE estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL,  -- あえてREFERENCESを書いていない(移行後にFKを追加するシナリオを検証するため)
    estimate_no TEXT NOT NULL,
    estimate_date TEXT NOT NULL,      -- ISO8601文字列 (例: '2026-07-15')
    total_amount REAL NOT NULL,  -- 型変換検証用にREALで保持(実システムでのREAL採用を推奨するものではない)
    ocr_confidence REAL,
    is_reviewed INTEGER NOT NULL DEFAULT 0,  -- 真偽値をINTEGER 0/1で表現
    created_at_epoch INTEGER NOT NULL,       -- UNIXエポック秒
    thumbnail BLOB
);

CREATE TABLE estimate_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    amount REAL NOT NULL
);

CREATE TABLE expense_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE expense_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    is_posted INTEGER NOT NULL DEFAULT 0,
    posted_at TEXT,              -- NULL許容(未仕訳の場合)
    accounting_system_ref TEXT   -- 会計システム側の伝票番号。未連携時はNULL
);
"""

VENDOR_NAMES = ["株式会社クマさん", "うさぎさん工務店", "第一カメさん工業", "うなぎさん電気工事",
                "株式会社ツチノコさん土木", "カブトムシさん建装", "ミツバチさんリフォーム", "ロバさん配管サービス"]
ITEM_NAMES = ["コンクリート打設工事", "型枠工事", "鉄筋加工組立", "内装クロス張替",
              "電気配線工事", "給排水設備工事", "外壁塗装", "足場仮設", "解体撤去工事",
              "防水工事（ウレタン系）"]
EXPENSE_CATEGORIES = [
    ("4001", "材料費"), ("4002", "外注費"), ("4003", "労務費"),
    ("4004", "経費（交通費）"), ("4005", "経費（諸経費）"),
]


def random_jp_text(base):
    suffix = "".join(random.choices(string.digits, k=3))
    return f"{base}（No.{suffix}）"


def build(conn, rows):
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    for name in VENDOR_NAMES:
        cur.execute(
            "INSERT INTO vendors (name, created_at) VALUES (?, ?)",
            (name, (datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=random.randint(0, 500))).isoformat()),
        )

    for code, name in EXPENSE_CATEGORIES:
        cur.execute(
            "INSERT INTO expense_categories (code, name) VALUES (?, ?)", (code, name)
        )

    vendor_ids = [r[0] for r in cur.execute("SELECT id FROM vendors")]
    category_ids = [r[0] for r in cur.execute("SELECT id FROM expense_categories")]

    base_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(rows):
        vendor_id = random.choice(vendor_ids)
        est_date = base_date + timedelta(days=random.randint(0, 200))
        total_amount = 0.0
        cur.execute(
            """INSERT INTO estimates
               (vendor_id, estimate_no, estimate_date, total_amount, ocr_confidence,
                is_reviewed, created_at_epoch, thumbnail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vendor_id,
                f"EST-{2026}{i:06d}",
                est_date.date().isoformat(),
                total_amount,  # 後で明細合計にUPDATE
                round(random.uniform(0.75, 0.99), 4),
                random.choice([0, 1]),
                int(est_date.timestamp()),  # UTC固定(実行環境のローカルTZに依存させない)
                bytes([random.randint(0, 255) for _ in range(32)]),  # ダミーサムネイル
            ),
        )
        estimate_id = cur.lastrowid

        item_count = random.randint(1, 5)
        total = 0.0
        for _ in range(item_count):
            qty = round(random.uniform(1, 50), 2)
            unit_price = random.choice([1500, 3200, 8800, 12000, 25000, 48000])
            amount = round(qty * unit_price, 2)
            total += amount
            cur.execute(
                """INSERT INTO estimate_items
                   (estimate_id, item_name, quantity, unit_price, amount)
                   VALUES (?, ?, ?, ?, ?)""",
                (estimate_id, random_jp_text(random.choice(ITEM_NAMES)), qty, unit_price, amount),
            )

        cur.execute(
            "UPDATE estimates SET total_amount = ? WHERE id = ?", (round(total, 2), estimate_id)
        )

        # 経費仕訳: 一部は未仕訳(NULL)のまま残す(is_posted=0 かつ posted_at IS NULL)
        posted = random.random() < 0.8
        cur.execute(
            """INSERT INTO expense_entries
               (estimate_id, category_id, amount, is_posted, posted_at, accounting_system_ref)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                estimate_id,
                random.choice(category_ids),
                round(total, 2),
                1 if posted else 0,
                (est_date + timedelta(days=random.randint(1, 10))).isoformat() if posted else None,
                f"JE-{random.randint(100000, 999999)}" if posted else None,
            ),
        )

    conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5000, help="生成する見積件数")
    parser.add_argument("--out", default="sample.db", help="出力するSQLiteファイルパス")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    conn = sqlite3.connect(args.out)
    build(conn, args.rows)
    conn.close()
    print(f"Generated {args.out} with {args.rows} estimates.")


if __name__ == "__main__":
    main()
