import sys


def main():
    from gi.repository import GLib

    from . import APP_ID, APP_NAME
    # Every window, including file choosers, then reports the same app id.
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    from .app import App
    app = App()
    return app.run(sys.argv) or app.exit_status


if __name__ == "__main__":
    sys.exit(main())
