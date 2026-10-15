r"""Build hierarchy-level revenue trees and quantify year-over-year effects.

The latest database's item master supplies item names for sales rows from
both periods. Those names are matched to the mapping CSV before aggregation
at the requested mapping hierarchy.

Example:
    python Affinity_analysis/revenue_tree_analysis.py mapping.csv \
        --level AI_Brand AI_group --filter-column category_name --filter-value TEA \
        --prior-period DMCHM2425 --current-period DMCHM2526

PowerShell example (uncomment and adapt when running from the project root):
    python .\Affinity_analysis\revenue_tree_analysis.py .\Affinity_analysis\mapping.csv `
      --level category_name AI_group AI_pack_size `
      --filter-column category_name `
      --filter-value TEA COFFEE BISCUITS`
      --prior-period DMCHM2425 `
      --current-period DMCHM2526
"""

import argparse
from pathlib import Path
from typing import List, Optional

import pandas as pd

from db_connection import get_connection
from mapping_utils import find_item_name_column, quote_identifier


SALES_DATABASES = ("DMCHM2526", "DMCHM2425")
ITEM_MASTER_DATABASE = "DMCHM2627"
TRANSACTION_TABLE = "datatransfersales"
DEFAULT_OUTPUT = Path(__file__).with_name("revenue_tree_by_level.csv")


def load_sales(mapping: pd.DataFrame, levels: List[str], filter_column: Optional[str],
               name_column: str) -> pd.DataFrame:
    """Join both sales periods to the latest item master, then map item names."""
    mapping_columns = list(dict.fromkeys([name_column, *levels] + ([filter_column] if filter_column else [])))
    records = mapping[mapping_columns].dropna(subset=[name_column, *levels]).copy()
    for column in mapping_columns:
        records[column] = records[column].astype(str).str.strip()
    non_empty = records[name_column].ne("")
    for column in levels:
        non_empty &= records[column].ne("")
    records = records[non_empty]
    if records.empty:
        raise ValueError("The mapping contains no item names and values for hierarchy {!r}.".format(levels))
    if records[name_column].str.casefold().duplicated().any():
        raise ValueError("Mapping item names must be unique (case-insensitive) for revenue tree grouping.")

    results = []
    chunk_size = 500
    value_columns = list(dict.fromkeys([*levels] + ([filter_column] if filter_column else [])))
    with get_connection() as connection:
        master_name_column = find_item_name_column(connection, ITEM_MASTER_DATABASE)
        master_name_expr = "LTRIM(RTRIM(CAST(master.{} AS varchar(255))))".format(
            quote_identifier(master_name_column)
        )
        for database in SALES_DATABASES:
            for start in range(0, len(records), chunk_size):
                chunk = records.iloc[start:start + chunk_size]
                value_aliases = ", ".join(quote_identifier(column) for column in mapping_columns)
                row_placeholders = "(" + ", ".join("?" for _ in mapping_columns) + ")"
                mapping_values = ", ".join(row_placeholders for _ in range(len(chunk)))
                value_select = ", ".join("mapping.{}".format(quote_identifier(column)) for column in value_columns)
                group_by = ", ".join("mapping.{}".format(quote_identifier(column)) for column in value_columns)
                chunk_params = []
                for _, row in chunk.iterrows():
                    chunk_params.extend([row[column] for column in mapping_columns])
                query = (
                    """SELECT {value_select}, sales.voucher_id,
                              SUM(CAST(sales.pamount AS float)) AS sales,
                              SUM(CAST(sales.issue_quantity AS float)) AS units,
                              SUM(CAST(sales.cost_rate AS float) * CAST(sales.issue_quantity AS float)) AS cost,
                              '{period}' AS period
                       FROM {database}.dbo.{sales_table} AS sales
                       INNER JOIN {master_database}.dbo.{master_table} AS master
                           ON master.item_code = sales.item_code
                       INNER JOIN (VALUES {mapping_values}) AS mapping ({mapping_columns})
                           ON UPPER({master_name_expr}) = UPPER(mapping.{name_alias})
                       GROUP BY {group_by}, sales.voucher_id""".format(
                        value_select=value_select,
                        mapping_values=mapping_values,
                        mapping_columns=value_aliases,
                        master_name_expr=master_name_expr,
                        name_alias=quote_identifier(name_column),
                        group_by=group_by,
                        period=database,
                        database=quote_identifier(database),
                        master_database=quote_identifier(ITEM_MASTER_DATABASE),
                        sales_table=quote_identifier(TRANSACTION_TABLE),
                        master_table=quote_identifier("datatransferItem_codes"),
                    )
                )
                results.append(pd.read_sql_query(query, connection, params=chunk_params))
    if not results:
        return pd.DataFrame(columns=[*value_columns, "sales", "bills", "units", "cost", "period"])
    raw = pd.concat(results, ignore_index=True)
    # Item names are unique across chunks, but one bill can contain items
    # assigned to different chunks. Count bills only after recombining them.
    grouping = [*value_columns, "period"]
    return (raw.groupby(grouping, dropna=False)
            .agg(sales=("sales", lambda values: values.sum(min_count=1)),
                 bills=("voucher_id", "nunique"),
                 units=("units", lambda values: values.sum(min_count=1)),
                 cost=("cost", lambda values: values.sum(min_count=1)))
            .reset_index())


