r"""Calculate hierarchy-level standard ASP per 100g from SKU pack mapping.

The mapping CSV must contain one row per item name, a pack size in grams, and
any user-defined hierarchy columns (brand, category, sub-category, etc.). The
latest item master supplies item names, which are matched to the mapping before
the selected sales period is aggregated.

Example:
    python Affinity_analysis/standard_asp_analysis.py mapping.csv \
        --level AI_Brand --reference-column category_name --reference-value TEA \
        --sales-value-column pamount --pack-size-column AI_pack_size \
        --filter-column category_name --filter-value TEA --exclude-invalid-pack-sizes

PowerShell example (uncomment and adapt when running from the project root):
    python .\Affinity_analysis\standard_asp_analysis.py .\Affinity_analysis\mapping.csv 
      --level ITEM_NAME 
      --reference-column AI_Brand 
      --sales-value-column pamount 
      --pack-size-column AI_pack_size 
      --filter-column category_name 
      --filter-value TEA 
      --exclude-invalid-pack-sizes
"""

import argparse
from pathlib import Path

import pandas as pd

from db_connection import get_connection
from mapping_utils import find_item_name_column, normalize_item_names, quote_identifier


SALES_DATABASES = ("DMCHM2526",)
ITEM_MASTER_DATABASE = "DMCHM2627"
TRANSACTION_TABLE = "datatransfersales"
DEFAULT_OUTPUT = Path(__file__).with_name("standard_asp_by_level.csv")


