"""Fast in-process unit tests for standalone helper functions -- no subprocess/tmp
project needed, so these run in microseconds and pin down exact matching semantics
that would be slow and indirect to verify only through a full CLI run."""
import pytest


class TestGitignoreMatches:
    def test_unanchored_pattern_matches_at_any_depth(self, cb):
        assert cb.gitignore_matches("a/b/c.log", ["*.log"])
        assert cb.gitignore_matches("c.log", ["*.log"])

    def test_anchored_pattern_matches_only_at_root(self, cb):
        assert cb.gitignore_matches("build.py", ["/build.py"])
        assert not cb.gitignore_matches("src/build.py", ["/build.py"])

    def test_directory_pattern_matches_everything_underneath(self, cb):
        assert cb.gitignore_matches("vendor/pkg/x.py", ["vendor/"])
        assert not cb.gitignore_matches("vendored_thing.py", ["vendor/"])

    def test_negation_reincludes_within_same_pattern_list(self, cb):
        patterns = ["*.log", "!important.log"]
        assert cb.gitignore_matches("debug.log", patterns)
        assert not cb.gitignore_matches("important.log", patterns)


class TestIgnoredByNestedTree:
    def test_empty_tree_never_ignores(self, cb):
        b = cb.GraphBuilder.__new__(cb.GraphBuilder)  # bypass __init__, method is pure
        from pathlib import Path
        assert b._ignored_by(Path("src/a.py"), {}) is False

    def test_root_level_entry_behaves_like_the_old_single_file_check(self, cb):
        from pathlib import Path
        b = cb.GraphBuilder.__new__(cb.GraphBuilder)
        tree = {"": ["vendor/"]}
        assert b._ignored_by(Path("vendor/x.py"), tree) is True
        assert b._ignored_by(Path("src/x.py"), tree) is False

    def test_nested_entry_scoped_to_its_directory_only(self, cb):
        from pathlib import Path
        b = cb.GraphBuilder.__new__(cb.GraphBuilder)
        tree = {"src/module_a": ["generated/"]}
        assert b._ignored_by(Path("src/module_a/generated/x.py"), tree) is True
        assert b._ignored_by(Path("src/module_b/generated/x.py"), tree) is False


class TestRedact:
    @pytest.mark.parametrize("line,leaked", [
        ("password = 'hunter2'", "hunter2"),
        ("api_key=\"sk-abcdef1234567890\"", "sk-abcdef1234567890"),
        ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ])
    def test_common_secret_shapes_are_blanked(self, cb, line, leaked):
        assert leaked not in cb.redact(line)

    def test_ordinary_code_is_left_untouched(self, cb):
        line = "def calculate_total(price, tax_rate):"
        assert cb.redact(line) == line

    # -- v4.4 Phase 5: expanded known-service-prefix patterns -----------------
    # NOTE: the fake token values below are split across string concatenations
    # on purpose -- they are synthetic fixtures, but a contiguous literal would
    # trip GitHub push protection / secret scanners. redact() still receives the
    # fully reassembled string at runtime, so the assertions are unchanged.
    @pytest.mark.parametrize("line,leaked", [
        ("const token = \"ghp_" + "abcdefghij0123456789ABCDEFGHIJ012345\"",
         "ghp_" + "abcdefghij0123456789ABCDEFGHIJ012345"),
        ("PAT = \"github_" + "pat_11ABCDEFG0abcdefghijklmnop1234567890ABCDEFGHIJKLMNOPQRSTUV\"",
         "github_" + "pat_11ABCDEFG0abcdefghijklmnop1234567890ABCDEFGHIJKLMNOPQRSTUV"),
        ("token = \"glpat-" + "abcdefghijklmnopqrst\"", "glpat-" + "abcdefghijklmnopqrst"),
        ("slack = \"xoxb-" + "1234567890-abcdefghijklmnop\"", "xoxb-" + "1234567890-abcdefghijklmnop"),
        ("hook = \"https://hooks." + "slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX\"",
         "hooks." + "slack.com/services/T00000000"),
        ("stripe = \"sk_" + "live_51H8xJ2KZvNq3mR7tY9wA1bC4dE6fG8h\"", "sk_" + "live_51H8xJ2KZvNq3mR7tY9wA1bC4dE6fG8h"),
        ("stripe_test = \"sk_" + "test_51H8xJ2KZvNq3mR7tY9wA1bC4dE6fG8h\"", "sk_" + "test_51H8xJ2KZvNq3mR7tY9wA1bC4dE6fG8h"),
        ("npm = \"npm_" + "abcdefghij0123456789ABCDEFGHIJ012345\"", "npm_" + "abcdefghij0123456789ABCDEFGHIJ012345"),
        ("key = \"-----BEGIN RSA PRIVATE KEY-----\"", "-----BEGIN RSA PRIVATE KEY-----"),
        ("jwt = \"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
         "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\"", "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    ])
    def test_known_service_prefixes_are_blanked(self, cb, line, leaked):
        assert leaked not in cb.redact(line)

    # -- v4.4 Phase 5: generic high-entropy assignment (no keyword, no prefix) -
    def test_generic_high_entropy_value_with_no_keyword_is_redacted(self, cb):
        line = 'x = "kJ8xQ2vN9pL4rT7wY1zA6bC3dF0gH5mK"'
        assert "kJ8xQ2vN9pL4rT7wY1zA6bC3dF0gH5mK" not in cb.redact(line)

    # -- v4.4 Phase 5: the generic detector must NOT fire on realistic non-secret
    # long strings -- this is the more important half of the feature, since a
    # heuristic that redacts hashes/UUIDs/identifiers on every build would make
    # graph.json noticeably less useful for very little real protection gained.
    @pytest.mark.parametrize("line", [
        'uuid_val = "550e8400-e29b-41d4-a716-446655440000"',
        'sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"',
        'name = "someLongVariableNameThatIsNotASecret"',
        'version = "1.2.3-beta.20231001"',
        'msg = "the quick brown fox jumps over the lazy dog"',
    ])
    def test_generic_detector_does_not_false_positive_on_benign_values(self, cb, line):
        assert cb.redact(line) == line
