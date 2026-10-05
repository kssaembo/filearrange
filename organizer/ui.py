import json
import os
import subprocess
import threading
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QThread, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QTableView, QHeaderView, QAbstractItemView,
    QFileDialog, QMessageBox, QDialog, QFormLayout, QDialogButtonBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QStyledItemDelegate, QMenu, QTextEdit,
    QProgressBar, QInputDialog)

from .storage import Store
from .filesystem import HOLD, EXCLUDE, scan, protected, plan_moves, execute, undo
from .ai import Gemini, analyze
from .secrets import load_key, save_key


class Worker(QThread):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel = threading.Event()

    def run(self):
        try:
            self.result.emit(self.fn(self.cancel, self.progress.emit))
        except Exception as e:
            self.error.emit(str(e))


class FileModel(QAbstractTableModel):
    changed = Signal()
    headers = ['선택', '파일명', '확장자', '현재 위치', 'AI 추천', '신뢰도*', '최종 카테고리 ▾', '상태']

    def __init__(self, store):
        super().__init__()
        self.rows, self.names, self.store = [], [], store

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 8

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        if role == Qt.DisplayRole:
            return section + 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], index.column()
        if role == Qt.CheckStateRole and col == 0:
            return Qt.Checked if row.selected else Qt.Unchecked
        if role == Qt.ToolTipRole:
            return row.reason or str(row.path)
        if role == Qt.ForegroundRole and col == 5 and row.confidence is not None:
            return QColor('#ae4c21' if row.confidence < .6 else '#947019' if row.confidence < .8 else '#344054')
        if role in (Qt.DisplayRole, Qt.EditRole):
            return ['', row.path.name, row.path.suffix, str(row.path.parent), row.ai,
                '' if row.confidence is None else f'{row.confidence:.0%}', row.final, row.status][col]
        if role == Qt.UserRole:
            return row.confidence if col == 5 and row.confidence is not None else str(self.data(index) or '')

    def flags(self, index):
        flags = super().flags(index)
        row = self.rows[index.row()]
        if row.status == '이동 완료' or row.permanent:
            return flags
        if index.column() == 0:
            flags |= Qt.ItemIsUserCheckable
        if index.column() == 6:
            flags |= Qt.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.EditRole):
        row = self.rows[index.row()]
        if row.permanent or row.status == '이동 완료':
            return False
        if index.column() == 0 and role == Qt.CheckStateRole:
            row.selected = value == Qt.Checked or value == Qt.Checked.value
        elif index.column() == 6 and role == Qt.EditRole:
            if value not in self.names + [HOLD, EXCLUDE]:
                return False
            row.final, row.manual = value, True
            row.selected = value not in (HOLD, EXCLUDE)
            row.status = '제외' if value == EXCLUDE else '판단보류' if value == HOLD else '직접 지정'
            # "이번만 제외" must not persist to a later scan/session.
            if value != EXCLUDE:
                self.store.put(row.key, value, 'choices')
        else:
            return False
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), 7))
        self.changed.emit()
        return True

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()
        self.changed.emit()

    def refresh(self):
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows)-1, 7))
        self.changed.emit()


class Filter(QSortFilterProxyModel):
    mode, category, extension, search = '전체', '', '', ''

    def filterAcceptsRow(self, source_row, parent):
        row = self.sourceModel().rows[source_row]
        if self.search.casefold() not in row.path.name.casefold():
            return False
        if self.category and row.final != self.category:
            return False
        if self.extension == '__NO_EXTENSION__':
            if row.path.suffix:
                return False
        elif self.extension and row.path.suffix.casefold() != self.extension.casefold():
            return False
        if self.mode == '선택된 파일' and not row.selected:
            return False
        if self.mode == '제외된 파일' and row.final != EXCLUDE and row.selected:
            return False
        if self.mode == '낮은 신뢰도' and not (row.confidence is not None and row.confidence < .6):
            return False
        return True


