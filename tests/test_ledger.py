"""The ledger is what keeps the reviewer from re-reporting itself, so the
identity rules get direct tests."""

from src import ledger
from src.schema import Finding

SNIPPET = 'query = f"SELECT * FROM docs WHERE id={doc_id}"'


def test_finding_id_ignores_reformatting():
    """Re-indenting or annotating the line must not mint a new finding."""
    plain = ledger.compute_finding_id("app/search.py", SNIPPET)
    reformatted = ledger.compute_finding_id(
        "app/search.py", f"        {SNIPPET}  # FIXME: parameterise"
    )
    assert plain == reformatted


def test_finding_id_depends_on_path():
    assert ledger.compute_finding_id("a.py", SNIPPET) != ledger.compute_finding_id(
        "b.py", SNIPPET
    )


def test_finding_id_has_no_line_number_input():
    """The signature takes no line number, which is the whole point.

    A line-sensitive ID breaks the moment an import is added above the finding.
    """
    import inspect

    params = inspect.signature(ledger.compute_finding_id).parameters
    assert "line" not in params


def test_marker_round_trip():
    original = ledger.Ledger.empty(42)
    original.last_reviewed_sha = "deadbee"
    original.record(
        ledger.LedgerEntry(
            finding_id="abc123",
            file_path="app/search.py",
            category="security",
            severity="high",
            title="SQL injection",
        )
    )

    body = "## Quorum review\n\nsome prose\n\n" + ledger.encode_marker(original)
    restored = ledger.decode_marker(body)

    assert restored is not None
    assert restored.last_reviewed_sha == "deadbee"
    assert restored.entries["abc123"].title == "SQL injection"


def test_marker_round_trip_when_compressed():
    big = ledger.Ledger.empty(1)
    for index in range(400):
        big.record(
            ledger.LedgerEntry(
                finding_id=f"id{index:04d}",
                file_path=f"app/module_{index}.py",
                category="correctness",
                severity="medium",
                title="a finding with a reasonably long title " * 3,
            )
        )

    marker = ledger.encode_marker(big)
    assert ":" in marker and "z:" in marker  # took the gzip branch

    restored = ledger.decode_marker(marker)
    assert restored is not None
    assert len(restored.entries) == 400


def test_corrupt_marker_is_treated_as_absent():
    """A broken marker degrades to a cold start rather than failing the run."""
    assert ledger.decode_marker("<!-- quorum-state: j:notbase64!!! -->") is None
    assert ledger.decode_marker("no marker here") is None


def test_open_and_wontfix_are_suppressed_but_fixed_is_not():
    book = ledger.Ledger.empty(1)
    book.record(ledger.LedgerEntry("open1", "a.py", "security", "high", "t"))
    book.record(
        ledger.LedgerEntry("wf1", "a.py", "security", "high", "t", status="wontfix")
    )
    book.record(
        ledger.LedgerEntry("fx1", "a.py", "security", "high", "t", status="fixed")
    )

    assert book.is_suppressed("open1")
    assert book.is_suppressed("wf1")
    # A fixed issue reappearing is a regression, and worth reporting again.
    assert not book.is_suppressed("fx1")
    assert not book.is_suppressed("never-seen")


def test_wontfix_survives_a_later_run():
    """A human dismissal must outlive the next scan that re-finds the issue."""
    book = ledger.Ledger.empty(1)
    book.record(
        ledger.LedgerEntry(
            "id1",
            "a.py",
            "security",
            "high",
            "t",
            status="wontfix",
            wontfix_reason="intentional",
            first_seen_sha="aaa",
        )
    )

    book.record(ledger.LedgerEntry("id1", "a.py", "security", "high", "t", status="open"))

    assert book.entries["id1"].status == "wontfix"
    assert book.entries["id1"].wontfix_reason == "intentional"
    assert book.entries["id1"].first_seen_sha == "aaa"


def test_fit_to_comment_drops_history_before_exceeding_the_limit():
    book = ledger.Ledger.empty(1)
    for index in range(2000):
        book.record(
            ledger.LedgerEntry(
                finding_id=f"id{index:05d}",
                file_path=f"very/long/path/to/module_number_{index}.py",
                category="correctness",
                severity="low",
                title="x" * 200,
                status="fixed",
            )
        )

    visible = "y" * 4000
    marker = ledger.fit_to_comment(book, visible)
    assert len(visible) + len(marker) <= ledger.COMMENT_CHAR_LIMIT


def test_assign_ids_populates_every_finding():
    findings = [
        Finding("a.py", 10, "security", "high", "t", "b", SNIPPET),
        Finding("b.py", 20, "correctness", "low", "t", "b", "x = 1"),
    ]
    ledger.assign_ids(findings)
    assert all(f.finding_id for f in findings)
    assert findings[0].finding_id != findings[1].finding_id
