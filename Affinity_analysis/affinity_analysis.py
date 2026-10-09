"""Market-basket analysis for sales vouchers.

Each unique (source database, voucher_id) is treated as a transaction.  The
output contains directional rules: ``item_a -> item_b`` and ``item_b ->
item_a``.  This makes confidence meaningful for the item on the left-hand
side of the rule.

Review the settings below before running this file.  Run it with:

    python Affinity_analysis/affinity_analysis.py
"""

import argparse
from pathlib import Path

import pandas as pd

from db_connection import get_connection


# Sales-history databases included in the analysis, newest first.
# SALES_DATABASES = ("DMCHM2627", "DMCHM2526", "DMCHM2425")
SALES_DATABASES = ("DMCHM2526",)
ITEM_MASTER_DATABASE = "DMCHM2627"
TRANSACTION_TABLE = "datatransfersales"
ITEM_MASTER_TABLE = "datatransferItem_codes"

# Rules must meet every threshold below.  Tune these after reviewing the
# first extract: lower thresholds yield more rules, but can be noisier.
MIN_PAIR_TRANSACTIONS = 5
MIN_SUPPORT = 0.01
MIN_CONFIDENCE = 0.10

OUTPUT_FILE = Path(__file__).with_name("affinity_rules_full.csv")


def quote_identifier(identifier: str) -> str:
    """Return a SQL Server identifier quoted safely for a generated query."""
    return f"[{identifier.replace(']', ']]')}]"


