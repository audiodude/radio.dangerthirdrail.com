import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import validate
from test_assemble import _bake


def test_validate_flags_wrong_duration_and_missing_vods(tmp_path):
    p, set_dir = _bake(tmp_path)
    p["vods"][0]["dur"] = 999.0
    (set_dir / "plan2.json").write_text(json.dumps(p))
    (set_dir / p["vods"][1]["file"]).unlink()
    problems = validate.validate(str(set_dir / "plan2.json"), str(set_dir))
    assert any(p["vods"][0]["file"] in x and "duration" in x for x in problems)
    assert any(p["vods"][1]["file"] in x and "missing" in x for x in problems)
