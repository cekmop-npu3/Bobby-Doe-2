"""Точка входа графического приложения."""

import sys

from PyQt6 import QtWidgets

from frontend import SerialMessenger


def main() -> None:
    """Создаёт Qt-приложение и запускает его цикл обработки событий.

    :return: ``None``. The function exits the process when the event loop ends.
    """
    # Логи настраиваются до создания окна, чтобы ошибки запуска также попали
    # в консоль разработчика.
    # QApplication владеет главным циклом обработки клавиатуры, мыши и сигналов.
    app = QtWidgets.QApplication(sys.argv)
    # Само окно не запускает цикл: оно только описывает интерфейс и соединения.
    window = SerialMessenger()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # Этот блок выполняется только при прямом запуске файла, но не при импорте
    # main.py из тестов или других модулей.
    main()
