"""Application gettext catalogs; Qt standard dialogs are translated separately."""

import gettext
import os
from pathlib import Path

_catalog = gettext.NullTranslations()
_language = "system"


def system_language():
    value = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "en"
    value = value.split(".")[0].split("@")[0]
    return "en" if value in ("C", "POSIX") else value


def set_language(language):
    global _catalog, _language
    _language = system_language() if language == "system" else language
    _catalog = gettext.translation("mypanel", localedir=Path(__file__).parent / "locales", languages=[_language], fallback=True)


def language_names():
    return list(dict.fromkeys([_language, _language.split("_")[0]]))


def _(message):
    return _catalog.gettext(message)
