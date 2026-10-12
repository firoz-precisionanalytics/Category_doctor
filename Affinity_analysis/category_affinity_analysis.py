"""Category-level market-basket analysis for sales vouchers.

Each voucher becomes a basket of distinct product categories. Run a small
validation first:

    python Affinity_analysis/category_affinity_analysis.py --sample-rows 100
"""

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd

from db_connection import get_connection


# Keep these aligned with affinity_analysis.py.
SALES_DATABASES = ("DMCHM2526",)
ITEM_MASTER_DATABASE = "DMCHM2627"
TRANSACTION_TABLE = "datatransfersales"
ITEM_MASTER_TABLE = "datatransferItem_codes"

MIN_PAIR_TRANSACTIONS = 5
MIN_SUPPORT = 0.0005
MIN_CONFIDENCE = 0.10
OUTPUT_FILE = Path(__file__).with_name("category_affinity_rules_full.csv")


def quote_identifier(identifier: str) -> str:
    return "[{}]".format(identifier.replace("]", "]]"))


def create_category_baskets(connection, max_sales_rows: Optional[int]) -> None:
    """Create an indexed, de-duplicated category basket table on this connection."""
    databases = SALES_DATABASES[:1] if max_sales_rows else SALES_DATABASES
    top = "TOP ({}) ".format(max_sales_rows) if max_sales_rows else ""
    sources = " UNION ALL ".join(
        """SELECT {top}'{database}' AS source_database, voucher_id, item_code
             FROM {database_name}.dbo.{sales_table}
             WHERE voucher_id IS NOT NULL AND item_code IS NOT NULL AND issue_quantity > 0""".format(
            top=top,
            database=database,
            database_name=quote_identifier(database),
            sales_table=quote_identifier(TRANSACTION_TABLE),
        )
        for database in databases
    )
    cursor = connection.cursor()
    cursor.execute(
        """SELECT sales.source_database, sales.voucher_id,
                  LTRIM(RTRIM(CAST(master.category_name AS varchar(255)))) AS category_name
             INTO #CategoryBaskets
             FROM ({sources}) AS sales
             INNER JOIN {master_database}.dbo.{master_table} AS master
                ON master.item_code = sales.item_code
             WHERE NULLIF(LTRIM(RTRIM(CAST(master.category_name AS varchar(255)))), '') IS NOT NULL
             GROUP BY sales.source_database, sales.voucher_id,
                      LTRIM(RTRIM(CAST(master.category_name AS varchar(255))));""".format(
            sources=sources,
            master_database=quote_identifier(ITEM_MASTER_DATABASE),
            master_table=quote_identifier(ITEM_MASTER_TABLE),
        )
    )
    while cursor.nextset():
        pass
    cursor.execute(
        "CREATE UNIQUE CLUSTERED INDEX IX_CategoryBaskets "
        "ON #CategoryBaskets (source_database, voucher_id, category_name);"
    )
    cursor.close()


def build_query() -> str:
    return """
    WITH TotalTransactions AS (
        SELECT COUNT_BIG(*) AS transaction_count
        FROM (SELECT source_database, voucher_id FROM #CategoryBaskets
              GROUP BY source_database, voucher_id) AS transactions
    ),
    CategoryCounts AS (
        SELECT category_name, COUNT_BIG(*) AS category_transaction_count
        FROM #CategoryBaskets GROUP BY category_name
    ),
    PairCounts AS (
        SELECT left_category.category_name AS category_a,
               right_category.category_name AS category_b,
               COUNT_BIG(*) AS pair_transaction_count
        FROM #CategoryBaskets AS left_category
        INNER JOIN #CategoryBaskets AS right_category
          ON right_category.source_database = left_category.source_database
         AND right_category.voucher_id = left_category.voucher_id
         AND right_category.category_name > left_category.category_name
        GROUP BY left_category.category_name, right_category.category_name
    ),
    Metrics AS (
        SELECT pairs.category_a, pairs.category_b, pairs.pair_transaction_count,
               total.transaction_count,
               a.category_transaction_count AS category_a_transaction_count,
               b.category_transaction_count AS category_b_transaction_count,
               CAST(pairs.pair_transaction_count * 1.0 / total.transaction_count AS decimal(12, 6)) AS support,
               CAST(pairs.pair_transaction_count * 1.0 / a.category_transaction_count AS decimal(12, 6)) AS confidence_a_to_b,
               CAST(pairs.pair_transaction_count * 1.0 / b.category_transaction_count AS decimal(12, 6)) AS confidence_b_to_a,
               CAST((pairs.pair_transaction_count * 1.0 * total.transaction_count) /
                    (a.category_transaction_count * 1.0 * b.category_transaction_count) AS decimal(12, 6)) AS lift
        FROM PairCounts AS pairs
        CROSS JOIN TotalTransactions AS total
        INNER JOIN CategoryCounts AS a ON a.category_name = pairs.category_a
        INNER JOIN CategoryCounts AS b ON b.category_name = pairs.category_b
        WHERE pairs.pair_transaction_count >= ?
          AND pairs.pair_transaction_count * 1.0 / total.transaction_count >= ?
    )
    SELECT category_a AS antecedent_category, category_b AS consequent_category,
           pair_transaction_count,
           category_a_transaction_count AS antecedent_transaction_count,
           category_b_transaction_count AS consequent_transaction_count,
           transaction_count AS total_transactions, support,
           confidence_a_to_b AS confidence, lift
    FROM Metrics WHERE confidence_a_to_b >= ?
    UNION ALL
    SELECT category_b, category_a, pair_transaction_count,
           category_b_transaction_count, category_a_transaction_count,
           transaction_count, support, confidence_b_to_a, lift
    FROM Metrics WHERE confidence_b_to_a >= ?
    OPTION (RECOMPILE, MAXDOP 1, MAX_GRANT_PERCENT = 10);
    """


def main() -> None:
    parser = argparse.ArgumentParser(description="Create category affinity rules.")
    parser.add_argument("--sample-rows", type=int, help="Limit source sales rows for validation.")
    args = parser.parse_args()
    if args.sample_rows is not None and args.sample_rows < 1:
        parser.error("--sample-rows must be at least 1")

    with get_connection() as connection:
        create_category_baskets(connection, args.sample_rows)
        rules = pd.read_sql_query(
            build_query(), connection,
            params=[MIN_PAIR_TRANSACTIONS, MIN_SUPPORT, MIN_CONFIDENCE, MIN_CONFIDENCE],
        )
    rules = rules.sort_values(["lift", "confidence", "pair_transaction_count"], ascending=[False, False, False])
    output = OUTPUT_FILE.with_name("category_affinity_rules_sample.csv") if args.sample_rows else OUTPUT_FILE
    rules.to_csv(output, index=False)
    print("Created {} with {:,} category rules.".format(output, len(rules)))


if __name__ == "__main__":
    main()
