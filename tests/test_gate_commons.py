from ultra_memory.maintenance import gate_commons as gc


def test_is_enabled_default_on_unset_is_enabled():
    assert gc.is_enabled_default_on("X", {}) is True            # unset ⇒ ON (opt-out)


def test_is_enabled_default_on_explicit_disable_values():
    for v in ("0", "false", "False", "no", "off", "OFF"):
        assert gc.is_enabled_default_on("X", {"X": v}) is False


def test_is_enabled_default_on_other_values_stay_enabled():
    for v in ("1", "true", "yes", "", "   ", "anything"):
        assert gc.is_enabled_default_on("X", {"X": v}) is True
