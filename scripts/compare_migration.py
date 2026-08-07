#!/usr/bin/env python3
"""
移行元(SQLite)と移行先(PostgreSQL)のデータ整合性を突合するスクリプト（シナリオ6）。

各テーブルについて
  - 行数の一致
  - 主要列(数値・金額列)の合計値の一致
  - 先頭N件のサンプル行の値一致
を確認し、差分があれば標準出力に警告として出す。

前提: pip3 install psycopg2-binary 済み(CFnのUserDataでインストール済み)
"""
import argparse
import sqlite3

import psycopg2

# テーブルごとに突合する数値列(SUMで整合性確認する列)を指定
NUMERIC_CHECK_COLUMNS = {
    "vendors": [],
    "estimates": ["total_amount"],
    "estimate_items": ["amount"],
    "expense_categories": [],
    "expense_entries": ["amount"],
}

SAMPLE_ROWS = 5


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

    sc.execute(f"SELECT COUNT(*) FROM {table}")
    sqlite_count = sc.fetchone()[0]
    pc.execute(f"SELECT COUNT(*) FROM {table}")
    pg_count = pc.fetchone()[0]

    status = "OK" if sqlite_count == pg_count else "NG"
    print(f"[{status}] row count: sqlite={sqlite_count} postgres={pg_count}")

    for col in NUMERIC_CHECK_COLUMNS.get(table, []):
        sc.execute(f"SELECT SUM({col}) FROM {table}")
        sqlite_sum = sc.fetchone()[0]
        pc.execute(f"SELECT SUM({col}) FROM {table}")
        pg_sum = pc.fetchone()[0]
        diff = None
        if sqlite_sum is not None and pg_sum is not None:
            diff = abs(float(sqlite_sum) - float(pg_sum))
        status = "OK" if (diff is not None and diff < 0.01) else "NG"
        print(f"[{status}] sum({col}): sqlite={sqlite_sum} postgres={pg_sum} diff={diff}")

    sc.execute(f"SELECT * FROM {table} ORDER BY id LIMIT {SAMPLE_ROWS}")
    sample_sqlite = [dict(r) for r in sc.fetchall()]
    pc.execute(f"SELECT * FROM {table} ORDER BY id LIMIT {SAMPLE_ROWS}")
    pg_cols = [d.name for d in pc.description]
    sample_pg = [dict(zip(pg_cols, r)) for r in pc.fetchall()]

    for s_row, p_row in zip(sample_sqlite, sample_pg):
        common_keys = set(s_row.keys()) & set(p_row.keys())
        mismatches = {
            k: (s_row[k], p_row[k])
            for k in common_keys
            if str(normalize_value(s_row[k])) != str(normalize_value(p_row[k]))
        }
        if mismatches:
            print(f"  [NG] id={s_row.get('id')} 値の差分: {mismatches}")
    print(f"  (先頭{SAMPLE_ROWS}件のサンプル比較 完了)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="SQLiteファイルパス")
    parser.add_argument("--pg", required=True, help="PostgreSQL DSN (postgresql://user:pass@host:5432/dbname)")
    args = parser.parse_args()

    sqlite_conn = get_sqlite_conn(args.sqlite)
    pg_conn = get_pg_conn(args.pg)

    for table in NUMERIC_CHECK_COLUMNS:
        compare_table(sqlite_conn, pg_conn, table)

    sqlite_conn.close()
    pg_conn.close()


if __name__ == "__main__":
    main()
