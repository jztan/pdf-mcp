"""Tests for the benchmark query-class names and the old-label lookup."""

from scripts._query_classes import (
    CLASS_NAMES,
    LEGACY_PREFIX,
    OLD_TO_NEW,
    legend_lines,
    normalize_class,
    normalize_classes,
    normalize_keys,
    normalize_rows,
)


class TestNormalizeClass:
    def test_every_old_label_maps_to_a_current_name(self):
        assert set(OLD_TO_NEW.values()) == set(CLASS_NAMES)

    def test_old_labels_map(self):
        assert normalize_class("described") == "paraphrase"
        assert normalize_class("needle") == "exact_match"
        assert normalize_class("spread") == "multi_doc"
        assert normalize_class("trap") == "lexical_distractor"

    def test_current_and_unrelated_labels_pass_through(self):
        assert normalize_class("paraphrase") == "paraphrase"
        assert normalize_class("table") == "table"
        assert normalize_class("route") == "route"

    def test_cli_string_and_list(self):
        assert normalize_classes("described, needle,,") == ["paraphrase", "exact_match"]
        assert normalize_classes(["trap", "multi_doc"]) == [
            "lexical_distractor",
            "multi_doc",
        ]


class TestNormalizeRows:
    def test_query_set_document(self):
        doc = {"queries": [{"id": "trap-01", "class": "trap"}]}
        assert normalize_rows(doc) == {
            "queries": [{"id": "trap-01", "class": "lexical_distractor"}]
        }

    def test_ids_are_left_alone(self):
        row = normalize_rows({"id": "needle-04", "class": "needle"})
        assert row["id"] == "needle-04"

    def test_nested_result_rows_keyed_by_query_id(self):
        saved = {
            "arms": {
                "corpus": {
                    "rows": {"snippet": {"described-01": {"class": "described"}}}
                }
            }
        }
        normalize_rows(saved)
        row = saved["arms"]["corpus"]["rows"]["snippet"]["described-01"]
        assert row["class"] == "paraphrase"

    def test_non_string_class_value_is_untouched(self):
        assert normalize_rows({"class": 3}) == {"class": 3}


def test_normalize_keys():
    assert normalize_keys({"trap": 0.5, "table": 0.1}) == {
        "lexical_distractor": 0.5,
        "table": 0.1,
    }


def test_legacy_prefix_maps_each_current_name_to_its_old_label():
    assert LEGACY_PREFIX == {
        "paraphrase": "described",
        "exact_match": "needle",
        "multi_doc": "spread",
        "lexical_distractor": "trap",
    }


class TestLegendLines:
    def test_one_bullet_per_class_then_a_blank_line(self):
        lines = legend_lines()
        assert len(lines) == len(CLASS_NAMES) + 1
        assert lines[0].startswith("- **paraphrase**: ")
        assert lines[-1] == ""

    def test_rows_that_are_not_query_classes_are_skipped(self):
        assert legend_lines(["all", "multi_doc", "table"]) == [
            "- **multi_doc**: several documents each hold a valid answer;"
            " finding any one counts",
            "",
        ]

    def test_no_query_classes_gives_no_legend(self):
        assert legend_lines(["all", "table"]) == []