def load_sales(mapping: pd.DataFrame, name_column: str, value_column: str,
               quantity_column: str) -> pd.DataFrame:
    """Load sales using item names from the latest item master."""
    actual_name_column = {column.casefold(): column for column in mapping.columns}.get(name_column.casefold())
    if actual_name_column is None:
        raise ValueError("Mapping is missing item-name column {!r}.".format(name_column))
    item_names = mapping[actual_name_column].dropna().astype(str).str.strip().unique().tolist()
    if not item_names:
        raise ValueError("The mapping contains no non-empty item names.")
    if len(SALES_DATABASES) != 1:
        raise ValueError("This extract currently expects one sales database.")
    database = SALES_DATABASES[0]
    selects, params = [], []
    chunk_size = 1000
    with get_connection() as connection:
        master_name_column = find_item_name_column(connection, ITEM_MASTER_DATABASE)
        master_name = "master.{}".format(quote_identifier(master_name_column))
        for start in range(0, len(item_names), chunk_size):
            names = item_names[start:start + chunk_size]
            selects.append(
                """SELECT LTRIM(RTRIM(CAST({master_name} AS varchar(255)))) AS item_name,
                          CAST(sales.{value} AS float) AS sales_value,
                          CAST(sales.{quantity} AS float) AS quantity
                   FROM {database}.dbo.{sales_table} AS sales
                   INNER JOIN {master_database}.dbo.{master_table} AS master
                       ON master.item_code = sales.item_code
                   WHERE sales.item_code IS NOT NULL AND sales.issue_quantity > 0
                     AND UPPER(LTRIM(RTRIM(CAST({master_name} AS varchar(255)))))
                         IN ({placeholders})""".format(
                    master_name=master_name,
                    value=quote_identifier(value_column),
                    quantity=quote_identifier(quantity_column),
                    database=quote_identifier(database),
                    master_database=quote_identifier(ITEM_MASTER_DATABASE),
                    sales_table=quote_identifier(TRANSACTION_TABLE),
                    master_table=quote_identifier("datatransferItem_codes"),
                    placeholders=", ".join("?" for _ in names),
                )
            )
            params.extend(name.upper() for name in names)
        query = " UNION ALL ".join(selects)
        result = pd.read_sql_query(query, connection, params=params)
    result["item_name_key"] = result["item_name"].astype("string").str.strip().str.casefold()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate standard ASP per 100g for a user-defined hierarchy."
    )
    parser.add_argument("mapping_file", type=Path, help="CSV mapping with item name, pack size and hierarchy columns")
    parser.add_argument("--mapping-name-column", default="ITEM_NAME",
                        help="Mapping item-name column used to join the item master (default: ITEM_NAME)")
    parser.add_argument("--pack-size-column", default="pack_size_g", help="Mapping pack size column, measured in grams (default: pack_size_g)")
    parser.add_argument("--sales-value-column", default="pamount", help="Transaction table column containing line sales value (default: pamount)")
    parser.add_argument("--quantity-column", default="issue_quantity", help="Transaction table quantity column (default: issue_quantity)")
    parser.add_argument("--level", required=True, help="Mapping hierarchy column to aggregate (e.g. brand, category)")
    parser.add_argument("--reference-column", required=True, nargs="+",
                        help="One or more mapping columns defining the ASP benchmark group")
    parser.add_argument("--reference-value", nargs="+",
                        help="Optional benchmark values, in reference-column order; omit to use each group's total")
    parser.add_argument("--filter-column", help="Optional mapping column to restrict the input (e.g. category_name)")
    parser.add_argument("--filter-value", nargs="+",
                        help="One or more values to select in --filter-column")
    parser.add_argument("--exclude-invalid-pack-sizes", action="store_true",
                        help="Skip rows with missing or non-positive pack sizes instead of failing")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output CSV path")
    args = parser.parse_args()

    mapping = pd.read_csv(args.mapping_file, dtype={args.mapping_name_column: str})
    reference_columns = args.reference_column
    required = [args.pack_size_column, args.level, *reference_columns, args.mapping_name_column]
    missing = [column for column in required if column not in mapping.columns]
    if missing:
        parser.error("mapping file is missing columns: {}".format(", ".join(missing)))

    mapping = mapping.copy()
    if bool(args.filter_column) != bool(args.filter_value):
        parser.error("--filter-column and --filter-value must be provided together")
    if args.filter_column:
        if args.filter_column not in mapping.columns:
            parser.error("mapping file is missing filter column {!r}".format(args.filter_column))
        requested_values = {value.strip().casefold() for value in args.filter_value}
        mapping = mapping[mapping[args.filter_column].astype(str).str.strip().str.casefold()
                            .isin(requested_values)].copy()
        if mapping.empty:
            parser.error("no mapping rows match the requested filter")
    try:
        mapping = normalize_item_names(mapping, args.mapping_name_column)
    except ValueError as error:
        parser.error(str(error))
    mapping[args.pack_size_column] = pd.to_numeric(mapping[args.pack_size_column], errors="coerce")
    invalid_pack = mapping[args.pack_size_column].isna() | (mapping[args.pack_size_column] <= 0)
    if invalid_pack.any():
        if args.exclude_invalid_pack_sizes:
            print("Excluding {:,} rows with invalid pack sizes.".format(int(invalid_pack.sum())))
            mapping = mapping.loc[~invalid_pack].copy()
        else:
            parser.error("pack sizes must be positive gram values; invalid rows: {}".format(int(invalid_pack.sum())))
    if mapping.empty:
        parser.error("no mapping rows remain after filtering invalid pack sizes")

    sales = load_sales(mapping, args.mapping_name_column, args.sales_value_column, args.quantity_column)
    rows = sales.merge(mapping, on="item_name_key", how="inner", validate="many_to_one")
    rows = rows.dropna(subset=["sales_value", "quantity", args.level, *reference_columns])
    rows = rows[rows["quantity"] > 0]
    rows["grams_sold"] = rows["quantity"] * rows[args.pack_size_column]
    rows["value_per_100g"] = rows["sales_value"] * 100 / rows["grams_sold"]

    group_columns = [args.level, *reference_columns]
    if args.filter_column and args.filter_column not in group_columns:
        group_columns.append(args.filter_column)
    result = (rows.groupby(group_columns, dropna=False)
              .agg(sales_value=("sales_value", "sum"),
                   quantity=("quantity", "sum"),
                   grams_sold=("grams_sold", "sum"))
              .reset_index())
    result["standard_asp_per_100g"] = result["sales_value"] * 100 / result["grams_sold"]

    if args.reference_value is not None:
        if len(args.reference_value) != len(reference_columns):
            parser.error("--reference-value must provide one value per --reference-column")
        benchmark_rows = rows
        for column, value in zip(reference_columns, args.reference_value):
            benchmark_rows = benchmark_rows[
                benchmark_rows[column].astype(str).str.strip().str.casefold()
                == value.strip().casefold()
            ]
        if benchmark_rows.empty:
            parser.error("no sales rows match reference values {!r}".format(args.reference_value))
        benchmark = benchmark_rows["sales_value"].sum() * 100 / benchmark_rows["grams_sold"].sum()
        result["reference_asp_per_100g"] = benchmark
        result["reference_value"] = " | ".join(args.reference_value)
    else:
        # Compare each level value with the total ASP of its reference group.
        benchmarks = (rows.groupby(reference_columns, dropna=False)
                      .agg(_reference_sales=("sales_value", "sum"),
                           _reference_grams=("grams_sold", "sum")))
        benchmarks["reference_asp_per_100g"] = benchmarks["_reference_sales"] * 100 / benchmarks["_reference_grams"]
        result = result.join(benchmarks["reference_asp_per_100g"], on=reference_columns)
        result["reference_value"] = result[reference_columns].astype(str).agg(" | ".join, axis=1)
    result["asp_index"] = (
        result["standard_asp_per_100g"] / result["reference_asp_per_100g"] * 100
    ).where(result["reference_asp_per_100g"] > 0)
    result = result.sort_values([*reference_columns, args.level] +
                                ([args.filter_column] if args.filter_column and args.filter_column not in reference_columns else [])).reset_index(drop=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print("Created {} with {:,} hierarchy rows.".format(args.output, len(result)))


if __name__ == "__main__":
    main()
