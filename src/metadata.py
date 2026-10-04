"""Load display metadata from a single application data file."""

import json
from pathlib import Path

_data = json.loads((Path(__file__).parent.parent / "data/app.json").read_text(encoding="utf-8"))
APP_NAME = _data["name"]
APP_VERSION = _data["version"]
APP_AUTHOR = _data["author"]
APP_DATE = _data["releaseDate"]
APP_ICON = Path(__file__).parent.parent / "data/myPanel.svg"
