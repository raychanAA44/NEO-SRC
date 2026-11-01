#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_catalog_groups.py - NEO-GW-01 模型分组命名、校验与加载器单测。
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.catalog import (
    ModelGroupCatalog,
    normalize_canonical,
    parse_canonical,
    validate_group_definition,
)

PROJECT_ROOT = HERE.parent.parent
SAMPLE_CONFIG = PROJECT_ROOT / "运维区" / "configs" / "model_groups.json"


def sample_group():
    """工单示例分组；每次返回新对象，避免测试间串扰。"""
    return {
        "name": "重型编程组",
        "policy": {"task_lock": True, "smart_tier": True, "hedged_racing": False},
        "members": [
            {"channel": "trae", "id": "claude-3-7-sonnet",
             "tier": "strong", "priority": 1, "enabled": True},
            {"channel": "workbuddy-intl", "id": "claude-3-7-sonnet",
             "tier": "strong", "priority": 2, "enabled": True},
            {"channel": "workbuddy-intl", "id": "glm-5.3",
             "tier": "weak", "priority": 3, "enabled": True},
        ],
    }


class TestCanonicalNaming(unittest.TestCase):
    def test_normalize_strips_whitespace(self):
        self.assertEqual(normalize_canonical(" trae ", " claude-3-7-sonnet "),
                         "trae/claude-3-7-sonnet")

    def test_normalize_rejects_empty_parts(self):
        for channel, model_id in (("", "x"), ("trae", ""), ("   ", "x")):
            with self.assertRaises(ValueError):
                normalize_canonical(channel, model_id)

    def test_normalize_rejects_slash_in_channel(self):
        with self.assertRaises(ValueError):
            normalize_canonical("trae/intl", "glm-5.3")

    def test_normalize_rejects_non_string(self):
        with self.assertRaises(ValueError):
            normalize_canonical(None, "glm-5.3")

    def test_parse_canonical(self):
        self.assertEqual(parse_canonical("workbuddy-intl/glm-5.3"),
                         ("workbuddy-intl", "glm-5.3"))

    def test_parse_canonical_strips_whitespace(self):
        self.assertEqual(parse_canonical(" trae / claude "), ("trae", "claude"))

    def test_parse_canonical_allows_slash_in_model_id(self):
        self.assertEqual(parse_canonical("trae/meta/llama-3"), ("trae", "meta/llama-3"))

    def test_parse_canonical_rejects_bad_format(self):
        for value in ("trae", "/glm-5.3", "trae/", "   ", "", None, 123):
            with self.assertRaises(ValueError):
                parse_canonical(value)


