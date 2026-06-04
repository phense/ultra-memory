# tests/test_setup_scheduler.py
from ultra_memory import setup


def test_detect_scheduler_platform():
    assert setup.detect_scheduler_platform("darwin") == "launchd"
    assert setup.detect_scheduler_platform("linux") == "systemd"
    assert setup.detect_scheduler_platform("win32") is None     # unsupported → no offer


def test_scheduler_offer_text_is_copy_pasteable():
    txt = setup.scheduler_offer_text("launchd", py="/v/bin/python")
    assert "launchd" in txt.lower()
    assert "/v/bin/python -m ultra_memory.maintenance" in txt
    assert "optional" in txt.lower()                            # never auto-installs


def test_scheduler_offer_text_none_platform_is_empty():
    assert setup.scheduler_offer_text(None, py="/v/bin/python") == ""
