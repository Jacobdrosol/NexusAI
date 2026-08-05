import json

from scripts.summarize_bot_tooling_snapshot import main, summarize_snapshot


def test_summarize_bot_tooling_snapshot_reports_blocked_browser_worker(tmp_path):
    bots_path = tmp_path / "bots.json"
    readiness_path = tmp_path / "readiness.json"
    workers_path = tmp_path / "workers.json"
    probes_path = tmp_path / "probes.json"
    bots_path.write_text(
        json.dumps(
            [
                {
                    "id": "browser-bot",
                    "name": "Browser Bot",
                    "enabled": True,
                    "backends": [{"type": "browser", "provider": "browser", "model": "browser-ui", "worker_id": "worker-1"}],
                    "execution_policy": {"required_worker_tools": ["browser-ui"]},
                },
                {
                    "id": "chat-bot",
                    "name": "Chat Bot",
                    "enabled": True,
                    "backends": [{"type": "cloud_api", "provider": "ollama_cloud", "model": "qwen3.5:cloud"}],
                },
            ]
        ),
        encoding="utf-8",
    )
    readiness_path.write_text(
        json.dumps(
            {
                "readiness": [
                    {
                        "bot_id": "browser-bot",
                        "state": "blocked",
                        "checks": [
                            {
                                "component": "backend[0]",
                                "status": "failed",
                                "message": "Worker 'worker-1' browser runtime is not ready: browser_session_check_failed",
                            }
                        ],
                    },
                    {"bot_id": "chat-bot", "state": "ready", "checks": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    workers_path.write_text(json.dumps([{"id": "worker-1", "status": "online", "enabled": True}]), encoding="utf-8")
    probes_path.write_text(json.dumps({"probes": [{"worker_id": "worker-1", "probe_status": "degraded"}]}), encoding="utf-8")

    status = summarize_snapshot(
        bots_path=bots_path,
        readiness_path=readiness_path,
        workers_path=workers_path,
        worker_probes_path=probes_path,
    )

    assert status["summary"]["total"] == 2
    assert status["summary"]["ready"] == 1
    assert status["summary"]["blocked"] == 1
    assert status["blocked_groups"][0]["category"] == "browser_session"
    assert status["blocked_groups"][0]["recommended_action"]["label"] == "restore browser session"
    assert status["blocked_groups"][0]["bots"][0]["workers"][0]["probe_status"] == "degraded"


def test_summarize_bot_tooling_snapshot_cli_exits_nonzero_for_blockers(tmp_path, capsys):
    bots_path = tmp_path / "bots.json"
    readiness_path = tmp_path / "readiness.json"
    bots_path.write_text(json.dumps([{"id": "blocked-bot", "enabled": True, "backends": []}]), encoding="utf-8")
    readiness_path.write_text(
        json.dumps(
            {
                "readiness": [
                    {
                        "bot_id": "blocked-bot",
                        "state": "blocked",
                        "checks": [{"status": "failed", "message": "Model is unavailable."}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["--bots", str(bots_path), "--readiness", str(readiness_path)])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Bot tooling readiness" in output
    assert "blocked: 1" in output
    assert "fix model route" in output
