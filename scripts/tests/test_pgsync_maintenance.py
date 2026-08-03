from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


REPO = pathlib.Path(__file__).resolve().parents[2]
MAINTENANCE_PATH = REPO / "ops" / "pgsync-stable-alias" / "maintenance.py"


def load_maintenance_module():
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.sql = types.SimpleNamespace()
    with mock.patch.dict(sys.modules, {"psycopg2": fake_psycopg2}):
        spec = importlib.util.spec_from_file_location("mp_maintenance", MAINTENANCE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load maintenance.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class RowsCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def execute(self, _query: str, _params=()) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class CrudConnection:
    def __init__(self, *, commit_error: Exception | None = None,
                 rollback_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.commits = 0
        self.closed = False
        self.cursor_value = RowsCursor([])

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closed = True


class MaintenanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.maintenance = load_maintenance_module()

    @staticmethod
    def view_row(table: str) -> tuple:
        fields = {
            "recipe": (
                ["id"],
                None,
                [
                    "id", "source", "name", "category", "cook_method",
                    "cooking_time", "level_nm", "serving", "kcal", "carb_g",
                    "protein_g", "fat_g", "image_url",
                ],
            ),
            "recipe_ingredient": (
                ["id"],
                ["item_id", "recipe_id"],
                ["id", "recipe_id", "ingredient_name", "item_id", "is_non_ingredient"],
            ),
        }[table]
        return (table, fields[0], fields[1], ["recipes_live"], fields[2])

    def test_read_view_rejects_duplicate_rows(self) -> None:
        cursor = RowsCursor([
            self.view_row("recipe"),
            self.view_row("recipe"),
            self.view_row("recipe_ingredient"),
        ])
        with self.assertRaisesRegex(RuntimeError, "exactly two unique rows"):
            self.maintenance.read_view(cursor)

    def test_read_view_rejects_duplicate_index_values(self) -> None:
        recipe = list(self.view_row("recipe"))
        recipe[3] = ["recipes_live", "recipes_live"]
        cursor = RowsCursor([tuple(recipe), self.view_row("recipe_ingredient")])
        with self.assertRaisesRegex(RuntimeError, "index sets"):
            self.maintenance.read_view(cursor)

    def test_read_view_rejects_missing_payload_column(self) -> None:
        recipe = list(self.view_row("recipe"))
        recipe[4] = [column for column in recipe[4] if column != "protein_g"]
        cursor = RowsCursor([tuple(recipe), self.view_row("recipe_ingredient")])
        with self.assertRaisesRegex(RuntimeError, "columns"):
            self.maintenance.read_view(cursor)

    def test_trigger_contract_rejects_extra_trigger(self) -> None:
        rows = list(self.maintenance.EXPECTED_TRIGGERS)
        rows.append(("recipe", "unrelated_after_insert", "O"))
        with self.assertRaisesRegex(RuntimeError, "trigger contract"):
            self.maintenance.assert_trigger_contract(RowsCursor(rows))

    def test_table_owner_contract_rejects_owner_transfer(self) -> None:
        rows = [("recipe", "mp-pgsync-bootstrap"), ("recipe_ingredient", "fbapp")]
        with self.assertRaisesRegex(RuntimeError, "table ownership"):
            self.maintenance.assert_table_owners(RowsCursor(rows))

    def test_slot_contract_rejects_wrong_plugin(self) -> None:
        rows = [
            ("foodbudget_recipes_live", "foodbudget", "pgoutput", "logical", True, "0/10", "0/08"),
            ("foodbudget_recipes_pgsync", "foodbudget", "test_decoding", "logical", False, "0/10", "0/08"),
        ]
        with self.assertRaisesRegex(RuntimeError, "slot contract"):
            self.maintenance.assert_slot_contracts(RowsCursor(rows), legacy="required")

    def test_slot_contract_requires_live_lsns(self) -> None:
        rows = [
            ("foodbudget_recipes_live", "foodbudget", "test_decoding", "logical", True, None, "0/08"),
        ]
        with self.assertRaisesRegex(RuntimeError, "live slot LSN"):
            self.maintenance.assert_slot_contracts(RowsCursor(rows), legacy="absent")

    @staticmethod
    def aliases(write: bool) -> dict:
        return {
            "recipes_v2": {
                "aliases": {"recipes_live": {"is_write_index": write}},
            }
        }

    def test_alias_write_is_idempotent_and_rechecks_exact_one(self) -> None:
        calls = []

        def es_call(path, method="GET", body=None):
            calls.append((path, method, body))
            if len(calls) == 1:
                return 200, self.aliases(False)
            if len(calls) == 2:
                return 200, {"acknowledged": True}
            return 200, self.aliases(True)

        with mock.patch.object(self.maintenance, "es_call", side_effect=es_call):
            self.maintenance.alias_write_explicit()

        self.assertEqual("POST", calls[1][1])
        self.assertEqual(
            {"actions": [{"add": {
                "index": "recipes_v2",
                "alias": "recipes_live",
                "is_write_index": True,
            }}]},
            calls[1][2],
        )
        self.assertEqual(3, len(calls))

    def test_alias_write_rejects_post_update_multi_backing(self) -> None:
        post_state = self.aliases(True)
        post_state["recipes_v3"] = {
            "aliases": {"recipes_live": {"is_write_index": False}},
        }
        responses = [
            (200, self.aliases(False)),
            (200, {"acknowledged": True}),
            (200, post_state),
        ]
        with mock.patch.object(self.maintenance, "es_call", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "exactly one backing"):
                self.maintenance.alias_write_explicit()

    def test_alias_write_rechecks_an_already_explicit_alias(self) -> None:
        responses = [(200, self.aliases(True)), (200, self.aliases(True))]
        with mock.patch.object(self.maintenance, "es_call", side_effect=responses) as es_call:
            self.maintenance.alias_write_explicit()
        self.assertEqual(2, es_call.call_count)
        self.assertTrue(all(call.args == ("/_alias/recipes_live",) for call in es_call.call_args_list))

    def test_alias_write_main_does_not_connect_to_postgres(self) -> None:
        with (
            mock.patch.object(self.maintenance, "connect", side_effect=AssertionError("DB connect")),
            mock.patch.object(self.maintenance, "alias_write_explicit") as alias_write,
            mock.patch.object(sys, "argv", ["maintenance.py", "alias-write"]),
        ):
            self.assertEqual(0, self.maintenance.main())
        alias_write.assert_called_once_with()

    def test_ambiguous_insert_commit_uses_fresh_cleanup_even_if_rollback_is_broken(self) -> None:
        connection = CrudConnection(
            commit_error=ConnectionError("ambiguous commit"),
            rollback_error=ConnectionError("broken rollback"),
        )
        with (
            mock.patch.object(self.maintenance, "one", side_effect=["fbapp", "foodbudget"]),
            mock.patch.object(self.maintenance, "cleanup_crud_test") as cleanup,
            self.assertRaisesRegex(ConnectionError, "ambiguous commit"),
        ):
            self.maintenance.crud_e2e(connection)
        cleanup.assert_called_once()
        self.assertLess(cleanup.call_args.args[0], 0)
        self.assertRegex(cleanup.call_args.args[1], r"^[0-9a-f]{32}$")

    def test_cleanup_retries_with_a_fresh_connection(self) -> None:
        healthy = CrudConnection()
        with (
            mock.patch.object(
                self.maintenance,
                "connect",
                side_effect=[ConnectionError("first connect failed"), healthy],
            ) as connect,
            mock.patch.object(self.maintenance, "one", side_effect=["fbapp", "foodbudget"]),
            mock.patch.object(self.maintenance, "wait_document") as wait_document,
            mock.patch.object(self.maintenance.time, "sleep"),
        ):
            self.maintenance.cleanup_crud_test(-123, "marker", attempts=2)
        self.assertEqual(2, connect.call_count)
        self.assertEqual(1, healthy.commits)
        self.assertTrue(healthy.closed)
        wait_document.assert_called_once_with(-123, None)


if __name__ == "__main__":
    unittest.main()
