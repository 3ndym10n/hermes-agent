from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import virgil_mobile_server as mobile


PUBLIC_URL = "https://virgil.example.ts.net:8443"
HEADERS = {
    "host": "virgil.example.ts.net:8443",
    "tailscale-user-login": "cal@example.com",
}


def test_session_exposes_only_compact_operational_state(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    older = (now - timedelta(minutes=10)).isoformat()
    newer = (now - timedelta(minutes=2)).isoformat()
    next_sync = (now + timedelta(minutes=3)).isoformat()
    later_sync = (now + timedelta(minutes=5)).isoformat()
    sources = [
        {
            "source": "calendar",
            "status": "active",
            "message": "Calendar is connected; no events are scheduled in the next seven days.",
            "last_successful_sync_at": older,
            "next_scheduled_sync_at": later_sync,
            "enabled": True,
            "paused": False,
            "error_code": "must-not-leak",
        },
        {
            "source": "system",
            "status": "healthy",
            "message": "System health is normal.",
            "last_successful_sync_at": newer,
            "next_scheduled_sync_at": next_sync,
            "enabled": True,
            "paused": False,
            "failure_count": 0,
        },
        {
            "source": "personal",
            "status": "not_connected",
            "message": "Personal is not connected.",
            "last_successful_sync_at": None,
            "next_scheduled_sync_at": (now + timedelta(minutes=1)).isoformat(),
            "enabled": False,
            "paused": False,
        },
    ]
    brief = {
        "summary": "One item needs you. System health is normal.",
        "needs_you_count": 1,
        "prepared_count": 0,
        "next_calendar_event": None,
        "active_agent_jobs": 0,
        "system_health": "normal",
        "recommended_action": "Review the highest-priority item.",
    }
    monkeypatch.setattr(mobile, "list_source_statuses", lambda **_kwargs: sources)
    monkeypatch.setattr(
        mobile, "available_projects", lambda **_kwargs: ["all", "linxio"]
    )
    monkeypatch.setattr(mobile, "attention_brief", lambda **_kwargs: brief)
    monkeypatch.setattr(mobile, "list_activity", lambda **_kwargs: [])
    monkeypatch.setattr(mobile, "_gmail_watcher_state", lambda: "active")

    app = mobile.create_app(
        "cal@example.com",
        PUBLIC_URL,
        db_path=tmp_path / "attention.db",
        trusted_proxy_hosts=frozenset({"testclient"}),
    )
    data = TestClient(app).get("/api/session", headers=HEADERS).json()

    assert data["projects"] == ["all", "linxio"]
    assert data["brief"] == brief
    assert data["connection"]["last_successful_source_sync_at"] == newer
    assert data["connection"]["next_scheduled_source_sync_at"] == next_sync
    assert data["connection"]["sources"] == [
        {
            "source": source["source"],
            "status": source["status"],
            "message": source["message"],
            "last_successful_sync_at": source["last_successful_sync_at"],
        }
        for source in sources
    ]