class TestGroupValidation(unittest.TestCase):
    def test_valid_group_is_normalized_and_copied(self):
        data = sample_group()
        result = validate_group_definition("auto/code-heavy", data)

        self.assertIsNot(result, data)
        self.assertEqual(result["members"][0]["channel"], "trae")
        self.assertEqual(result["members"][0]["id"], "claude-3-7-sonnet")
        self.assertEqual(result["members"][0]["name"], "trae/claude-3-7-sonnet")

        data["members"][0]["channel"] = "mutated"
        self.assertEqual(result["members"][0]["channel"], "trae")

    def test_member_whitespace_is_stripped(self):
        data = sample_group()
        data["members"][0]["channel"] = " trae "
        data["members"][0]["id"] = " claude-3-7-sonnet "
        result = validate_group_definition("auto/code-heavy", data)
        self.assertEqual(result["members"][0]["name"], "trae/claude-3-7-sonnet")

    def test_group_id_must_use_auto_prefix(self):
        for group_id in ("code-heavy", "AUTO/code-heavy", "auto", ""):
            with self.assertRaises(ValueError):
                validate_group_definition(group_id, sample_group())

    def test_group_id_suffix_must_not_be_empty(self):
        for group_id in ("auto/", "auto/   "):
            with self.assertRaises(ValueError):
                validate_group_definition(group_id, sample_group())

    def test_group_id_whitespace_rejected(self):
        with self.assertRaises(ValueError):
            validate_group_definition(" auto/code-heavy", sample_group())

    def test_group_data_must_be_dict(self):
        with self.assertRaises(ValueError):
            validate_group_definition("auto/code-heavy", [])

    def test_members_must_be_list(self):
        data = sample_group()
        data["members"] = {}
        with self.assertRaises(ValueError):
            validate_group_definition("auto/code-heavy", data)

    def test_member_must_have_non_empty_channel_and_id(self):
        data = sample_group()
        del data["members"][0]["channel"]
        with self.assertRaises(ValueError):
            validate_group_definition("auto/code-heavy", data)

        data = sample_group()
        data["members"][0]["id"] = "   "
        with self.assertRaises(ValueError):
            validate_group_definition("auto/code-heavy", data)

    def test_registered_models_pass(self):
        registered = ["trae/claude-3-7-sonnet",
                      "workbuddy-intl/claude-3-7-sonnet",
                      "workbuddy-intl/glm-5.3"]
        result = validate_group_definition("auto/code-heavy", sample_group(), registered)
        self.assertEqual(len(result["members"]), 3)

    def test_unregistered_member_rejected_with_clear_message(self):
        data = sample_group()
        data["members"][0] = {"channel": "ghost", "id": "missing-model",
                              "tier": "strong", "priority": 1, "enabled": True}
        with self.assertRaises(ValueError) as ctx:
            validate_group_definition("auto/code-heavy", data,
                                      ["trae/claude-3-7-sonnet"])
        self.assertIn("未登记的非法模型ID: ghost/missing-model", str(ctx.exception))

    def test_orphan_member_allowed(self):
        data = sample_group()
        data["members"][0] = {"channel": "ghost", "id": "missing-model",
                              "tier": "strong", "priority": 1,
                              "enabled": True, "orphan": True}
        registered = ["trae/claude-3-7-sonnet",
                      "workbuddy-intl/claude-3-7-sonnet",
                      "workbuddy-intl/glm-5.3"]
        result = validate_group_definition("auto/code-heavy", data, registered)
        self.assertEqual(result["members"][0]["name"], "ghost/missing-model")


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, payload, name="groups.json"):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    @staticmethod
    def _payload():
        return {"version": "2.0.0", "groups": {"auto/code-heavy": sample_group()}}

    def test_load_get_and_list(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        group = catalog.get_group("auto/code-heavy")

        self.assertIsNotNone(group)
        self.assertEqual(catalog.version, "2.0.0")
        self.assertEqual(group["name"], "重型编程组")
        self.assertEqual(len(group["members"]), 3)
        groups = catalog.list_groups()
        self.assertEqual(groups[0]["group_id"], "auto/code-heavy")

    def test_get_group_unknown_returns_none(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        self.assertIsNone(catalog.get_group("auto/unknown"))
        self.assertIsNone(catalog.get_group(None))

    def test_get_group_returns_copy(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        group = catalog.get_group("auto/code-heavy")
        group["members"][0]["channel"] = "mutated"
        self.assertEqual(catalog.get_group("auto/code-heavy")["members"][0]["channel"], "trae")

    def test_members_expose_canonical_name_for_dispatcher(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        names = [m["name"] for m in catalog.get_group("auto/code-heavy")["members"]]
        self.assertEqual(names, ["trae/claude-3-7-sonnet",
                                 "workbuddy-intl/claude-3-7-sonnet",
                                 "workbuddy-intl/glm-5.3"])

    def test_active_members_filters_disabled_and_sorts_priority(self):
        data = sample_group()
        data["members"] = [
            {"channel": "trae", "id": "a", "tier": "strong", "priority": 3, "enabled": True},
            {"channel": "trae", "id": "b", "tier": "strong", "priority": 1, "enabled": False},
            {"channel": "trae", "id": "c", "tier": "weak", "priority": 2, "enabled": True},
            {"channel": "trae", "id": "d", "tier": "strong", "priority": 1, "enabled": True},
        ]
        catalog = ModelGroupCatalog().load_from_file(
            self._write({"groups": {"auto/code-heavy": data}}))

        active = catalog.get_active_members("auto/code-heavy")
        self.assertEqual([m["id"] for m in active], ["d", "c", "a"])

    def test_active_members_tier_filter(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        strong = catalog.get_active_members("auto/code-heavy", tier="strong")
        self.assertEqual([m["name"] for m in strong],
                         ["trae/claude-3-7-sonnet",
                          "workbuddy-intl/claude-3-7-sonnet"])
        self.assertTrue(all(m["tier"] == "strong" for m in strong))

    def test_active_members_unknown_group_returns_empty(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        self.assertEqual(catalog.get_active_members("auto/unknown"), [])

    def test_active_members_return_copy(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        member = catalog.get_active_members("auto/code-heavy")[0]
        member["channel"] = "mutated"
        self.assertEqual(catalog.get_group("auto/code-heavy")["members"][0]["channel"], "trae")

    def test_sample_config_file_is_valid(self):
        catalog = ModelGroupCatalog().load_from_file(str(SAMPLE_CONFIG))
        group = catalog.get_group("auto/code-heavy")

        self.assertIsNotNone(group)
        self.assertEqual(group["policy"]["task_lock"], True)
        members = catalog.get_active_members("auto/code-heavy")
        self.assertEqual(len(members), 3)
        # 样例会随模型目录更换成员；校验结构而不是绑死某个模型名。
        self.assertTrue(all(m.get("upstream_name") for m in members))
        self.assertEqual(members[0]["tier"], "strong")

    def test_invalid_json_raises(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ModelGroupCatalog().load_from_file(str(path))

    def test_missing_group_object_raises(self):
        with self.assertRaises(ValueError):
            ModelGroupCatalog().load_from_file(self._write({"version": "2.0.0"}))

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError):
            ModelGroupCatalog().load_from_file(str(Path(self.tmp.name) / "nope.json"))

    def test_invalid_reload_keeps_previous_cache(self):
        catalog = ModelGroupCatalog().load_from_file(self._write(self._payload()))
        bad = {"groups": {"auto/broken": {"members": "not-a-list"}}}
        with self.assertRaises(ValueError):
            catalog.load_from_file(self._write(bad, "bad.json"))
        self.assertIsNotNone(catalog.get_group("auto/code-heavy"))

    def test_registered_models_enforced_on_load(self):
        data = sample_group()
        data["members"][0] = {"channel": "ghost", "id": "missing-model",
                              "tier": "strong", "priority": 1, "enabled": True}
        path = self._write({"groups": {"auto/code-heavy": data}})
        registered = ["trae/claude-3-7-sonnet",
                      "workbuddy-intl/claude-3-7-sonnet",
                      "workbuddy-intl/glm-5.3"]

        with self.assertRaises(ValueError):
            ModelGroupCatalog().load_from_file(path, registered)

        data["members"][0]["orphan"] = True
        path = self._write({"groups": {"auto/code-heavy": data}}, "orphan.json")
        catalog = ModelGroupCatalog().load_from_file(path, registered)
        self.assertIsNotNone(catalog.get_group("auto/code-heavy"))


if __name__ == "__main__":
    unittest.main()
