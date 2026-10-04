#!/usr/bin/env python3
"""myPanel bootstrap; models, desktop services and views live in modules."""
import sys
from PySide6.QtCore import Qt, QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from config_store import ConfigStore, ConfigError
from desktop_services import GlibPump
from localization import set_language, system_language
from metadata import APP_NAME, APP_VERSION, APP_AUTHOR, APP_ICON

_qt_translators = []


def apply_theme(config):
    scheme = config["preferences"]["theme"]
    QApplication.instance().styleHints().setColorScheme({
        "dark": Qt.ColorScheme.Dark, "light": Qt.ColorScheme.Light,
    }.get(scheme, Qt.ColorScheme.Unknown))


def apply_language(app, config):
    language = config["preferences"]["language"]
    set_language(language)
    locale = QLocale(system_language() if language == "system" else language)
    QLocale.setDefault(locale)
    for translator in _qt_translators:
        app.removeTranslator(translator)
    _qt_translators.clear()
    directory = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    for prefix in ("qtbase", "qt"):
        translator = QTranslator(app)
        if translator.load(locale, prefix, "_", directory):
            app.installTranslator(translator)
            _qt_translators.append(translator)


def main():
    from window import MyPanelWindow
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_AUTHOR)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    store = ConfigStore()
    result = store.load()
    apply_language(app, result.config)
    apply_theme(result.config)
    if store.expected is None and not store.protected:
        try:
            store.save(result.config)
        except ConfigError as error:
            result.notices.append(error.notice)
    pump = GlibPump(app)
    window = MyPanelWindow(store, result)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
