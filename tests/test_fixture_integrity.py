"""Guards the benchmark fixture against accidental repair.

Every measurement in `benchmark/seeded-bugs/README.md` assumes ten specific
bugs are present and three specific decoys are not bugs. A well-meaning cleanup
that fixes one of them would silently invalidate every recorded result, so the
answer key is asserted here.

Skipped on `main`, where the seeded files do not exist.
"""

import ast
import pathlib

import pytest

FIXTURE = pathlib.Path(__file__).resolve().parent.parent / "benchmark" / "seeded-bugs"
APP = FIXTURE / "app"

pytestmark = pytest.mark.skipif(
    not (APP / "search.py").exists(),
    reason="seeded bugs live on the benchmark/seeded-bugs-v1 branch",
)


def source(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def test_every_fixture_file_parses():
    """The bugs must be defects, not syntax errors — a model has to read this."""
    for path in APP.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_b1_sql_injection():
    text = source("search.py")
    assert 'documents_fts MATCH \'{term}\'' in text
    assert "owner_id = {user[" in text


def test_b2_path_traversal():
    text = source("export.py")
    assert "os.path.join(user_dir, filename)" in text
    assert "basename" not in text
    assert "realpath" not in text


def test_b3_timing_unsafe_comparison():
    text = source("sharing.py")
    assert "if expected != signature:" in text
    assert "compare_digest" not in text


def test_b4_ssrf():
    text = source("fetcher.py")
    assert "requests.get(url" in text
    assert "urlparse" not in text
    assert "allowlist" not in text.lower()


def test_b5_hardcoded_fallback_secret():
    fallback = 'os.getenv("QUORUM_DEMO_SECRET", "dev-secret-change-me")'
    assert fallback in source("config.py")


def test_b6_missing_authorization_on_delete():
    tree = ast.parse(source("admin.py"))
    delete = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "delete_document"
    )
    body = ast.dump(delete)
    assert "_assert_admin" not in body
    assert "owner_id" not in body


def test_b7_toctou():
    text = source("export.py")
    assert "if os.path.exists(destination):" in text
    assert "time.sleep" in text


def test_b8_mutable_default_argument():
    text = source("sharing.py")
    assert "scopes: list = []" in text
    assert "scopes.append" in text


def test_b9_unbounded_page_size():
    text = source("documents.py")
    assert "MAX_PAGE_SIZE" not in text


def test_b10_swallowed_authorization_failure():
    text = source("admin.py")
    assert "except Exception:\n        pass" in text


# -- context-dependent cases -----------------------------------------------
#
# These are only decidable by reading a file the pull request does not touch.
# Both halves have to hold: the call site in reports.py, and the definition on
# main that makes it safe or broken. Assert both, or a change to either one
# silently turns the case into something else.


def test_c1_the_path_join_is_guarded_by_a_shared_validator():
    """Looks like traversal in the diff; is not. Flagging it is a false positive.

    The validator is called `export_name`, not `safe_export_name`. The original
    name gave the answer away — neither configuration ever flagged this, so the
    decoy discriminated nothing and the "no false positives" claim rested on a
    case that was never really asked.
    """
    call_site = source("reports.py")
    assert "os.path.join(_user_dir(user), validators.export_name(filename))" in (
        call_site
    )

    validator = source("validators.py")
    assert "os.path.basename" in validator
    assert '".."' in validator
    assert "fullmatch" in validator


def test_c2_the_scope_check_passes_for_everyone():
    """Reads correctly; the action is missing from the registry, which fails open."""
    assert 'permissions.has_scope(user, "document.report")' in source("reports.py")

    registry = source("permissions.py")
    assert '"document.report"' not in registry
    # Unlisted actions must still be treated as permitted, or the case is a
    # denial-of-service rather than a bypass.
    assert "if required is None:\n        return True" in registry


def test_c3_the_audit_call_does_not_match_the_signature():
    assert 'audit.record(user, "document.report", doc_id)' in source("reports.py")
    assert (
        "def record(action: str, user_id: int, doc_id: int" in source("audit.py")
    )


def test_c4_the_raw_join_is_guarded_by_its_only_caller():
    """The shape a real false positive takes.

    C1 has the validator call on the changed line, which is a strong "someone
    thought about this" signal — and neither configuration ever flagged it. C4
    removes that signal: the changed lines show a parameter joined into a path
    with nothing guarding it, and the guard is at the HTTP boundary in a file
    the pull request does not touch.

    Flagging it is a false positive. Not flagging it *without having looked* is
    luck rather than judgement, which is why the tool logs say which files were
    opened.
    """
    call_site = source("reports.py")
    assert "def write_named_report(" in call_site
    assert "os.path.join(_user_dir(user), filename)" in call_site
    assert "validators" not in call_site.split("def write_named_report(")[1]

    boundary = source("api.py")
    assert "validators.export_name(filename)" in boundary
    assert "reports.write_named_report(" in boundary


# -- decoys: these must stay correct ---------------------------------------


def test_d1_subprocess_decoy_is_safe():
    text = source("indexer.py")
    assert "shell=False" in text
    assert "shell=True" not in text
    assert "commonpath" in text  # the path is confined before use
    assert "realpath" in text


def test_d2_importlib_decoy_is_safe():
    text = source("plugins.py")
    assert "FORMATTERS.get(name)" in text
    # The argument to import_module is a value from the allowlist, never input.
    assert "import_module(module_path)" in text
    assert "import_module(name)" not in text


def test_d3_random_decoy_is_non_cryptographic():
    text = source("fetcher.py")
    assert "random.uniform" in text
    # Only used for backoff; nothing security-bearing is generated here.
    assert "token" not in text
    assert "secrets" not in text
