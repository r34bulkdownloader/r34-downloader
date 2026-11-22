import sys

from PySide6.QtWidgets import QApplication

from . import log
from .ui import MainWindow


def main() -> int:
    log.setup()
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
