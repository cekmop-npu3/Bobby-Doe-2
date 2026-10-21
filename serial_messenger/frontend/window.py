"""Graphical interface for the variant 1 COM messenger."""

from __future__ import annotations

from contextlib import ExitStack
from typing import override

from PyQt6 import QtCore, QtGui, QtWidgets

from backend import BAUD_RATES, SerialConnection
# Тип результата передачи содержит байты кадра и представления для статуса.
from backend.frame import FrameTransmission, encode_frame

__all__ = ("SerialMessenger",)


class ImmediateInput(QtWidgets.QPlainTextEdit):
    """Text area that sends printable characters immediately."""

    character_entered = QtCore.pyqtSignal(str)

    def __init__(self) -> None:
        """Create the immediate-transmission input area."""
        super().__init__()

        # Подсказка объясняет режим: символ появляется в поле ввода сразу,
        # а backend сам решает, когда накопился полный пакет для COM-порта.
        self.setPlaceholderText("Type here — every character is sent immediately")
        # История ввода только пополняется: перетаскивание текста отключено.
        self.setAcceptDrops(False)

    def _move_cursor_to_end(self) -> None:
        """Помещает курсор после последнего отображённого символа."""
        cursor = self.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    @override
    def keyPressEvent(
        self,
        event: QtGui.QKeyEvent | None,
    ) -> None:
        """Send entered characters immediately."""
        if event is None:
            return

        # Введённые символы уже попали в буфер передачи, поэтому их нельзя
        # удалять, вырезать или восстанавливать через Undo/Redo.
        if event.key() in (
            QtCore.Qt.Key.Key_Backspace,
            QtCore.Qt.Key.Key_Delete,
        ) or event.matches(QtGui.QKeySequence.StandardKey.Cut) or event.matches(
            QtGui.QKeySequence.StandardKey.Undo
        ) or event.matches(QtGui.QKeySequence.StandardKey.Redo):
            return

        text = event.text()

        # Enter не является частью данных пакета, но остаётся обычным переводом
        # строки в визуальной истории набранного текста.
        is_line_break = event.key() in (
            QtCore.Qt.Key.Key_Return,
            QtCore.Qt.Key.Key_Enter,
        )

        # The input history is append-only: before adding text, a line break
        # or clipboard contents, restore the insertion point to its end.
        if (
            text
            or is_line_break
            or event.matches(QtGui.QKeySequence.StandardKey.Paste)
        ):
            self._move_cursor_to_end()

        if is_line_break:
            super().keyPressEvent(event)
            self.character_entered.emit("\n")
            return

        super().keyPressEvent(event)

        if text and text.isprintable():
            # Сигнал не пишет в порт напрямую: его обрабатывает SerialConnection.
            self.character_entered.emit(text)

    @override
    def mousePressEvent(
        self,
        event: QtGui.QMouseEvent | None,
    ) -> None:
        """Do not leave the insertion point in the middle after a click."""
        if event is None:
            return

        super().mousePressEvent(event)

        # Нельзя позволить пользователю вставлять символы в середину уже
        # отображённой истории: порядок в поле должен совпадать с порядком
        # добавления символов в буфер передатчика.
        self._move_cursor_to_end()

    @override
    def contextMenuEvent(
        self,
        event: QtGui.QContextMenuEvent | None,
    ) -> None:
        """Hide context-menu actions that can alter existing input."""
        # В стандартном меню есть Cut, Undo и другие команды изменения текста.
        if event is not None:
            event.accept()


