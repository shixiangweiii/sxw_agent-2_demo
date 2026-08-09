from pathlib import Path


WEB_APP = Path(__file__).parents[2] / "web" / "app.js"


def test_fresh_page_rebuilds_ui_from_all_committed_events() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    resume = source[source.index("async function resumeStoredRun()") : source.index("async function loadHealth()")]

    assert 'state.lastSeq = 0;' in resume
    assert 'localStorage.setItem("sxw.last_seq", "0");' in resume
    assert 'after_seq: 0' in resume
    assert "await watchRun(assistant);" in resume


def test_committed_assistant_message_is_authoritative_projection() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    handler = source[source.index("function handleSseEvent") : source.index("async function consumeSse")]

    assert 'event.type === "assistant_message"' in handler
    assert 'assistant.body.textContent = payload.text || "";' in handler
