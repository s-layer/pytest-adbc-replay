"""Shared pytest fixtures for pytest_adbc_replay test suite."""

# pytester for the plugin's own tests; ci_skips: under CI, a skipped test fails the run.
pytest_plugins = ["pytester", "tests.ci_skips"]
