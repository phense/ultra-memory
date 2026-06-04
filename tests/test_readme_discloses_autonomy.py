import pathlib
RM = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text().lower()

def test_readme_states_on_by_default_and_optout():
    assert "on by default" in RM
    assert "opt out" in RM or "opt-out" in RM or "turn" in RM and "off" in RM

def test_readme_does_not_claim_ships_turned_off():
    assert "ship turned off until you opt in" not in RM
