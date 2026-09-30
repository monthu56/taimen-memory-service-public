"""CLI ``cb redrive-legacy``: видимость пишущего указывается явно (MEM-ADR-019). БД не нужна."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import platform_memory.observations.ingest as ingest_mod
from platform_memory.cli.main import app

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture()
def calls(monkeypatch):
    seen: list[dict] = []

    def fake(settings, **kw):
        seen.append(kw)
        return {
            "stamped": 1,
            "skipped": [{"observation_id": "obs-x", "scope": "workspace:w2"}],
            "candidates": [
                {
                    "observation_id": "obs-1",
                    "source": {"system": "tracker", "stream": "issues", "external_id": "e1"},
                    "scopes": ["workspace:w1"],
                }
            ],
            "processed": 1,
            "statuses": {"processed": 1},
        }

    monkeypatch.setattr(ingest_mod, "redrive_legacy_observations", fake)
    return seen


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--unrestricted", "--namespace-level"],
        ["--writer-scope", "workspace:w1", "--unrestricted"],
    ],
)
def test_visibility_must_be_explicit(calls, args):
    result = runner.invoke(app, ["redrive-legacy", *args])
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--writer-scope", "workspace:w1", "--writer-scope", "principal:p"],
            ["workspace:w1", "principal:p"],
        ),
        (["--unrestricted"], None),
        (["--namespace-level"], []),
    ],
)
def test_visibility_is_passed_through(calls, args, expected):
    result = runner.invoke(
        app, ["redrive-legacy", *args, "--namespace", "bank", "--observation-id", "obs-1"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "writer_scopes": expected,
            "namespace": "bank",
            "observation_ids": ["obs-1"],
            "limit": 500,
            "actor": "operator",
            "dry_run": False,
        }
    ]
    assert "obs-x" in result.output  # пропущенные — видны оператору
    assert "tracker/issues/e1" in result.output  # источник — сверить до заявления


def test_dry_run_and_actor_are_passed_through(calls):
    result = runner.invoke(
        app,
        ["redrive-legacy", "--writer-scope", "workspace:w1", "--dry-run", "--actor", "alice"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["dry_run"] is True and calls[0]["actor"] == "alice"
    assert "DRY-RUN" in result.output and "tracker/issues/e1" in result.output