def find_item_description_column(connection) -> str | None:
    """Find a human-readable item-master column, if the master has one."""
    candidates = (
        "item_name",
        "item_description",
        "item_desc",
        "description",
        "itemname",
    )
    columns = pd.read_sql_query(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ?
        """,
        connection,
        params=[ITEM_MASTER_TABLE],
    )["COLUMN_NAME"]

    available = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate in available:
            return available[candidate]
    return None


def build_query(description_column: str | None, max_sales_rows: int | None = None) -> str:
    """Build SQL that aggregates item pairs without loading raw sales lines."""
    source_databases = SALES_DATABASES[:1] if max_sales_rows else SALES_DATABASES
    row_limit = f"TOP ({max_sales_rows}) " if max_sales_rows else ""
    sales_sources = "\n        UNION ALL\n        ".join(
        f"""SELECT {row_limit}'{database}' AS source_database,
                   voucher_id,
                   LTRIM(RTRIM(CAST(item_code AS varchar(100)))) AS item_code
            FROM {quote_identifier(database)}.dbo.{quote_identifier(TRANSACTION_TABLE)}
            WHERE voucher_id IS NOT NULL
              AND item_code IS NOT NULL
              AND issue_quantity > 0"""
        for database in source_databases
    )

    if description_column:
        item_label_expression = (
            f"COALESCE(NULLIF(LTRIM(RTRIM(CAST({quote_identifier(description_column)} "
            "AS varchar(255)))), ''), item_code)"
        )
    else:
        item_label_expression = "item_code"

    sample_transactions = """SELECT source_database, voucher_id
        FROM RawSalesLines
        GROUP BY source_database, voucher_id"""

    return f"""
    WITH RawSalesLines AS (
        {sales_sources}
    ),
    SampleTransactions AS (
        {sample_transactions}
    ),
    BasketItems AS (
        SELECT sales_lines.source_database, sales_lines.voucher_id, sales_lines.item_code
        FROM RawSalesLines AS sales_lines
        INNER JOIN SampleTransactions AS sampled_transactions
            ON sampled_transactions.source_database = sales_lines.source_database
           AND sampled_transactions.voucher_id = sales_lines.voucher_id
        GROUP BY sales_lines.source_database, sales_lines.voucher_id, sales_lines.item_code
    ),
    TotalTransactions AS (
        SELECT COUNT(*) AS transaction_count
        FROM (
            SELECT source_database, voucher_id
            FROM BasketItems
            GROUP BY source_database, voucher_id
        ) AS transactions
    ),
    ItemCounts AS (
        SELECT item_code, COUNT(*) AS item_transaction_count
        FROM BasketItems
        GROUP BY item_code
    ),
    PairCounts AS (
        SELECT left_item.item_code AS item_a_code,
               right_item.item_code AS item_b_code,
               COUNT(*) AS pair_transaction_count
        FROM BasketItems AS left_item
        INNER JOIN BasketItems AS right_item
            ON right_item.source_database = left_item.source_database
           AND right_item.voucher_id = left_item.voucher_id
           AND right_item.item_code > left_item.item_code
        GROUP BY left_item.item_code, right_item.item_code
    ),
    ItemMaster AS (
        SELECT item_code,
               MAX(item_label) AS item_label,
               MAX(deptname) AS deptname,
               MAX(category_name) AS category_name
        FROM (
            SELECT LTRIM(RTRIM(CAST(item_code AS varchar(100)))) AS item_code,
                   {item_label_expression} AS item_label,
                   NULLIF(LTRIM(RTRIM(CAST(DEPTNAME AS varchar(255)))), '') AS deptname,
                   NULLIF(LTRIM(RTRIM(CAST(category_name AS varchar(255)))), '') AS category_name
            FROM {quote_identifier(ITEM_MASTER_DATABASE)}.dbo.{quote_identifier(ITEM_MASTER_TABLE)}
            WHERE item_code IS NOT NULL
        ) AS master_rows
        GROUP BY item_code
    ),
    PairMetrics AS (
        SELECT pairs.item_a_code,
               pairs.item_b_code,
               pairs.pair_transaction_count,
               total.transaction_count,
               item_a.item_transaction_count AS item_a_transaction_count,
               item_b.item_transaction_count AS item_b_transaction_count,
               CAST(pairs.pair_transaction_count * 1.0 / total.transaction_count AS decimal(12, 6)) AS support,
               CAST(pairs.pair_transaction_count * 1.0 / item_a.item_transaction_count AS decimal(12, 6)) AS confidence_a_to_b,
               CAST(pairs.pair_transaction_count * 1.0 / item_b.item_transaction_count AS decimal(12, 6)) AS confidence_b_to_a,
               CAST((pairs.pair_transaction_count * 1.0 * total.transaction_count) /
                    (item_a.item_transaction_count * 1.0 * item_b.item_transaction_count) AS decimal(12, 6)) AS lift
        FROM PairCounts AS pairs
        CROSS JOIN TotalTransactions AS total
        INNER JOIN ItemCounts AS item_a ON item_a.item_code = pairs.item_a_code
        INNER JOIN ItemCounts AS item_b ON item_b.item_code = pairs.item_b_code
        WHERE pairs.pair_transaction_count >= ?
          AND pairs.pair_transaction_count * 1.0 / total.transaction_count >= ?
    )
    SELECT metrics.item_a_code AS antecedent_code,
           COALESCE(item_a.item_label, metrics.item_a_code) AS antecedent,
           item_a.deptname AS antecedent_deptname,
           item_a.category_name AS antecedent_category_name,
           metrics.item_b_code AS consequent_code,
           COALESCE(item_b.item_label, metrics.item_b_code) AS consequent,
           item_b.deptname AS consequent_deptname,
           item_b.category_name AS consequent_category_name,
           metrics.pair_transaction_count,
           metrics.item_a_transaction_count AS antecedent_transaction_count,
           metrics.item_b_transaction_count AS consequent_transaction_count,
           metrics.transaction_count AS total_transactions,
           metrics.support,
           metrics.confidence_a_to_b AS confidence,
           metrics.lift
    FROM PairMetrics AS metrics
    LEFT JOIN ItemMaster AS item_a ON item_a.item_code = metrics.item_a_code
    LEFT JOIN ItemMaster AS item_b ON item_b.item_code = metrics.item_b_code
    WHERE metrics.confidence_a_to_b >= ?

    UNION ALL

    SELECT metrics.item_b_code AS antecedent_code,
           COALESCE(item_b.item_label, metrics.item_b_code) AS antecedent,
           item_b.deptname AS antecedent_deptname,
           item_b.category_name AS antecedent_category_name,
           metrics.item_a_code AS consequent_code,
           COALESCE(item_a.item_label, metrics.item_a_code) AS consequent,
           item_a.deptname AS consequent_deptname,
           item_a.category_name AS consequent_category_name,
           metrics.pair_transaction_count,
           metrics.item_b_transaction_count AS antecedent_transaction_count,
           metrics.item_a_transaction_count AS consequent_transaction_count,
           metrics.transaction_count AS total_transactions,
           metrics.support,
           metrics.confidence_b_to_a AS confidence,
           metrics.lift
    FROM PairMetrics AS metrics
    LEFT JOIN ItemMaster AS item_a ON item_a.item_code = metrics.item_a_code
    LEFT JOIN ItemMaster AS item_b ON item_b.item_code = metrics.item_b_code
    WHERE metrics.confidence_b_to_a >= ?
    """


def main() -> None:
    parser = argparse.ArgumentParser(description="Create item affinity rules from sales vouchers.")
    parser.add_argument(
        "--sample-rows",
        type=int,
        help="Read only this many sales lines from the newest database; useful for validation.",
    )
    args = parser.parse_args()
    if args.sample_rows is not None and args.sample_rows < 1:
        parser.error("--sample-rows must be at least 1")

    with get_connection() as connection:
        description_column = find_item_description_column(connection)
        query = build_query(description_column, args.sample_rows)
        rules = pd.read_sql_query(
            query,
            connection,
            params=[MIN_PAIR_TRANSACTIONS, MIN_SUPPORT, MIN_CONFIDENCE, MIN_CONFIDENCE],
        )

    rules = rules.sort_values(
        ["lift", "confidence", "pair_transaction_count"],
        ascending=[False, False, False],
    )
    output_file = (
        OUTPUT_FILE.with_name("affinity_rules_sample.csv")
        if args.sample_rows
        else OUTPUT_FILE
    )
    rules.to_csv(output_file, index=False)

    label_note = description_column or "item_code (no description column found)"
    print(f"Created {output_file} with {len(rules):,} affinity rules.")
    print(f"Item labels use: {label_note}")
    if not rules.empty:
        print(rules.head(20).to_string(index=False))
    else:
        print(
            "No rules met the current thresholds. "
            "Try a larger sample or lower MIN_PAIR_TRANSACTIONS, MIN_SUPPORT, "
            "or MIN_CONFIDENCE."
        )


if __name__ == "__main__":
    main()
