#!/usr/bin/env python3
"""Least-privilege post-bootstrap maintenance for PGSync 7.1.

This file is mounted into a suspended, manual-only Job by ops.sh. It never logs
connection strings or passwords.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

import psycopg2
from psycopg2 import sql

BOOTSTRAP_ROLE = "mp-pgsync-bootstrap"
DATABASE = "foodbudget"
LIVE_INDEX = "recipes_live"
LEGACY_INDEX = "recipes_pgsync"
LIVE_SLOT = "foodbudget_recipes_live"
LEGACY_SLOT = "foodbudget_recipes_pgsync"
EXPECTED_TABLES = {"recipe", "recipe_ingredient"}
EXPECTED_VIEW_FIELDS = {
    "recipe": {
        "primary_keys": {"id"},
        "foreign_keys": None,
        "columns": {
            "id", "source", "name", "category", "cook_method", "cooking_time",
            "level_nm", "serving", "kcal", "carb_g", "protein_g", "fat_g",
            "image_url",
        },
    },
    "recipe_ingredient": {
        "primary_keys": {"id"},
        "foreign_keys": {"item_id", "recipe_id"},
        "columns": {"id", "recipe_id", "ingredient_name", "item_id", "is_non_ingredient"},
    },
}
EXPECTED_TRIGGERS = {
    ("recipe", "public_recipe_notify", "O"),
    ("recipe", "public_recipe_truncate", "O"),
    ("recipe_ingredient", "public_recipe_ingredient_notify", "O"),
    ("recipe_ingredient", "public_recipe_ingredient_truncate", "O"),
}


def connect():
    return psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=int(os.environ.get("PG_PORT", "5432")),
        dbname=os.environ.get("PG_DATABASE", "foodbudget"),
        user=os.environ["PG_USER"],
        password=os.environ["PG_PASSWORD"],
        connect_timeout=5,
        application_name="mp-pgsync-manual-maintenance",
    )


def one(cur, query: str, params: tuple[Any, ...] = ()):
    cur.execute(query, params)
    row = cur.fetchone()
    return row[0] if row else None


def assert_identity(cur) -> None:
    if one(cur, "SELECT current_user") != BOOTSTRAP_ROLE:
        raise RuntimeError("maintenance must run as the dedicated bootstrap role")
    if one(cur, "SELECT current_database()") != DATABASE:
        raise RuntimeError(f"maintenance must run against {DATABASE}")


def assert_table_owners(cur) -> None:
    cur.execute(
        "SELECT c.relname,pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname IN ('recipe','recipe_ingredient') "
        "ORDER BY c.relname"
    )
    if cur.fetchall() != [("recipe", "fbapp"), ("recipe_ingredient", "fbapp")]:
        raise RuntimeError("recipe table ownership differs from the exact fbapp contract")


def restore_acl(conn) -> None:
    with conn, conn.cursor() as cur:
        assert_identity(cur)
        owner = one(
            cur,
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid='public._view'::regclass",
        )
        if owner != BOOTSTRAP_ROLE:
            raise RuntimeError(f"unexpected public._view owner: {owner}")
        read_view(cur)
        assert_trigger_contract(cur)
        assert_slot_contracts(cur, legacy="optional")
        assert_table_owners(cur)
        cur.execute("GRANT SELECT ON TABLE public._view TO fbapp, pgsync")
        cur.execute(
            "SELECT has_table_privilege('fbapp','public._view','SELECT'), "
            "has_table_privilege('pgsync','public._view','SELECT')"
        )
        if cur.fetchone() != (True, True):
            raise RuntimeError("public._view SELECT ACL verification failed")
    print("OK: public._view SELECT restored for fbapp and pgsync")


def read_view(cur):
    cur.execute(
        "SELECT table_name, primary_keys, foreign_keys, indices, columns "
        "FROM public._view ORDER BY table_name"
    )
    rows = cur.fetchall()
    table_names = [row[0] for row in rows]
    if (len(rows) != 2 or len(set(table_names)) != 2 or
            set(table_names) != EXPECTED_TABLES):
        raise RuntimeError(
            "public._view must contain exactly two unique rows for the recipe schema"
        )
    for table_name, primary_keys, foreign_keys, _indices, columns in rows:
        expected = EXPECTED_VIEW_FIELDS[table_name]

        def exact_array(actual, wanted) -> bool:
            if wanted is None:
                return actual is None
            return (isinstance(actual, list) and len(actual) == len(wanted) and
                    len(actual) == len(set(actual)) and set(actual) == wanted)

        if not exact_array(primary_keys, expected["primary_keys"]):
            raise RuntimeError(f"unexpected public._view primary_keys for {table_name}")
        if not exact_array(foreign_keys, expected["foreign_keys"]):
            raise RuntimeError(f"unexpected public._view foreign_keys for {table_name}")
        if not exact_array(columns, expected["columns"]):
            raise RuntimeError(f"unexpected public._view columns for {table_name}")
    index_sets = {tuple(sorted(row[3] or [])) for row in rows}
    if index_sets == {tuple(sorted((LIVE_INDEX, LEGACY_INDEX)))}:
        return "live+legacy", rows
    if index_sets == {(LIVE_INDEX,)}:
        return "live-only", rows
    raise RuntimeError(f"unexpected public._view index sets: {index_sets}")


def assert_trigger_contract(cur) -> None:
    cur.execute(
        "SELECT c.relname, t.tgname, t.tgenabled FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' "
        "AND c.relname IN ('recipe','recipe_ingredient') "
        "AND NOT t.tgisinternal ORDER BY c.relname,t.tgname"
    )
    rows = cur.fetchall()
    if len(rows) != 4 or set(rows) != EXPECTED_TRIGGERS:
        raise RuntimeError(f"unexpected PGSync trigger contract: {rows}")


def assert_slot_contracts(cur, *, legacy: str):
    if legacy not in {"required", "optional", "absent"}:
        raise ValueError(f"invalid legacy slot expectation: {legacy}")
    cur.execute(
        "SELECT slot_name,database,plugin,slot_type,active,"
        "confirmed_flush_lsn,restart_lsn FROM pg_replication_slots "
        "WHERE slot_name IN (%s,%s) ORDER BY slot_name",
        (LIVE_SLOT, LEGACY_SLOT),
    )
    rows = cur.fetchall()
    slots = {row[0]: row[1:] for row in rows}
    if len(slots) != len(rows):
        raise RuntimeError("duplicate replication slot rows returned")
    if LIVE_SLOT not in slots:
        raise RuntimeError(f"required live slot is missing: {LIVE_SLOT}")

    def check(name: str, values: tuple, *, require_lsn: bool) -> None:
        database, plugin, slot_type, _active, confirmed_lsn, restart_lsn = values
        expected = (DATABASE, "test_decoding", "logical")
        if (database, plugin, slot_type) != expected:
            raise RuntimeError(
                f"unexpected replication slot contract for {name}: "
                f"database={database}, plugin={plugin}, slot_type={slot_type}"
            )
        if require_lsn and (confirmed_lsn is None or restart_lsn is None):
            raise RuntimeError(f"live slot LSN is incomplete for {name}")

    check(LIVE_SLOT, slots[LIVE_SLOT], require_lsn=True)
    legacy_values = slots.get(LEGACY_SLOT)
    if legacy == "required" and legacy_values is None:
        raise RuntimeError(f"required legacy slot is missing: {LEGACY_SLOT}")
    if legacy == "absent" and legacy_values is not None:
        raise RuntimeError(f"legacy slot must be absent: {LEGACY_SLOT}")
    if legacy_values is not None:
        check(LEGACY_SLOT, legacy_values, require_lsn=False)
        if legacy_values[3] is not False:
            raise RuntimeError("legacy slot must be inactive")
    return slots


def assert_view_contract(cur, expected_state: str):
    owner = one(
        cur,
        "SELECT pg_get_userbyid(relowner) FROM pg_class "
        "WHERE oid='public._view'::regclass",
    )
    if owner != BOOTSTRAP_ROLE:
        raise RuntimeError(f"unexpected public._view owner: {owner}")
    state, rows = read_view(cur)
    if state != expected_state:
        raise RuntimeError(f"public._view is {state}, expected {expected_state}")
    cur.execute(
        "SELECT has_table_privilege('fbapp','public._view','SELECT'), "
        "has_table_privilege('pgsync','public._view','SELECT')"
    )
    if cur.fetchone() != (True, True):
        raise RuntimeError("public._view SELECT ACL verification failed")
    assert_trigger_contract(cur)
    assert_table_owners(cur)
    return rows


def retire_legacy(conn) -> None:
    """Two-phase, resumable retirement under PGSync-compatible session locks."""
    cur = conn.cursor()
    try:
        assert_identity(cur)
        # Session locks survive the view-swap commit and serialize the later,
        # non-transactional slot drop. Order matches PGSync setup/teardown.
        cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (DATABASE,))
        cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (LEGACY_SLOT,))
        conn.commit()

        slots = assert_slot_contracts(cur, legacy="optional")
        legacy_exists = LEGACY_SLOT in slots

        state, rows = read_view(cur)
        # Allowed resumable states:
        #   A: live+legacy view + legacy slot -> perform phase 1 then phase 2
        #   B: live-only view + legacy slot  -> resume at phase 2
        #   C: live-only view + no slot      -> already complete
        if state == "live+legacy" and not legacy_exists:
            raise RuntimeError("legacy view route exists after slot disappearance; inspect manually")
        assert_view_contract(cur, state)
        cur.execute(
            "SELECT to_regclass('public._view_next'), "
            "to_regclass('public._view_legacy')"
        )
        if cur.fetchone() != (None, None):
            raise RuntimeError("staging view already exists; inspect manually")
        conn.commit()

        if state == "live+legacy":
            values = []
            params: list[Any] = []
            for table_name, pks, fks, _indices, columns in rows:
                values.append(
                    sql.SQL("(%s::text,%s::text[],%s::text[],%s::text[],%s::text[])")
                )
                params.extend((table_name, pks, fks, [LIVE_INDEX], columns))
            create = sql.SQL(
                "CREATE MATERIALIZED VIEW public._view_next AS "
                "SELECT * FROM (VALUES {}) "
                "AS v(table_name,primary_keys,foreign_keys,indices,columns)"
            ).format(sql.SQL(",").join(values))
            cur.execute(create, params)
            cur.execute("CREATE UNIQUE INDEX _idx_next ON public._view_next(table_name)")
            cur.execute("GRANT SELECT ON TABLE public._view_next TO fbapp, pgsync")
            cur.execute("LOCK TABLE public._view IN ACCESS EXCLUSIVE MODE")
            cur.execute("ALTER MATERIALIZED VIEW public._view RENAME TO _view_legacy")
            cur.execute("ALTER INDEX public._idx RENAME TO _idx_legacy")
            cur.execute("ALTER MATERIALIZED VIEW public._view_next RENAME TO _view")
            cur.execute("ALTER INDEX public._idx_next RENAME TO _idx")
            cur.execute("DROP MATERIALIZED VIEW public._view_legacy")
            conn.commit()  # phase 1: atomic view swap
            print("OK: phase 1 committed (_view is recipes_live-only)")

        assert_view_contract(cur, "live-only")
        slots = assert_slot_contracts(
            cur,
            legacy="required" if legacy_exists else "absent",
        )
        legacy_row = slots.get(LEGACY_SLOT)
        conn.commit()

        if legacy_row is not None:
            # Slot removal has no transaction rollback callback. Execute only
            # after phase 1 is durably committed, while both session locks remain held.
            conn.autocommit = True
            cur.execute("SELECT pg_drop_replication_slot(%s)", (LEGACY_SLOT,))
            print(f"OK: phase 2 dropped {LEGACY_SLOT}")
        else:
            conn.autocommit = True
            print(f"OK: phase 2 already complete ({LEGACY_SLOT} absent)")

        assert_view_contract(cur, "live-only")
        assert_slot_contracts(cur, legacy="absent")
    finally:
        try:
            conn.rollback()
            conn.autocommit = True
            with conn.cursor() as unlock:
                unlock.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (LEGACY_SLOT,))
                unlock.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (DATABASE,))
        except Exception:
            # Closing the connection below also releases session locks.
            pass
        cur.close()
    print(f"OK: retired {LEGACY_SLOT} and removed {LEGACY_INDEX} from public._view")


def es_call(path: str, method: str = "GET", body: dict | None = None):
    token = base64.b64encode(
        (os.environ.get("ELASTICSEARCH_USER", "elastic") + ":" +
         os.environ["ELASTICSEARCH_PASSWORD"]).encode()
    ).decode()
    scheme = os.environ.get("ELASTICSEARCH_SCHEME", "http")
    host = os.environ.get("ELASTICSEARCH_HOST", "es-es-http")
    port = os.environ.get("ELASTICSEARCH_PORT", "9200")
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{scheme}://{host}:{port}" + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Basic " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        raise


def alias_write_explicit() -> None:
    status, aliases = es_call(f"/_alias/{LIVE_INDEX}")
    if status != 200 or not isinstance(aliases, dict) or len(aliases) != 1:
        raise RuntimeError(f"{LIVE_INDEX} must have exactly one backing")
    backing = next(iter(aliases))
    config = aliases[backing].get("aliases", {}).get(LIVE_INDEX, {})
    if config.get("is_write_index") is not True:
        status, result = es_call(
            "/_aliases",
            "POST",
            {"actions": [{"add": {
                "index": backing,
                "alias": LIVE_INDEX,
                "is_write_index": True,
            }}]},
        )
        if status != 200 or not result or result.get("acknowledged") is not True:
            raise RuntimeError("Elasticsearch did not acknowledge alias write-index update")

    status, aliases = es_call(f"/_alias/{LIVE_INDEX}")
    if status != 200 or not isinstance(aliases, dict) or len(aliases) != 1:
        raise RuntimeError(f"{LIVE_INDEX} must have exactly one backing after update")
    post_backing = next(iter(aliases))
    post_config = aliases[post_backing].get("aliases", {}).get(LIVE_INDEX, {})
    if post_backing != backing or post_config.get("is_write_index") is not True:
        raise RuntimeError("alias backing changed or explicit write flag was not persisted")
    print(f"OK: {LIVE_INDEX} -> {backing} (is_write_index=true)")


def wait_document(doc_id: int, expected_name: str | None, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = es_call(f"/recipes_live/_doc/{doc_id}")
        if expected_name is None and status == 404:
            return
        if status == 200 and expected_name is not None:
            if body.get("_source", {}).get("name") == expected_name:
                return
        time.sleep(0.5)
    raise RuntimeError(f"CDC timeout for document {doc_id}, expected name={expected_name!r}")


def cleanup_crud_test(doc_id: int, marker: str, attempts: int = 3) -> None:
    """Idempotent cleanup on a fresh connection, including ambiguous COMMIT results."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        cleanup_conn = None
        try:
            cleanup_conn = connect()
            with cleanup_conn.cursor() as cur:
                if one(cur, "SELECT current_user") != "fbapp":
                    raise RuntimeError("CRUD cleanup must run as fbapp")
                if one(cur, "SELECT current_database()") != DATABASE:
                    raise RuntimeError(f"CRUD cleanup must run against {DATABASE}")
                cur.execute(
                    "DELETE FROM public.recipe "
                    "WHERE id=%s AND source='PGSYNC_VERIFY' AND src_recipe_id=%s",
                    (doc_id, marker),
                )
            cleanup_conn.commit()
            wait_document(doc_id, None)
            print(f"OK: cleanup confirmed for test id {doc_id}")
            return
        except Exception as exc:
            last_error = exc
            if cleanup_conn is not None:
                try:
                    cleanup_conn.rollback()
                except Exception:
                    pass
            if attempt < attempts:
                time.sleep(1)
        finally:
            if cleanup_conn is not None:
                try:
                    cleanup_conn.close()
                except Exception:
                    pass
    raise RuntimeError(
        f"CRUD cleanup failed after {attempts} fresh-connection attempts for id {doc_id}"
    ) from last_error


