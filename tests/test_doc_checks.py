"""Built-in doc checks judge presence and links, never content (D25)."""
from ratchetloop.doc_checks import broken_links, doc_check_results


def test_relative_links_must_resolve_and_urls_and_anchors_are_ignored(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DESIGN.md").write_text("# Design\n")
    (tmp_path / "docs" / "PLAN.md").write_text(
        "[d](DESIGN.md) [d2](DESIGN.md#goals) [web](https://example.com) [top](#top) "
        "[mail](mailto:x@y.z) [gone](missing.md) [up](../README.md)\n")
    assert broken_links(tmp_path, ["docs/PLAN.md", "docs/absent.md", "notes.txt"]) == [
        "docs/PLAN.md: missing.md", "docs/PLAN.md: ../README.md"]


def test_results_fail_when_nothing_changed_or_a_link_is_broken(tmp_path):
    none_changed, links_ok = doc_check_results(tmp_path, [])
    assert none_changed["exit"] == 1 and links_ok["exit"] == 0
    (tmp_path / "P.md").write_text("[x](nope.md)\n")
    changed, links = doc_check_results(tmp_path, ["P.md"])
    assert changed["exit"] == 0 and links["exit"] == 1 and "nope.md" in links["tail"]
