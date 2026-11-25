"""Local sign-in and account creation dialogs."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from decui.accounts import Account, AccountStore


class AccountDialog(QDialog):
    def __init__(
        self, store: AccountStore, parent: QWidget, *, mode: str,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.mode = mode
        self.account: Account | None = None
        self.setWindowTitle({
            "login": "Вход", "first": "Создание администратора", "create": "Новая учётная запись",
        }[mode])
        self.resize(435, 200)
        form = QFormLayout(self)
        self.login = QLineEdit()
        self.login.setMaxLength(100)
        self.password = QLineEdit()
        self.password.setMaxLength(256)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirmation = QLineEdit()
        self.confirmation.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirmation.setMaxLength(256)
        self.role = QLineEdit()
        self.role.setMaxLength(100)
        self.error = QLabel()
        self.error.setWordWrap(True)
        form.addRow("Логин", self.login)
        form.addRow("Пароль", self.password)
        if mode != "login":
            form.addRow("Повторите пароль", self.confirmation)
        if mode == "create":
            form.addRow("Роль", self.role)
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Войти" if mode == "login" else "Создать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _submit(self) -> None:
        login, password = self.login.text(), self.password.text()
        try:
            if self.mode != "login" and password != self.confirmation.text():
                raise ValueError("Пароли не совпадают.")
            if self.mode == "first":
                account = self.store.create_first_admin(login, password)
            elif self.mode == "create":
                account = self.store.create(login, password, role=self.role.text())
            else:
                account = self.store.authenticate(login, password)
                if account is None:
                    raise ValueError("Неверный логин или пароль.")
        except ValueError as exc:
            self.error.setText(str(exc))
            self.password.clear()
            self.confirmation.clear()
            self.password.setFocus()
            return
        self.account = account
        self.password.clear()
        self.confirmation.clear()
        self.accept()
