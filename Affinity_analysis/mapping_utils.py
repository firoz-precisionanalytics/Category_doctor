"""Helpers for matching mapping product names to database item masters."""

import pandas as pd


ITEM_MASTER_TABLE = "datatransferItem_codes"
ITEM_NAME_CANDIDATES = (
    "item_name",
    "item_description",
    "item_desc",
    "description",
    "itemname",
)


def _get_item_master_columns(connection, database: str) -> list:
    """Read item-master columns through pyodbc without pandas' DBAPI warning."""
    query = (
        "SELECT COLUMN_NAME FROM {database}.INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ?"
    ).format(database=quote_identifier(database))
    cursor = connection.cursor()
    try:
        cursor.execute(query, ITEM_MASTER_TABLE)
        return [str(row[0]) for row in cursor.fetchall()]
    finally:
        cursor.close()


def quote_identifier(identifier: str) -> str:
    return "[{}]".format(identifier.replace("]", "]]"))


def normalize_item_names(mapping: pd.DataFrame, name_column: str) -> pd.DataFrame:
    """Add a normalized ``item_name_key`` based on mapping item names."""
    available = {str(column).casefold(): column for column in mapping.columns}
    actual_name_column = available.get(name_column.casefold())
    if actual_name_column is None:
        raise ValueError("Mapping is missing item-name column {!r}.".format(name_column))

    result = mapping.copy()
    names = result[actual_name_column].astype("string").str.strip()
    if names.isna().any() or names.eq("").any():
        raise ValueError("Mapping item names must be non-empty.")
    result["item_name_key"] = names.str.casefold()
    if result["item_name_key"].duplicated().any():
        duplicates = names[result["item_name_key"].duplicated(keep=False)].drop_duplicates().head(6).tolist()
        raise ValueError("Mapping has duplicate item names: {}".format(", ".join(duplicates)))
    return result


def find_master_column(connection, database: str, requested_column: str) -> str:
    """Resolve a requested item-master column without assuming its casing."""
    columns = _get_item_master_columns(connection, database)
    lookup = {column.casefold(): column for column in columns}
    actual = lookup.get(requested_column.casefold())
    if actual is None:
        raise ValueError(
            "Column {!r} is not available in {}.dbo.{}. Available columns: {}".format(
                requested_column, database, ITEM_MASTER_TABLE, ", ".join(columns)
            )
        )
    return actual


def find_item_name_column(connection, database: str) -> str:
    """Find a description column in the item master for the given database."""
    columns = _get_item_master_columns(connection, database)
    lookup = {column.casefold(): column for column in columns}
    for candidate in ITEM_NAME_CANDIDATES:
        if candidate in lookup:
            return lookup[candidate]
    raise ValueError(
        "No recognized item-name column found in {}.dbo.{}. Available columns: {}".format(
            database, ITEM_MASTER_TABLE, ", ".join(columns)
        )
    )
