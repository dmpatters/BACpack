"""Dropping a column the way every SQLite can, for tests that build an old
database by taking columns off a current one. ALTER TABLE ... DROP COLUMN
needs SQLite 3.35, and BACpack runs on 3.34 (RHEL 9), so the suite uses the
rebuild below everywhere and the code it exercises is the code that runs
there."""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection


def drop_column(connection: Connection, table: str, column: str, schema: str = "main") -> None:
    """Rebuild `schema.table` without `column`: the same columns, defaults,
    primary key, UNIQUE constraints, plain indexes and the file's triggers.
    Not kept: FOREIGN KEY and CHECK clauses and AUTOINCREMENT. An index or
    trigger on this table is dropped when its SQL contains the column name as
    a word (so a longer name in a string literal is dropped with it); a trigger
    on another table that names both this table and the column raises, as a
    real DROP COLUMN would refuse it."""
    qualified = f'{schema}."{table}"'
    info = connection.execute(text(f"PRAGMA {schema}.table_info(\"{table}\")")).all()
    if column not in {row[1] for row in info}:
        raise AssertionError(f"{table} has no column {column}")
    kept = [row for row in info if row[1] != column]
    keys = [row[1] for row in sorted(kept, key=lambda row: row[5]) if row[5]]
    single_key = len(keys) == 1
    parts = []
    for _cid, name, kind, notnull, default, pk in kept:
        part = f'"{name}" {kind}'
        if pk and single_key:
            part += " PRIMARY KEY"
        if notnull:
            part += " NOT NULL"
        if default is not None:
            part += f" DEFAULT {default}"
        parts.append(part)
    if len(keys) > 1:
        parts.append("PRIMARY KEY (" + ", ".join(f'"{key}"' for key in keys) + ")")
    for _seq, index_name, unique, origin, _partial in connection.execute(
        text(f'PRAGMA {schema}.index_list("{table}")')
    ).all():
        if unique and origin == "u":
            columns = [row[2] for row in connection.execute(text(f'PRAGMA {schema}.index_info("{index_name}")')).all()]
            if column not in columns:
                parts.append("UNIQUE (" + ", ".join(f'"{name}"' for name in columns) + ")")
    extras = connection.execute(
        text(
            f"SELECT type, name, sql, tbl_name FROM {schema}.sqlite_master "
            "WHERE sql IS NOT NULL AND ((type = 'index' AND tbl_name = :table) OR type = 'trigger')"
        ),
        {"table": table},
    ).all()
    for kind, name, sql, owner in extras:
        if (
            kind == "trigger"
            and owner != table
            and re.search(rf"\b{re.escape(table)}\b", sql)
            and re.search(rf"\b{re.escape(column)}\b", sql)
        ):
            raise AssertionError(f"trigger {name} on {owner} uses {table}.{column}")
    # A trigger on another table that names this one breaks the rename, so
    # every trigger in the file comes off for the rebuild and goes back after.
    for kind, name, _sql, _owner in extras:
        if kind == "trigger":
            connection.execute(text(f'DROP TRIGGER {schema}."{name}"'))
    names = ", ".join(f'"{row[1]}"' for row in kept)
    temporary = f"{table}__rebuild"
    connection.execute(text(f'CREATE TABLE {schema}."{temporary}" ({", ".join(parts)})'))
    connection.execute(text(f'INSERT INTO {schema}."{temporary}" ({names}) SELECT {names} FROM {qualified}'))
    connection.execute(text(f"DROP TABLE {qualified}"))
    connection.execute(text(f'ALTER TABLE {schema}."{temporary}" RENAME TO "{table}"'))
    for _kind, _name, sql, owner in extras:
        if owner == table and re.search(rf"\b{re.escape(column)}\b", sql):
            continue
        # The stored statement may name no schema; without one it lands in main.
        qualified_sql = re.sub(
            r"^(CREATE (?:UNIQUE )?(?:INDEX|TRIGGER) (?:IF NOT EXISTS )?)(?![\w\"]*\.)",
            lambda found: f"{found.group(1)}{schema}.",
            sql,
            flags=re.I,
        )
        connection.execute(text(qualified_sql))
