

import pyodbc
import pandas as pd

def get_connection():
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=localhost\\SQLEXPRESS;"
        "DATABASE=DMCHM2627;"
        "Trusted_Connection=yes;"
        # SQLEXPRESS uses a locally issued certificate. Trust it for this
        # local development connection so the TLS handshake can complete.
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "Connection Timeout=10;"
    )

if __name__ == "__main__":
    with get_connection() as conn:
        df = pd.read_sql_query(
            "SELECT TABLE_SCHEMA, TABLE_NAME "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME",
            conn
        )
        print(df.to_string(index=False))