def make_result(rows: pd.DataFrame, level: str, periods: List[str],
                dimensions: Optional[List[str]] = None) -> pd.DataFrame:
    """Combine period-level aggregates and derive the revenue tree and effects."""
    prior, current = periods
    dimensions = dimensions or [level]
    keys = [*dimensions, "period"]
    grouped = (rows.groupby(keys, dropna=False)
               .agg(sales=("sales", lambda values: values.sum(min_count=1)),
                    bills=("bills", "sum"),
                    units=("units", lambda values: values.sum(min_count=1)),
                    cost=("cost", lambda values: values.sum(min_count=1)))
               .reset_index())
    # Outer-join period aggregates so hierarchy values present in only one
    # year remain in the output. Use zero only when that hierarchy is absent
    # from a period; preserve nulls for measures missing on present sales rows.
    wide = None
    for period in periods:
        period_rows = grouped[grouped["period"] == period].drop(columns="period").rename(
            columns={metric: "{}_{}".format(metric, period)
                     for metric in ("sales", "bills", "units", "cost")}
        )
        wide = (period_rows if wide is None else
                wide.merge(period_rows, on=dimensions, how="outer"))
    for metric in ("sales", "bills", "units", "cost"):
        for period in periods:
            column = "{}_{}".format(metric, period)
            if column not in wide:
                wide[column] = 0.0
            present_keys = grouped.loc[grouped["period"] == period, dimensions].drop_duplicates()
            presence = wide[dimensions].merge(
                present_keys.assign(_present=True), on=dimensions, how="left"
            )["_present"].fillna(False).to_numpy(dtype=bool)
            absent_from_period = ~presence
            wide.loc[absent_from_period, column] = 0.0

    def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
        return numerator.div(denominator.where(denominator != 0))

    # Revenue tree identities: Sales = Bills × ABV = Bills × UPT × ASP.
    for period in periods:
        wide["abv_{}".format(period)] = safe_divide(wide["sales_{}".format(period)], wide["bills_{}".format(period)])
        wide["asp_{}".format(period)] = safe_divide(wide["sales_{}".format(period)], wide["units_{}".format(period)])
        wide["upt_{}".format(period)] = safe_divide(wide["units_{}".format(period)], wide["bills_{}".format(period)])
        wide["acp_{}".format(period)] = safe_divide(wide["cost_{}".format(period)], wide["units_{}".format(period)])
        wide["am_{}".format(period)] = safe_divide(wide["sales_{}".format(period)] - wide["cost_{}".format(period)], wide["units_{}".format(period)])

    tree_metrics = ("sales", "bills", "units", "cost", "abv", "asp", "upt", "acp", "am")
    # for metric in tree_metrics:
    #     p, c = "{}_{}".format(metric, prior), "{}_{}".format(metric, current)
    #     wide["{}_growth_pct".format(metric)] = safe_divide(wide[c] - wide[p], wide[p]) * 100

    # Isolated component effects: change one driver at a time from prior year
    # to current year while holding every other driver at its current-year level.
    # These independent counterfactuals are not additive because their effects
    # include overlapping interactions.
    p_sales, c_sales = wide["sales_{}".format(prior)], wide["sales_{}".format(current)]
    p_bills, c_bills = wide["bills_{}".format(prior)], wide["bills_{}".format(current)]
    p_abv, c_abv = wide["abv_{}".format(prior)], wide["abv_{}".format(current)]
    wide["sales_growth_amount"] = c_sales - p_sales
    wide["sales_growth_pct"] = safe_divide(c_sales - p_sales, p_sales) * 100
    baskets_effect = (c_bills - p_bills) * c_abv
    wide["baskets_effect_pct"] = safe_divide(baskets_effect, p_sales) * 100
    abv_effect = p_bills * (c_abv - p_abv)
    wide["abv_effect_pct"] = safe_divide(abv_effect, p_sales) * 100

    p_units, c_units = wide["units_{}".format(prior)], wide["units_{}".format(current)]
    p_asp, c_asp = wide["asp_{}".format(prior)], wide["asp_{}".format(current)]
    p_upt, c_upt = wide["upt_{}".format(prior)], wide["upt_{}".format(current)]
    upt_effect = (c_upt - p_upt) * p_asp
    wide["upt_effect_pct"] = safe_divide(upt_effect, p_abv) * 100
    asp_effect = (c_asp - p_asp) * c_upt
    wide["asp_effect_pct"] = safe_divide(asp_effect, p_abv) * 100
 
    p_cost, c_cost = wide["cost_{}".format(prior)], wide["cost_{}".format(current)]
    p_acp, c_acp = wide["acp_{}".format(prior)], wide["acp_{}".format(current)]
    p_am, c_am = wide["am_{}".format(prior)], wide["am_{}".format(current)]
    cost_price_effect = c_acp - p_acp
    wide["cost_price_effect_pct"] = safe_divide(cost_price_effect, p_asp) * 100
    margin_effect = c_am - p_am
    wide["margin_effect_pct"] = safe_divide(margin_effect, p_asp) * 100

    ordered_columns = dimensions.copy()
    for metric in tree_metrics:
        ordered_columns.extend(
            column for column in (
                "{}_{}".format(metric, current),
                "{}_{}".format(metric, prior),
                "{}_growth_pct".format(metric),
            ) if column in wide.columns
        )
    ordered_columns.extend([
        "sales_growth_amount",
        "baskets_effect_pct",
        "abv_effect_pct",
        "upt_effect_pct",
        "asp_effect_pct",
        "cost_price_effect_pct",
        "margin_effect_pct",
    ])
    return (wide[ordered_columns]
            .sort_values(dimensions, na_position="last")
            .reset_index(drop=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create hierarchy-level revenue tree and isolated sales effects.")
    parser.add_argument("mapping_file", type=Path, help="CSV whose hierarchy values define the report groups")
    parser.add_argument("--level", required=True, nargs="+",
                        help="One or more mapping columns defining report groups (e.g. AI_group brand)")
    parser.add_argument("--mapping-name-column", default="ITEM_NAME",
                        help="Mapping item-name column used to match the item master (default: ITEM_NAME)")
    parser.add_argument("--filter-column", help="Optional mapping column to restrict the input (e.g. category_name)")
    parser.add_argument("--filter-value", nargs="+", help="One or more values to select in --filter-column")
    parser.add_argument("--prior-period", default="DMCHM2425", help="Prior database period (default: DMCHM2425)")
    parser.add_argument("--current-period", default="DMCHM2526", help="Current database period (default: DMCHM2526)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output wide CSV path")
    args = parser.parse_args()
    if args.prior_period == args.current_period:
        parser.error("prior and current periods must differ")
    if args.prior_period not in SALES_DATABASES or args.current_period not in SALES_DATABASES:
        parser.error("periods must be selected from {}".format(", ".join(SALES_DATABASES)))

    mapping = pd.read_csv(args.mapping_file)
    level_columns = args.level
    if len(set(level_columns)) != len(level_columns):
        parser.error("--level columns must be unique")
    missing = [column for column in [*level_columns, args.mapping_name_column] if column not in mapping.columns]
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
                            .isin(requested_values)]
        if mapping.empty:
            parser.error("no mapping rows match the requested filter")
    for column in level_columns:
        mapping[column] = mapping[column].astype("string").str.strip()
    mapping = mapping.dropna(subset=level_columns)
    if mapping.empty:
        parser.error("mapping has no values for hierarchy columns {!r}".format(level_columns))
    try:
        sales = load_sales(mapping, level_columns, args.filter_column,
                           args.mapping_name_column)
    except ValueError as error:
        parser.error(str(error))
    for index, column in enumerate(level_columns):
        key_column = "_level_key_{}".format(index)
        mapping[key_column] = mapping[column].astype("string").str.strip().str.casefold()
        labels = mapping.drop_duplicates(key_column).set_index(key_column)[column]
        sales[key_column] = sales[column].astype("string").str.strip().str.casefold()
        sales[column] = sales[key_column].map(labels)
    if args.filter_column:
        filter_labels = mapping.dropna(subset=[args.filter_column]).copy()
        filter_labels["_filter_key"] = filter_labels[args.filter_column].astype("string").str.strip().str.casefold()
        filter_labels = filter_labels.drop_duplicates("_filter_key").set_index("_filter_key")[args.filter_column]
        sales["_filter_key"] = sales[args.filter_column].astype("string").str.strip().str.casefold()
        sales[args.filter_column] = sales["_filter_key"].map(filter_labels)
    level_key_columns = ["_level_key_{}".format(index) for index in range(len(level_columns))]
    rows = sales.dropna(subset=level_columns).drop(columns=level_key_columns)
    if args.filter_column:
        rows = rows.dropna(subset=[args.filter_column]).drop(columns="_filter_key")
    rows["period"] = rows["period"].astype(str).str.strip()
    rows = rows[rows["period"].isin([args.prior_period, args.current_period])]
    # Keep sales rows when cost, quantity, or voucher is missing; those null
    # fields should affect only their own measure, not exclude the sales value.
    rows = rows.dropna(subset=level_columns)
    if rows.empty:
        parser.error("no sales rows matched the requested hierarchy and periods")
    dimensions = list(dict.fromkeys([*level_columns] + ([args.filter_column] if args.filter_column else [])))
    result = make_result(rows, level_columns[0], [args.prior_period, args.current_period], dimensions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print("Created {} with {:,} hierarchy values.".format(args.output, len(result)))


if __name__ == "__main__":
    main()