class SerialMessenger(QtWidgets.QWidget):
    """Present connection controls and serial messaging UI."""

    def __init__(self) -> None:
        """Build the application window."""
        super().__init__()

        # connection существует только после успешного открытия выбранного порта.
        self.connection: SerialConnection | None = None
        # ExitStack гарантированно вызовет __exit__() соединения при закрытии
        # окна, смене скорости или ошибке соединения.
        self._connection_scope = ExitStack()

        # Этот счётчик относится к интерфейсу, а не к размеру байтового буфера.
        self.sent_characters = 0
        # Prevent recursive or repeated QMessageBox calls caused by
        # queued serial signals.
        self._handling_error = False

        self.setWindowTitle("COM Port Messenger")
        self.resize(1200, 900)
        self.setMinimumSize(640, 420)

        # Интерфейс собирается до подключения сигналов: пользователь не сможет
        # вызвать обработчик для ещё не созданного виджета.
        self._create_widgets()
        self._create_layout()
        self._apply_style()
        self._connect_signals()
        self._load_ports()
        self._refresh_state()

        self.input_area.setFocus()

    def _create_widgets(self) -> None:
        """Create controls and text widgets."""
        # Списки порта и скорости существуют отдельно, потому что скорость
        # становится доступна только после выбора конкретного COM-порта.
        self.port_choice = QtWidgets.QComboBox()

        self.baud_rate_choice = QtWidgets.QComboBox()

        for value in BAUD_RATES:
            self.baud_rate_choice.addItem(
                str(value),
                value,
            )

        self.baud_rate_choice.setCurrentIndex(-1)
        self.baud_rate_choice.setEnabled(False)

        # До открытия порта ввод заблокирован: нельзя накопить локальные данные,
        # которые пользователь ошибочно будет считать отправленными.
        self.input_area = ImmediateInput()
        self.input_area.setEnabled(False)
        self.input_area.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        # Поле Output доступно только для чтения, так как его содержимое пришло
        # из успешно декодированных кадров удалённого устройства.
        self.output_area = QtWidgets.QPlainTextEdit()
        self.output_area.setReadOnly(True)
        self.output_area.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        # Шапка находится отдельно от журнала кадров и остаётся видимой при
        # прокрутке. Зарезервированные байты намеренно не перечисляются.
        self.field_names_label = QtWidgets.QLabel(
            "StartFlag FrameType FrameNumber Data Checksum"
        )
        self.field_names_label.setObjectName("fieldNamesLabel")
        self.field_names_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.field_names_label.setWordWrap(True)
        # QTextBrowser понимает HTML: вставленные stuffing-биты отображаются
        # подчёркнутыми тегом <u>0</u> или <u>1</u>.
        self.status_area = QtWidgets.QTextBrowser()
        self.status_area.setReadOnly(True)
        # Если кадр не помещается по ширине, Qt переносит отображение; полоса
        # прокрутки нужна только вертикальная, справа от области состояния.
        self.status_area.setLineWrapMode(
            QtWidgets.QTextEdit.LineWrapMode.WidgetWidth
        )
        self.status_area.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Data — длинная непрерывная строка битов. Разрешаем перенос
        # внутри неё, чтобы она занимала остаток строки после FrameNumber.
        text_option = self.status_area.document().defaultTextOption()
        text_option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAnywhere)
        self.status_area.document().setDefaultTextOption(text_option)
        self.status_area.setOpenExternalLinks(False)
        self.status_area.setMinimumHeight(145)
        self.status_area.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        self.sent_characters_label = QtWidgets.QLabel()
        self.sent_characters_label.setObjectName("stateLabel")

    def _create_layout(self) -> None:
        """Arrange the resizable controls and text areas."""
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(22, 18, 22, 12)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)

        port_box, port_layout = self._section("COM port")
        port_layout.addWidget(self.port_choice)

        baud_rate_box, baud_rate_layout = self._section("Baud rate")
        baud_rate_layout.addWidget(self.baud_rate_choice)

        # Верхняя строка содержит настройки соединения, средняя — текстовый
        # обмен, нижняя — диагностику сформированных пакетов.
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(port_box)
        controls.addWidget(baud_rate_box)
        controls.addStretch()
        layout.addLayout(controls, 0, 0, 1, 2)

        outgoing, outgoing_layout = self._section("Input")
        outgoing_layout.addWidget(self.input_area)

        incoming, incoming_layout = self._section("Output")
        incoming_layout.addWidget(self.output_area)

        layout.addWidget(outgoing, 1, 0)
        layout.addWidget(incoming, 1, 1)
        state, state_layout = self._section("Status")
        state_layout.addWidget(self.field_names_label)
        state_layout.addWidget(self.status_area)
        layout.addWidget(state, 2, 0, 1, 2)
        layout.addWidget(self.sent_characters_label, 3, 0, 1, 2)

        layout.setRowStretch(1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

    @staticmethod
    def _section(
        title: str,
    ) -> tuple[QtWidgets.QGroupBox, QtWidgets.QVBoxLayout]:
        """Создаёт визуальную секцию и сразу возвращает её внутренний layout."""
        section = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QVBoxLayout(section)
        layout.setContentsMargins(10, 18, 10, 10)
        return section, layout

    def _connect_signals(self) -> None:
        """Connect UI signals."""
        # Любое действие пользователя проходит через Qt-сигнал, поэтому GUI
        # остаётся отзывчивым, а чтение самого COM-порта работает в фоне.
        self.port_choice.currentIndexChanged.connect(self._port_selected)

        self.baud_rate_choice.currentIndexChanged.connect(self._change_baud_rate)

        self.input_area.character_entered.connect(self._send_character)

    def _load_ports(self) -> None:
        """Load currently available serial ports."""
        # Здесь используются только системные имена (COM3, COM10 и т. п.),
        # поскольку именно их принимает pyserial при открытии соединения.
        ports = list(SerialConnection.available_ports())

        self.port_choice.addItems(ports)

        self.port_choice.setCurrentIndex(-1)

    def _port_selected(self) -> None:
        """Enable speed selection only after a COM port is selected.

        :return: ``None``.
        """
        if self._handling_error:
            return

        # Пустая строка означает, что пользователь снял выбор и соединение
        # открывать нельзя.
        has_port = bool(self.port_choice.currentText().strip())
        self.baud_rate_choice.setEnabled(has_port)

        if not has_port:
            self.baud_rate_choice.setCurrentIndex(-1)
            return

        self._try_open()

    def _try_open(self) -> None:
        """Open the selected COM port when both choices exist."""
        # Одновременно поддерживается ровно одно активное последовательное
        # соединение: это предотвращает два потока чтения одного порта.
        if self.connection is not None:
            return

        if self._handling_error:
            return

        port_name = self.port_choice.currentText().strip()

        baud_rate = self.baud_rate_choice.currentData()

        if not port_name or baud_rate is None:
            return

        try:
            # Контекстный менеджер создаёт Serial, запускает ReceiverThread и
            # передаёт управление обратно только после успешного открытия.
            connection = self._connection_scope.enter_context(
                SerialConnection(
                    port_name,
                    baud_rate,
                )
            )

        except ConnectionError as error:
            self._show_open_error(str(error))

            return

        self.connection = connection

        connection.received.connect(self._append_received)

        connection.frame_received.connect(self._show_received_frame)

        # В этом же окне отображаем кадр сразу после его успешной записи.
        # Приёмник использует независимый сигнал frame_received выше.
        connection.frame_sent.connect(self._show_frame)

        connection.error.connect(self._show_error)

        self._port_opened()

    def _change_baud_rate(self) -> None:
        """Reconnect the selected port when the baud rate changes.

        :return: ``None``.
        """
        if self._handling_error:
            return

        baud_rate = self.baud_rate_choice.currentData()
        if baud_rate is None:
            return

        if self.connection is not None:
            # Изменение скорости требует переоткрытия порта, чтобы параметры
            # обеих сторон последовательного соединения были единообразны.
            if baud_rate == self.connection.baud_rate:
                return
            self._close_connection()

        self._try_open()

    def _port_opened(self) -> None:
        """Lock the port but allow changing baud rate and enable input."""
        # Имя порта фиксируется, пока идёт обмен; скорость при этом можно
        # изменить, что корректно закроет и заново откроет соединение.
        self.port_choice.setEnabled(False)
        self.input_area.setEnabled(True)
        self.input_area.setFocus()

    def _send_character(
        self,
        character: str,
    ) -> None:
        """Send one entered character."""
        # Окно не формирует кадры само: оно передаёт символ backend-слою.
        connection = self.connection

        if connection is None:
            return

        connection.send_character(character)

        # Считаем именно введённые символы данных, а не количество кадров.
        # Кадр появляется лишь после заполнения 79-байтового поля, поэтому
        # увеличивать счётчик внутри условия ниже было бы неверно. Вставленные
        # bit-stuffing биты сюда не попадают: они добавляются позже в backend.
        # Перенос строки тоже является передаваемым символом и учитывается
        # наравне с буквами и цифрами.
        self.sent_characters += 1
        self._refresh_state()

        # Отображение отправленного кадра выполняет сигнал frame_sent. Это
        # работает и при отправке кадра перед многобайтным UTF-8-символом.

    def _append_received(
        self,
        text: str,
    ) -> None:
        """Append received user data to the output area."""
        # Вставляем текст в конец, чтобы приходящие кадры не меняли уже
        # показанную историю сообщений.
        cursor = self.output_area.textCursor()

        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)

        cursor.insertText(
            text.replace(
                "\r\n",
                "\n",
            ).replace(
                "\r",
                "\n",
            )
        )

        self.output_area.setTextCursor(cursor)

        self.output_area.ensureCursorVisible()

    def _refresh_state(self) -> None:
        """Refresh the status display."""
        self.sent_characters_label.setText(
            f"Sent characters: {self.sent_characters}"
        )

    def _show_frame(self, transmission: FrameTransmission) -> None:
        """Show only the current frame before and after bit stuffing."""
        # Новый кадр заменяет предыдущий. В области остаются только значения
        # полей: первая строка до stuffing, вторая — после него.
        self.status_area.setHtml(
            f'{transmission.before}<br>{transmission.after_html}'
        )
        self.status_area.verticalScrollBar().setValue(
            self.status_area.verticalScrollBar().maximum()
        )

    def _show_received_frame(self, frame: object) -> None:
        """Show the last recognized incoming frame in the same required format."""
        data = getattr(frame, "data", None)
        if isinstance(data, bytes):
            # У принятого Frame уже удалены stuffing-биты. Повторное кодирование
            # здесь нужно только для одинакового отображения в окне статуса.
            self._show_frame(encode_frame(data.decode("utf-8")))

    def _show_open_error(
        self,
        message: str,
    ) -> None:
        """Show an error that occurred before a connection existed."""
        if self._handling_error:
            return

        # Флаг предотвращает повторный показ того же окна ошибки из очереди Qt.
        self._handling_error = True

        try:
            QtWidgets.QMessageBox.critical(
                self,
                "COM Port Messenger",
                message,
            )

        finally:
            self._handling_error = False

    def _show_error(
        self,
        message: str,
    ) -> None:
        """Show one error and safely tear down the active connection."""
        # A queued signal belonging to an already-reset connection
        # should simply be ignored.
        if self.connection is None:
            return

        # Prevent recursive QMessageBoxes while Qt is running the
        # modal dialog's nested event loop.
        if self._handling_error:
            return

        self._handling_error = True

        try:
            # Сначала закрываем соединение и возвращаем интерфейс в исходное
            # состояние, затем показываем сообщение пользователю.
            self._reset_connection_controls()

            QtWidgets.QMessageBox.critical(
                self,
                "COM Port Messenger",
                message,
            )

        finally:
            self._handling_error = False

    def _close_connection(self) -> None:
        """Stop the active connection without changing selector values.

        :return: ``None``.
        """
        connection = self.connection

        # Clear the reference FIRST so any queued error signal from the
        # old connection is ignored by _show_error().
        # Ссылка обнуляется до остановки потока: поздний сигнал старого порта
        # будет проигнорирован обработчиком ошибки.
        self.connection = None

        if connection is not None:
            # Все сигналы отключаются одинаково; TypeError означает, что
            # обработчик уже не был подключён к данному соединению.
            for signal, handler in (
                (connection.received, self._append_received),
                (connection.error, self._show_error),
                (connection.frame_received, self._show_received_frame),
                (connection.frame_sent, self._show_frame),
            ):
                try:
                    signal.disconnect(handler)
                except TypeError:
                    pass

        self._connection_scope.close()
        self._connection_scope = ExitStack()

    def _reset_connection_controls(self) -> None:
        """Close the active connection and unlock cleared selectors.

        :return: ``None``.
        """
        self._close_connection()

        # После ошибки пользователь снова может выбрать порт и скорость.
        self.port_choice.setEnabled(True)

        # Clear the port first. Programmatically changing
        # baud_rate_choice emits currentIndexChanged, but _try_open()
        # then sees no selected COM port and does nothing.
        self.port_choice.setCurrentIndex(-1)

        self.baud_rate_choice.setCurrentIndex(-1)
        self.baud_rate_choice.setEnabled(False)

        self.input_area.setEnabled(False)

    def _apply_style(self) -> None:
        """Apply the application style."""
        # Стили заданы в одном месте, чтобы не смешивать внешний вид с логикой
        # сборки/разбора кадров.
        self.setStyleSheet("""
            QWidget {
                background: #f0f0f0;
                color: #202020;
                font: 14px 'Segoe UI';
            }

            QGroupBox {
                border: 1px solid #c8c8c8;
                margin-top: 9px;
                padding-top: 7px;
            }

            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 3px;
            }

            QComboBox {
                background: white;
                border: 1px solid #a8a8a8;
                padding: 5px 8px;
                min-height: 24px;
            }

            QPlainTextEdit {
                background: white;
                border: 1px solid #303030;
                padding: 4px;
                selection-background-color: #9bbcff;
            }

            QTextBrowser {
                background: white;
                border: 1px solid #303030;
                padding: 4px;
                selection-background-color: #9bbcff;
            }

            """)

    @override
    def closeEvent(
        self,
        event: QtGui.QCloseEvent | None,
    ) -> None:
        """Close timers and the serial connection before exiting."""
        # Останавливаем поток чтения раньше закрытия Qt-приложения, иначе он
        # мог бы обратиться к уже уничтоженным объектам интерфейса.
        self._close_connection()

        if event is not None:
            event.accept()
