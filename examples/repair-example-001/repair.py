import json
from pathlib import Path
import sys

source = Path("/input/input/broken.json")
output = Path(sys.argv[1]) / "config.json"
data = json.loads(source.read_text(encoding="utf-8"))
data["enabled"] = True
output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
