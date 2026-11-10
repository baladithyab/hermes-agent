"""Tests for _coalesce_session_name_args — multi-word session name merging."""

from hermes_cli.main import _coalesce_session_name_args


class TestCoalesceSessionNameArgs:
    """Ensure unquoted multi-word session names are merged into one token."""

    # ── -c / --continue ──────────────────────────────────────────────────

    def test_continue_multiword_unquoted(self):
        """hermes -c Pokemon Agent Dev → -c 'Pokemon Agent Dev'"""
        assert _coalesce_session_name_args(
            ["-c", "Pokemon", "Agent", "Dev"]
        ) == ["-c", "Pokemon Agent Dev"]


    # ── -r / --resume ────────────────────────────────────────────────────


    # ── combined flags ───────────────────────────────────────────────────


    def test_option_value_matching_raw_command_does_not_stop_merging(self):
        assert _coalesce_session_name_args(
            ["--in", "cpa", "--resume", "Alpha", "Beta"], raw_commands={"cpa"}
        ) == ["--in", "cpa", "--resume", "Alpha Beta"]

    def test_raw_command_keeps_its_own_resume_arguments(self):
        assert _coalesce_session_name_args(
            ["--resume", "Alpha", "Beta", "cpa", "probe", "-r", "one", "two"],
            raw_commands={"cpa"},
        ) == ["--resume", "Alpha Beta", "cpa", "probe", "-r", "one", "two"]


    # ── subcommand boundary ──────────────────────────────────────────────


    def test_stops_at_setup_subcommand(self):
        """hermes -c my setup → 'setup' is a subcommand, not part of name"""
        assert _coalesce_session_name_args(
            ["-c", "my", "setup"]
        ) == ["-c", "my", "setup"]