def crud_e2e(conn) -> None:
    """Commit INSERT/UPDATE/DELETE as fbapp and verify each event via recipes_live."""
    marker = uuid.uuid4().hex
    doc_id = -(time.time_ns() % 8_000_000_000_000_000_000)
    before = f"PGSync CDC verify {marker[:12]}"
    after = before + " updated"
    cleanup_required = False
    try:
        with conn.cursor() as cur:
            if one(cur, "SELECT current_user") != "fbapp":
                raise RuntimeError("CRUD E2E must run as fbapp")
            if one(cur, "SELECT current_database()") != DATABASE:
                raise RuntimeError(f"CRUD E2E must run against {DATABASE}")
            # Set before INSERT/COMMIT: a server-side commit with a lost client
            # acknowledgement is still possible and must be cleaned idempotently.
            cleanup_required = True
            cur.execute(
                "INSERT INTO public.recipe(id,source,src_recipe_id,name) "
                "VALUES (%s,'PGSYNC_VERIFY',%s,%s)",
                (doc_id, marker, before),
            )
        conn.commit()
        wait_document(doc_id, before)
        print(f"OK: INSERT propagated for test id {doc_id}")

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.recipe SET name=%s "
                "WHERE id=%s AND source='PGSYNC_VERIFY'",
                (after, doc_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("test UPDATE did not affect exactly one row")
        conn.commit()
        wait_document(doc_id, after)
        print(f"OK: UPDATE propagated for test id {doc_id}")

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.recipe WHERE id=%s AND source='PGSYNC_VERIFY'",
                (doc_id,),
            )
            if cur.rowcount != 1:
                raise RuntimeError("test DELETE did not affect exactly one row")
        conn.commit()
        wait_document(doc_id, None)
        print(f"OK: DELETE propagated and test id {doc_id} is absent")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        if cleanup_required:
            cleanup_crud_test(doc_id, marker)


def main() -> int:
    actions = {"restore-acl", "retire-legacy", "crud-e2e", "alias-write"}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        print(
            "usage: maintenance.py {restore-acl|retire-legacy|crud-e2e|alias-write}",
            file=sys.stderr,
        )
        return 2
    if sys.argv[1] == "alias-write":
        alias_write_explicit()
        return 0
    conn = connect()
    try:
        if sys.argv[1] == "restore-acl":
            restore_acl(conn)
        elif sys.argv[1] == "retire-legacy":
            retire_legacy(conn)
        else:
            crud_e2e(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
