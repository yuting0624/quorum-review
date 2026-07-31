import pytest

from src.pathfilter import PathFilter, matches, parse_patterns

NOISE = [
    "package-lock.json",
    "frontend/pnpm-lock.yaml",
    "go.sum",
    "vendor/github.com/x/y.go",
    "node_modules/react/index.js",
    "dist/bundle.js",
    "web/dist/assets/app.min.js",
    "api/proto/service_pb2.py",
    "src/__snapshots__/App.test.tsx.snap",
    "pkg/testdata/input.json",
    "docs/diagram.png",
]

CODE = [
    "src/app.py",
    "src/providers/vertex.py",
    "web/src/components/Button.tsx",
    "cmd/server/main.go",
    "Makefile",
    "src/distributed/queue.py",  # 'dist' as a prefix, not the build directory
    "app/models.py",
    ".github/workflows/ci.yml",
]


@pytest.mark.parametrize("path", NOISE)
def test_generated_and_vendored_paths_are_excluded(path):
    assert PathFilter().excluded(path), path


@pytest.mark.parametrize("path", CODE)
def test_hand_written_code_is_kept(path):
    assert not PathFilter().excluded(path), path


def test_a_double_star_prefix_matches_at_the_root_too():
    """fnmatch alone requires something before the slash; gitignore does not."""
    assert matches("generated/api.ts", "**/generated/**")
    assert matches("a/b/generated/api.ts", "**/generated/**")


def test_a_bare_name_matches_at_any_depth():
    assert matches("a/b/c/Thumbs.db", "Thumbs.db")
    assert matches("Thumbs.db", "Thumbs.db")


def test_a_trailing_slash_means_the_whole_directory():
    assert matches("legacy/old.py", "legacy/")
    assert not matches("legacy.py", "legacy/")


def test_patterns_come_from_both_commas_and_newlines():
    assert parse_patterns("a/**, b/**\n# a comment\n\nc.py") == ["a/**", "b/**", "c.py"]


def test_extra_patterns_add_to_the_defaults():
    filt = PathFilter(["docs/**"])
    assert filt.excluded("docs/guide.md")
    assert filt.excluded("package-lock.json")  # default still applies
    assert not filt.excluded("src/app.py")


def test_defaults_can_be_turned_off():
    filt = PathFilter(["docs/**"], use_defaults=False)
    assert not filt.excluded("package-lock.json")
    assert filt.excluded("docs/guide.md")


def test_partition_reports_both_sides():
    keep, skip = PathFilter().partition(["src/app.py", "package-lock.json"])
    assert keep == ["src/app.py"]
    assert skip == ["package-lock.json"]


def test_ignore_file_is_read_from_the_checkout(tmp_path):
    (tmp_path / ".quorumignore").write_text("legacy/**\n# comment\n*.tmpl\n")
    filt = PathFilter.build(root=tmp_path)
    assert filt.excluded("legacy/thing.py")
    assert filt.excluded("web/page.tmpl")
    assert not filt.excluded("src/app.py")


def test_a_missing_ignore_file_is_not_an_error(tmp_path):
    assert PathFilter.build(root=tmp_path).excluded("package-lock.json")