class CategoryDelegate(QStyledItemDelegate):
    def __init__(self, model, parent):
        super().__init__(parent)
        self.files = model

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.addItems(self.files.names + [EXCLUDE, HOLD])
        box.activated.connect(lambda: self.commitData.emit(box))
        return box

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data(Qt.EditRole))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


class CategoriesDialog(QDialog):
    def __init__(self, categories, parent):
        super().__init__(parent)
        self.setWindowTitle('카테고리와 이동 폴더')
        self.resize(820, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('등록된 카테고리만 AI에 전달합니다. 목적지 폴더는 AI에 전송하지 않습니다.'))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(['카테고리 이름', '이동할 폴더 (더블클릭하여 수정)'])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.table)
        for c in categories:
            self.add(c['name'], c['path'])
        buttons = QHBoxLayout()
        for label, fn in [('+ 추가', lambda: self.add()), ('폴더 선택', self.folder), ('삭제', self.remove),
                          ('위로 ↑', lambda: self.reorder(-1)), ('아래로 ↓', lambda: self.reorder(1))]:
            button = QPushButton(label)
            button.clicked.connect(fn)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        save = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save.accepted.connect(self.validate)
        save.rejected.connect(self.reject)
        layout.addWidget(save)

    def add(self, name='', path=''):
        i = self.table.rowCount()
        self.table.insertRow(i)
        self.table.setItem(i, 0, QTableWidgetItem(name))
        self.table.setItem(i, 1, QTableWidgetItem(path))
        self.table.selectRow(i)

    def folder(self):
        i = self.table.currentRow()
        if i >= 0:
            path = QFileDialog.getExistingDirectory(self, '이동할 폴더 선택')
            if path:
                self.table.item(i, 1).setText(path)

    def remove(self):
        if self.table.currentRow() >= 0:
            self.table.removeRow(self.table.currentRow())

    def reorder(self, delta):
        i, j = self.table.currentRow(), self.table.currentRow() + delta
        if 0 <= i < self.table.rowCount() and 0 <= j < self.table.rowCount():
            for col in range(2):
                a, b = self.table.takeItem(i, col), self.table.takeItem(j, col)
                self.table.setItem(i, col, b)
                self.table.setItem(j, col, a)
            self.table.selectRow(j)

    def validate(self):
        result, seen = [], set()
        for i in range(self.table.rowCount()):
            name, path = [self.table.item(i, j).text().strip() for j in range(2)]
            if not name or name in seen or name in (HOLD, EXCLUDE) or not path or not Path(path).is_absolute():
                QMessageBox.warning(self, '설정 확인', '이름은 중복 없이 입력하고, 절대 경로를 지정하세요. 제외/판단보류는 예약된 이름입니다.')
                return
            if protected(path):
                QMessageBox.warning(self, '보호 영역', '시스템/앱 데이터 폴더는 목적지로 사용할 수 없습니다.')
                return
            seen.add(name)
            result.append({'name': name, 'path': str(Path(path).resolve())})
        self.categories = result
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, store, key, parent):
        super().__init__(parent)
        self.store, self.worker = store, None
        self.setWindowTitle('Gemini 설정')
        self.resize(560, 320)
        form = QFormLayout(self)
        self.key = QLineEdit(key)
        self.key.setEchoMode(QLineEdit.Password)
        self.model = QLineEdit(store.get('model', ''))
        self.model.setPlaceholderText('Google AI Studio에서 사용 가능한 모델명 입력')
        self.persist = QCheckBox('이 PC의 Windows 사용자 계정으로 암호화하여 저장')
        self.persist.setChecked(os.name == 'nt')
        self.persist.setEnabled(os.name == 'nt')
        self.batch = QSpinBox()
        self.batch.setRange(100, 300)
        self.batch.setValue(store.get('batch_size', 100))
        form.addRow('API Key', self.key)
        form.addRow('모델명', self.model)
        form.addRow('배치 크기', self.batch)
        form.addRow(self.persist)
        note = QLabel('파일명·확장자·임시 ID·카테고리명만 전송합니다.\n파일명 자체에 개인정보가 포함될 수 있습니다.\n암호화 저장도 현재 계정의 악성코드로부터 키를 완전히 보호하지는 못합니다.')
        note.setWordWrap(True)
        form.addRow(note)
        self.test_button = QPushButton('연결 테스트 (가상 파일 1개)')
        self.test_button.clicked.connect(self.test)
        form.addRow(self.test_button)
        self.state = QLabel('')
        form.addRow(self.state)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)

    def test(self):
        try:
            client = Gemini(self.key.text(), self.model.text().strip())
        except Exception as e:
            self.state.setText(str(e))
            return
        self.test_button.setEnabled(False)
        self.buttons.setEnabled(False)
        self.state.setText('연결 확인 중…')
        self.worker = Worker(lambda cancel, progress: client.classify(
            [{'id': 0, 'filename': '연결테스트.txt', 'extension': '.txt'}], ['기타'], cancel), self)
        self.worker.result.connect(lambda _: self.state.setText('연결 성공 · JSON 응답 검증 완료'))
        self.worker.error.connect(self.state.setText)
        self.worker.finished.connect(lambda: (self.test_button.setEnabled(True), self.buttons.setEnabled(True)))
        self.worker.start()

    def save(self):
        try:
            Gemini(self.key.text(), self.model.text().strip())
            if self.persist.isChecked():
                save_key(self.store, self.key.text().strip())
            else:
                self.store.put('api_key_dpapi', '')
            self.store.put('model', self.model.text().strip())
            self.store.put('batch_size', self.batch.value())
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, '저장 실패', str(e))

    def reject(self):
        if self.worker and self.worker.isRunning():
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, store=None):
        super().__init__()
        self.store = store or Store()
        self.categories = self.store.get('categories', [])
        self.worker, self.api_blocked = None, False
        try:
            self.key = load_key(self.store)
        except Exception:
            self.key = ''
        self.setWindowTitle('파일 정리 도우미 · AI 추천, 내가 결정')
        self.resize(*self.store.get('window_size', [1320, 820]))
        position = self.store.get('window_pos')
        if position and any(screen.availableGeometry().contains(position[0], position[1]) for screen in QApplication.screens()):
            self.move(*position)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 20, 22, 18)
        title = QLabel('파일 정리 도우미')
        title.setStyleSheet('font-size:24px;font-weight:700;')
        layout.addWidget(title)
        layout.addWidget(QLabel('① 폴더 선택   →   ② AI 추천   →   ③ 수정·제외   →   ④ 승인 후 이동'))
        top = QHBoxLayout()
        self.folder = QLineEdit(self.store.get('source', ''))
        self.folder.setReadOnly(True)
        self.folder.setPlaceholderText('정리할 폴더를 선택하세요')
        self.pick = QPushButton('폴더 선택')
        self.pick.clicked.connect(self.choose_folder)
        top.addWidget(self.folder, 1)
        top.addWidget(self.pick)
        layout.addLayout(top)
        options = QHBoxLayout()
        self.recursive, self.hidden, self.system = QCheckBox('하위 폴더 포함'), QCheckBox('숨김 파일 포함'), QCheckBox('시스템 파일 제외')
        self.recursive.setChecked(self.store.get('recursive', True))
        self.hidden.setChecked(self.store.get('hidden', False))
        self.system.setChecked(self.store.get('system', True))
        for control in (self.recursive, self.hidden, self.system):
            options.addWidget(control)
            control.toggled.connect(self.invalidate_scan)
        options.addStretch()
        options.addWidget(QLabel('※ 목적지 하위 폴더·링크·시스템 주요 영역은 스캔 제외'))
        layout.addLayout(options)
        actions = QHBoxLayout()
        self.analyze_button = QPushButton('AI 분석 시작')
        self.refresh_button = QPushButton('새로고침')
        self.category_button = QPushButton('카테고리·이동 폴더')
        self.settings_button = QPushButton('설정')
        for button, fn in [(self.analyze_button, self.start_analysis), (self.refresh_button, self.start_scan),
                           (self.category_button, self.edit_categories), (self.settings_button, self.settings)]:
            button.clicked.connect(fn)
            actions.addWidget(button)
        actions.addStretch()
        self.log_button = QPushButton('이동 로그')
        self.log_button.clicked.connect(self.show_log)
        actions.addWidget(self.log_button)
        layout.addLayout(actions)
        self.summary = QLabel('폴더와 카테고리를 설정해 주세요.')
        self.summary.setStyleSheet('background:#eaf0f6;padding:12px;font-weight:600;')
        layout.addWidget(self.summary)
        self.category_summary = QLabel('')
        self.category_summary.setWordWrap(True)
        layout.addWidget(self.category_summary)
        filters = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(['전체', '선택된 파일', '제외된 파일', '낮은 신뢰도'])
        self.category_filter, self.extension_filter = QComboBox(), QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText('파일명 검색…')
        for widget in (self.mode, self.category_filter, self.extension_filter, self.search):
            filters.addWidget(widget)
        layout.addLayout(filters)
        self.model = FileModel(self.store)
        self.model.names = [c['name'] for c in self.categories]
        self.proxy = Filter(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.SelectedClicked | QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setItemDelegateForColumn(6, CategoryDelegate(self.model, self.table))
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for i, width in enumerate([48, 300, 75, 210, 120, 78, 150, 100]):
            self.table.setColumnWidth(i, width)
        self.table.doubleClicked.connect(self.open_double)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        layout.addWidget(self.table, 1)
        bulk = QHBoxLayout()
        self.check_all = QPushButton('보이는 파일 모두 체크')
        self.uncheck_all = QPushButton('보이는 파일 체크 해제')
        self.bulk_category = QPushButton('선택 행 분류 변경')
        self.exclude_button = QPushButton('선택 행 이번만 제외')
        self.permanent_button = QPushButton('선택 행 항상 제외')
        for button, fn in [(self.check_all, lambda: self.check_visible(True)), (self.uncheck_all, lambda: self.check_visible(False)),
                           (self.bulk_category, self.bulk_change), (self.exclude_button, lambda: self.exclude(False)),
                           (self.permanent_button, lambda: self.exclude(True))]:
            button.clicked.connect(fn)
            bulk.addWidget(button)
        layout.addLayout(bulk)
        hint = QLabel('행 선택: Ctrl/Shift · 분류 수정: 최종 카테고리 셀 클릭 · *신뢰도는 AI의 추정값입니다.\n필터는 보기만 바꿉니다. 정리 실행은 숨겨진 행을 포함한 전체 체크 상태를 기준으로 합니다.')
        hint.setStyleSheet('color:#667085;')
        layout.addWidget(hint)
        bottom = QHBoxLayout()
        self.status = QLabel('준비')
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(140)
        self.progress.hide()
        self.cancel_button = QPushButton('작업 중지')
        self.cancel_button.clicked.connect(self.cancel_work)
        self.cancel_button.hide()
        self.undo_button = QPushButton('마지막 정리 취소')
        self.undo_button.clicked.connect(self.start_undo)
        self.move_button = QPushButton('정리 실행 · 최종 확인')
        self.move_button.setStyleSheet('background:#275b83;color:white;font-weight:700;padding:10px 20px;')
        self.move_button.clicked.connect(self.start_move)
        bottom.addWidget(self.status, 1)
        for widget in (self.progress, self.cancel_button, self.undo_button, self.move_button):
            bottom.addWidget(widget)
        layout.addLayout(bottom)
        self.busy_controls = [self.pick, self.analyze_button, self.refresh_button, self.category_button,
            self.settings_button, self.table, self.move_button, self.undo_button, self.check_all,
            self.uncheck_all, self.bulk_category, self.exclude_button, self.permanent_button,
            self.recursive, self.hidden, self.system, self.log_button]
        self.model.changed.connect(self.update_summary)
        self.mode.currentTextChanged.connect(self.filter_changed)
        self.category_filter.currentTextChanged.connect(self.filter_changed)
        self.extension_filter.currentTextChanged.connect(self.filter_changed)
        self.search.textChanged.connect(self.filter_changed)
        self.refresh_filters()
        self.update_summary()
        if any(r['state'] in ('pending','undo_pending','uncertain') for r in self.store.records()):
            self.status.setText('중단된 이동 기록이 있습니다. 이동 로그와 마지막 정리 취소를 확인하세요.')

    def info(self, title, text):
        QMessageBox.information(self, title, text)

    def details(self, title, text, confirm=False):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(850, 520)
        layout = QVBoxLayout(dialog)
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel if confirm else QDialogButtonBox.Close)
        if confirm:
            buttons.button(QDialogButtonBox.Ok).setText('위 파일 이동 승인')
            buttons.button(QDialogButtonBox.Cancel).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted

    def run_work(self, fn, callback, on_error=None):
        if self.worker and self.worker.isRunning():
            return
        for w in self.busy_controls:
            w.setEnabled(False)
        self.progress.show()
        self.cancel_button.show()
        self.worker = Worker(fn, self)
        self.worker.progress.connect(self.status.setText)
        self.worker.result.connect(callback)
        def failed(message):
            if on_error:
                on_error(message)
            self.status.setText(message)
            QMessageBox.warning(self, '작업 안내', message)
        self.worker.error.connect(failed)
        self.worker.finished.connect(self.work_finished)
        self.worker.start()

    def work_finished(self):
        for w in self.busy_controls:
            w.setEnabled(True)
        self.progress.hide()
        self.cancel_button.hide()
        self.update_summary()

    def cancel_work(self):
        if self.worker:
            self.worker.cancel.set()
            self.status.setText('현재 요청/파일 처리가 끝난 뒤 중지합니다…')

    def invalidate_scan(self):
        self.model.replace([])
        self.status.setText('스캔 옵션이 바뀌었습니다. 새로고침하세요.')

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, '정리할 폴더 선택', self.folder.text())
        if path:
            if protected(path):
                QMessageBox.warning(self, '시스템 영역 경고', 'Windows, Program Files, AppData 등의 주요 영역은 안전을 위해 정리할 수 없습니다.')
                return
            self.folder.setText(path)
            self.store.put('source', path)
            self.start_scan()

    def start_scan(self):
        if not self.folder.text():
            self.choose_folder()
            return
        args = (self.folder.text(), self.recursive.isChecked(), self.hidden.isChecked(), self.system.isChecked(),
                [c['path'] for c in self.categories], self.store)
        for name, value in zip(['recursive', 'hidden', 'system'], args[1:4]):
            self.store.put(name, value)
        self.model.replace([])
        def done(result):
            rows, warnings = result
            for r in rows:
                if r.manual and r.final not in self.model.names + [HOLD, EXCLUDE]:
                    r.final, r.selected, r.status = HOLD, False, '카테고리 재지정 필요'
            self.model.replace(rows)
            self.api_blocked = False
            self.refresh_filters()
            self.status.setText(f'{len(rows):,}개 파일을 불러왔습니다.')
            if warnings:
                self.details('읽지 못한 경로 (나머지 스캔 완료)', '\n'.join(warnings))
        self.run_work(lambda cancel, progress: scan(*args, cancel, progress), done)

    def edit_categories(self):
        dialog = CategoriesDialog(self.categories, self)
        if dialog.exec() == QDialog.Accepted:
            self.categories = dialog.categories
            self.store.put('categories', self.categories)
            self.model.names = [c['name'] for c in self.categories]
            self.model.replace([])
            self.refresh_filters()
            self.status.setText('카테고리 설정을 저장했습니다. 새로고침 후 분석하세요.')

    def settings(self):
        dialog = SettingsDialog(self.store, self.key, self)
        if dialog.exec() == QDialog.Accepted:
            self.key = dialog.key.text().strip()

    def start_analysis(self, checked=False, subset=None, force=False):
        if not self.model.rows or not self.categories:
            self.info('분석 준비', '폴더를 불러오고 카테고리를 등록하세요.')
            return
        try:
            client = Gemini(self.key, self.store.get('model', ''))
        except Exception as e:
            self.info('설정 필요', str(e))
            self.settings()
            return
        rows = subset if subset is not None else self.model.rows[:]
        targets = [r for r in rows if not r.manual and not r.permanent and r.final != EXCLUDE and r.status != '이동 완료']
        if not targets:
            self.info('분석 대상 없음', '직접 지정·제외·이동 완료 파일은 재분석하지 않습니다.')
            return
        def done(output):
            for r in targets:
                if r.key in output:
                    item = output[r.key]
                    r.ai, r.confidence, r.reason = item['category'], item['confidence'], item['reason']
                    r.final = r.ai if r.confidence >= .6 else HOLD
                    r.selected = r.final in self.model.names
                    r.status = '추천 완료' if r.selected else '확인 필요'
            self.api_blocked = any(r.status == '분석 실패' for r in self.model.rows)
            self.model.refresh()
            self.status.setText('분석 완료. 추천을 검토한 뒤 정리 실행을 누르세요.')
        def failed(_):
            self.api_blocked = True
            for r in targets:
                r.status, r.selected = '분석 실패', False
            self.model.refresh()
        self.run_work(lambda cancel, progress: analyze(targets, self.categories, self.store, client,
            self.store.get('batch_size', 100), cancel, progress, force), done, failed)

    def selected_rows(self):
        return sorted({self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows()})

    def bulk_change(self):
        indices = self.selected_rows()
        if not indices:
            self.info('행 선택', '표에서 행을 선택하세요. Ctrl/Shift로 여러 행을 선택할 수 있습니다.')
            return
        category, ok = QInputDialog.getItem(self, '선택 행 분류 변경', f'{len(indices)}개 행의 최종 카테고리', self.model.names + [HOLD, EXCLUDE], 0, False)
        if ok:
            for i in indices:
                self.model.setData(self.model.index(i, 6), category)

    def exclude(self, permanent=False):
        indices = self.selected_rows()
        if permanent and indices:
            if QMessageBox.question(self, '항상 제외', f'{len(indices)}개 파일을 이후에도 제외할까요?\n파일명·경로·크기·수정일이 바뀌면 새 파일로 인식합니다.') != QMessageBox.Yes:
                return
        for i in indices:
            r = self.model.rows[i]
            if r.status == '이동 완료':
                continue
            self.model.setData(self.model.index(i, 6), EXCLUDE)
            if permanent:
                r.permanent, r.status = True, '항상 제외'
                self.store.exclude(r.key, r.path)
        self.model.refresh()

    def check_visible(self, checked):
        indices = [self.proxy.mapToSource(self.proxy.index(i, 0)).row() for i in range(self.proxy.rowCount())]
        for i in indices:
            self.model.setData(self.model.index(i, 0), Qt.Checked if checked else Qt.Unchecked, Qt.CheckStateRole)

    def filter_changed(self, *_):
        self.proxy.mode = self.mode.currentText()
        self.proxy.category = self.category_filter.currentData() or ''
        self.proxy.extension = self.extension_filter.currentData() or ''
        self.proxy.search = self.search.text()
        self.proxy.invalidate()

    def refresh_filters(self):
        self.category_filter.clear()
        self.category_filter.addItem('모든 카테고리', '')
        for name in self.model.names + [HOLD, EXCLUDE]:
            self.category_filter.addItem(name, name)
        self.extension_filter.clear()
        self.extension_filter.addItem('모든 확장자', '')
        for ext in sorted({r.path.suffix for r in self.model.rows}):
            self.extension_filter.addItem(ext or '(확장자 없음)', ext or '__NO_EXTENSION__')

    def update_summary(self):
        rows = self.model.rows
        eligible = [r for r in rows if r.selected and not r.permanent and r.final in self.model.names
                    and (r.manual or r.ai) and r.status not in ('이동 완료','이동 실패','분석 실패')]
        counts = Counter(r.final for r in eligible)
        self.summary.setText(f'전체 {len(rows):,}개    ·    AI 분석 {sum(bool(r.ai) for r in rows):,}개    ·    낮은 신뢰도 {sum(r.confidence is not None and r.confidence < .6 for r in rows):,}개    ·    제외 {sum(r.final == EXCLUDE for r in rows):,}개    ·    이동 후보 {len(eligible):,}개')
        self.category_summary.setText('이동 후보  |  ' + ('   ·   '.join(f'{k} {v}개' for k, v in counts.items()) or '없음'))
        self.move_button.setEnabled(bool(eligible) and not self.api_blocked and not (self.worker and self.worker.isRunning()))
        self.proxy.invalidate()

    def open_double(self, index):
        if index.column() not in (0, 6):
            r = self.model.rows[self.proxy.mapToSource(index).row()]
            self.open_file(r)

    def open_file(self, row):
        if not row.path.exists():
            self.info('파일 없음', '이동되었거나 삭제된 파일입니다. 새로고침하세요.')
            return
        if row.path.suffix.lower() in ('.exe','.bat','.cmd','.ps1','.vbs','.js','.msi','.scr','.com','.lnk'):
            if QMessageBox.question(self, '실행 파일 열기', '이 파일은 프로그램이나 명령을 실행할 수 있습니다. 열까요?') != QMessageBox.Yes:
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(row.path)))

    def context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        if not self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.table.selectRow(index.row())
        row = self.model.rows[self.proxy.mapToSource(index).row()]
        menu = QMenu(self)
        def location():
            if os.name == 'nt':
                subprocess.Popen(['explorer.exe', '/select,', str(row.path)])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(row.path.parent)))
        for label, fn in [('원본 파일 열기', lambda: self.open_file(row)), ('탐색기에서 위치 열기', location),
                          ('해당 파일 다시 분석', lambda: self.start_analysis(subset=[row], force=True)),
                          ('선택 행 카테고리 변경', self.bulk_change), ('이번만 제외', lambda: self.exclude(False)),
                          ('항상 제외', lambda: self.exclude(True))]:
            menu.addAction(label, fn)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def start_move(self):
        if self.api_blocked:
            self.info('이동 잠금', 'API 오류가 있습니다. 재분석에 성공하거나 새로고침 후 검토하세요.')
            return
        try:
            plans = plan_moves(self.model.rows, self.categories)
        except Exception as e:
            self.info('이동 준비 실패', str(e))
            return
        if not plans:
            self.info('이동 대상 없음', '체크 상태와 최종 카테고리를 확인하세요. 이미 목적지에 있는 파일은 이동하지 않습니다.')
            return
        counts = Counter(p.row.final for p in plans)
        text = f'실제 이동 {len(plans)}개 · 이름 충돌 {sum(p.renamed for p in plans)}개\n'
        text += '\n'.join(f'• {k}: {v}개' for k, v in counts.items())
        text += '\n\n아래 경로대로 이동합니다. 덮어쓰기는 하지 않습니다.\n'
        text += '\n\n'.join(f'{p.row.path}\n→ {p.target}' + (' [번호 추가]' if p.renamed else '') for p in plans)
        if not self.details('최종 이동 승인', text, True):
            return
        def done(results):
            by_key = {r.key: r for r in self.model.rows}
            for key, state, detail in results:
                by_key[key].status, by_key[key].reason, by_key[key].selected = state, detail, False
            self.model.refresh()
            self.status.setText(f'이동 완료 {sum(s == "이동 완료" for _, s, _ in results)}개 / 처리 {len(results)}개')
            self.details('이동 결과', '\n'.join(f'{s}: {by_key[k].path.name}\n{d}' for k, s, d in results) or '이동 전에 중지되었습니다.')
        self.run_work(lambda cancel, progress: execute(plans, self.store, cancel, progress), done)

    def start_undo(self):
        if self.store.last_job() is None:
            self.info('실행 취소', '취소할 작업이 없습니다.')
            return
        if QMessageBox.question(self, '마지막 정리 취소', '마지막 작업의 파일을 원래 폴더로 복원할까요?\n동일 이름이 있으면 번호를 붙입니다. 수정된 파일은 자동 복원하지 않습니다.') != QMessageBox.Yes:
            return
        def done(messages):
            self.model.replace([])
            self.details('복원 결과', '\n'.join(messages))
            self.status.setText('복원 처리 완료. 새로고침하여 파일을 확인하세요.')
        self.run_work(lambda cancel, progress: undo(self.store, cancel, progress), done)

    def show_log(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('이동 기록 / 항상 제외 관리')
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(json.dumps(self.store.records(), ensure_ascii=False, indent=2))
        layout.addWidget(view)
        clear = QPushButton(f'항상 제외 목록 초기화 ({len(self.store.exclusions())}개)')
        def clear_list():
            if QMessageBox.question(dialog, '제외 해제', '저장된 항상 제외 설정을 모두 해제할까요? 파일은 변경하지 않습니다.') == QMessageBox.Yes:
                self.store.clear_exclusions()
                self.model.replace([])
                dialog.accept()
        clear.clicked.connect(clear_list)
        layout.addWidget(clear)
        unresolved = [r for r in self.store.records() if r['state'] in ('pending', 'uncertain', 'undo_pending')]
        if unresolved:
            resolve = QPushButton('중단 기록 직접 확인 완료 처리…')
            def acknowledge():
                choices = [f"#{r['id']}  {r['src']} → {r['dst']}" for r in unresolved]
                chosen, ok = QInputDialog.getItem(dialog, '중단 기록 선택', '두 경로를 직접 확인한 기록만 선택하세요.', choices, 0, False)
                if not ok:
                    return
                record = unresolved[choices.index(chosen)]
                message = ('원본·목적지·복원 경로를 직접 확인하고 필요한 복구를 완료했습니까?\n'
                           '이 기록은 자동 실행 취소 대상에서 해제됩니다. 파일은 변경하지 않습니다.\n'
                           '기존 이동 기록은 그대로 보존합니다.')
                if QMessageBox.question(dialog, '직접 확인 완료', message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                    self.store.update_move(record['id'], 'acknowledged', error=record['error'] + ' / 사용자가 직접 확인 완료')
                    dialog.accept()
            resolve.clicked.connect(acknowledge)
            layout.addWidget(resolve)
        dialog.exec()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.info('작업 진행 중', '작업 중지를 누르고 현재 처리가 끝난 뒤 종료하세요.')
            event.ignore()
            return
        self.store.put('window_size', [self.width(), self.height()])
        self.store.put('window_pos', [self.x(), self.y()])
        event.accept()


def main():
    import sys
    from PySide6.QtCore import QLockFile
    from .storage import data_dir
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet('''QWidget {font-family:"Malgun Gothic","Noto Sans CJK KR",sans-serif;font-size:12px;}
        QMainWindow {background:#f6f8fb;} QPushButton {padding:7px 12px;}
        QLineEdit,QComboBox {padding:6px;} QTableView {background:white;alternate-background-color:#f7f9fc;gridline-color:#e5e9ef;}
        QHeaderView::section {padding:7px;background:#eef2f6;border:0;border-bottom:1px solid #d5dce5;}
    ''')
    lock = QLockFile(str(data_dir() / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock():
        QMessageBox.warning(None, '이미 실행 중', '파일 정리 도우미가 이미 실행 중입니다.')
        return 1
    window = MainWindow()
    window.show()
    result = app.exec()
    lock.unlock()
    return result
