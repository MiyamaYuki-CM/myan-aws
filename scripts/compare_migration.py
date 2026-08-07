#!/usr/bin/env python3
"""
移行元(SQLite)と移行先(PostgreSQL)のデータ整合性を突合するスクリプト（シナリオ6）。

各テーブルについて
  - 行数の一致
  - 主要列(数値・金額列)の合計値の一致
  - 先頭N件のサンプル行の値一致
を確認し、差分があれば標準出力に警告として出す。

このスクリプトが確認しているのは「全テーブルの行数」「指定した数値列の合計」
「各テーブルの先頭 SAMPLE_ROWS 行の値」の範囲であり、全レコード・全列の完全一致を
保証するものではない。本番移行では、主キー順の全件比較やレコードハッシュの突合など
より厳密な検証を別途行うことを推奨する。

前提: pip3 install psycopg2-binary 済み(CFnのUserDataでインストール済み)
"""
import argparse
import sqlite3
import sys
from decimal import ROUND_HALF_UP, Decimal

import psycopg2

# テーブルごとに突合する数値列(SUMで整合性確認する列)を指定
NUMERIC_CHECK_COLUMNS = {
    "vendors": [],
    "estimates": ["total_amount"],
    "estimate_items": ["amount"],
    "expense_categories": [],
    "expense_entries": ["amount"],
}

# 先頭何件をサンプル比較するか。あくまで簡易チェックであり、全件比較の代わりにはならない。
SAMPLE_ROWS = 5

MONEY_SCALE = Decimal("0.01")


def normalize_money(value):
    """金額値を業務上のスケール(小数2桁)へ揃える。

    psycopg2はnumeric列をDecimalで、sqlite3はREAL列をfloatで返す。
    floatへ揃えると再び浮動小数点誤差が入るため、Decimalへ寄せて比較する。
    """
    if value is None:
        return None
    return Decimal(str(value)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def get_sqlite_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn(dsn):
    return psycopg2.connect(dsn)


def normalize_value(value):
    """
    PostgreSQLのbyteaをmemoryviewで返すことへの対応。
    psycopg2がbyteaをmemoryviewオブジェクトで返す一方、sqlite3がBLOBをbytesで返すため、
    str()での文字列化時に異なる表現になる（バイナリ内容が同一でも）。
    この関数は値を正規化し、両DBでの比較が正確に行えるようにする。
    """
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def compare_table(sqlite_conn, pg_conn, table):
    print(f"\n=== {table} ===")
    sc = sqlite_conn.cursor()
    pc = pg_conn.cursor()
    ng_count = 0

    sc.execute(f"SELECT COUNT(*) FROM {table}")
    sqlite_count = sc.fetchone()[0]
    pc.execute(f"SELECT COUNT(*) FROM {table}")
    pg_count = pc.fetchone()[0]

    row_count_ok = sqlite_count == pg_count
    if not row_count_ok:
        ng_count += 1
    print(f"[{'OK' if row_count_ok else 'NG'}] row count: sqlite={sqlite_count} postgres={pg_count}")

    numeric_columns = NUMERIC_CHECK_COLUMNS.get(table, [])
    for col in numeric_columns:
        sc.execute(f"SELECT SUM({col}) FROM {table}")
        sqlite_sum = sc.fetchone()[0]
        pc.execute(f"SELECT SUM({col}) FROM {table}")
        pg_sum = pc.fetchone()[0]

        sqlite_norm = normalize_money(sqlite_sum)
        pg_norm = normalize_money(pg_sum)

        if sqlite_norm is None and pg_norm is None:
            sum_ok = True
            diff = None
        elif sqlite_norm is None or pg_norm is None:
            sum_ok = False
            diff = None
        else:
            sum_ok = sqlite_norm == pg_norm
            diff = sqlite_norm - pg_norm

        if not sum_ok:
            ng_count += 1
        print(f"[{'OK' if sum_ok else 'NG'}] sum({col}): sqlite={sqlite_norm} postgres={pg_norm} diff={diff}")

    sc.execute(f"SELECT * FROM {table} ORDER BY id LIMIT {SAMPLE_ROWS}")
    sample_sqlite = [dict(r) for r in sc.fetchall()]
    pc.execute(f"SELECT * FROM {table} ORDER BY id LIMIT {SAMPLE_ROWS}")
    pg_cols = [d.name for d in pc.description]
    sample_pg = [dict(zip(pg_cols, r)) for r in pc.fetchall()]

    for s_row, p_row in zip(sample_sqlite, sample_pg):
        common_keys = set(s_row.keys()) & set(p_row.keys())
        mismatches = {}
        for k in common_keys:
            if k in numeric_columns:
                if normalize_money(s_row[k]) != normalize_money(p_row[k]):
                    mismatches[k] = (s_row[k], p_row[k])
            elif str(normalize_value(s_row[k])) != str(normalize_value(p_row[k])):
                mismatches[k] = (s_row[k], p_row[k])
        if mismatches:
            ng_count += 1
            print(f"  [NG] id={s_row.get('id')} 値の差分: {mismatches}")
    print(f"  (先頭{SAMPLE_ROWS}件のサンプル比較 完了)")

    return ng_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="SQLiteファイルパス")
    parser.add_argument("--pg", required=True, help="PostgreSQL DSN (postgresql://user:pass@host:5432/dbname)")
    args = parser.parse_args()

    sqlite_conn = get_sqlite_conn(args.sqlite)
    pg_conn = get_pg_conn(args.pg)

    total_ng = 0
    for table in NUMERIC_CHECK_COLUMNS:
        total_ng += compare_table(sqlite_conn, pg_conn, table)

    sqlite_conn.close()
    pg_conn.close()

    if total_ng > 0:
        print(f"\n[NG] 差分が {total_ng} 件見つかりました。")
        sys.exit(1)


if __name__ == "__main__":
    main()
