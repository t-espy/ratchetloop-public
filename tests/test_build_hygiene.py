"""What every worker's and check's environment turns off, and what counts as a test. The Donchian build
(2026-09-12) found both gaps: a reviewer's `dotnet test` in its read-only sandbox left a shared .NET
build server that broke the next builds, and its 47 xunit tests counted as none."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ratchetloop import result  # not `from … import tests_added`: pytest would collect it as a test
from ratchetloop.checks import check_env
from ratchetloop.worker_proc import worker_env

DOTNET_OFF = {"DOTNET_CLI_USE_MSBUILD_SERVER": "0", "MSBUILDDISABLENODEREUSE": "1",
              "UseSharedCompilation": "false"}


def test_workers_and_checks_start_no_shared_dotnet_build_server(tmp_path):
    for env in (worker_env(gh_dir=str(tmp_path / "gh")), check_env()):
        assert {k: env.get(k) for k in DOTNET_OFF} == DOTNET_OFF
    assert check_env({"MSBUILDDISABLENODEREUSE": "0"})["MSBUILDDISABLENODEREUSE"] == "0"  # task wins


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_csharp_test_attributes_count_one_per_test_method(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "CalcTests.cs").write_text(
        "public class CalcTests\n{\n"
        "    [Fact]\n    public void Adds() {}\n"
        "    [Theory]\n    [InlineData(1)]\n    [InlineData(2)]\n    public void Many(int x) {}\n"
        "    [Test]\n    public void NUnit() {}\n"
        "    [TestMethod]\n    public void MsTest() {}\n"
        "    [TestCase(3)]\n    public void Row(int x) {}\n}\n")
    (repo / "Calc.cs").write_text("public class Calc { /* not a test: [Fact] mid-line */ }\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "tests")
    # Fact, Theory, Test, TestMethod; data rows (InlineData, TestCase) and mid-line text are not tests.
    assert result.tests_added(repo, base, _git(repo, "rev-parse", "HEAD")) == 4
