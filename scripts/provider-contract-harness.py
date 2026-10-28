#!/usr/bin/env python3
"""Run deterministic provider contract tests without external credentials."""

from __future__ import annotations

import sys


def main() -> int:
    import pytest

    tests = [
        "tests/test_adapters.py::test_megafon_contract",
        "tests/test_adapters.py::test_remote_write_failure_policy",
        "tests/test_adapters.py::test_missing_callid_is_unknown",
        "tests/test_adapters.py::test_amo_existing_marker_prevents_second_write",
        "tests/test_adapters.py::test_amo_creates_contact_note",
        "tests/test_adapters.py::test_remote_urls_and_ami_injection_rejected",
        "tests/test_adapters.py::test_ami_real_socket_correlates_response_and_user_event",
    ]
    # The selected tests use only local fixtures. Skip the project's database
    # conftest so this harness never needs a PostgreSQL instance or credentials.
    return pytest.main(["-q", "--noconftest", *tests])


if __name__ == "__main__":
    sys.exit(main())
