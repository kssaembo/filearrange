import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from organizer.ui import MainWindow, CategoriesDialog, SettingsDialog
from organizer.storage import Store
from organizer.filesystem import FileRow, signature, identity, EXCLUDE, HOLD

app=QApplication.instance() or QApplication([])

class UITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.store=Store(self.root/'state.db')
        self.store.put('categories',[{'name':'수업자료','path':str(self.root/'out')}])
        self.window=MainWindow(self.store)
        self.window.show(); app.processEvents()
        rows=[]
        for name in ['체육평가.xlsx','사진.jpg','확장자없음']:
            path=self.root/name; path.write_text('test'); sig=signature(path)
            rows.append(FileRow(path,sig,identity(path,sig)))
        self.window.model.replace(rows); self.window.refresh_filters()
    def tearDown(self):
        self.window.close(); app.processEvents(); self.temp.cleanup()
    def test_manual_category_and_checkbox(self):
        m=self.window.model
        self.assertTrue(m.setData(m.index(0,6),'수업자료'))
        self.assertTrue(m.rows[0].selected)
        self.assertTrue(self.window.move_button.isEnabled())
        m.setData(m.index(0,0),Qt.Unchecked,Qt.CheckStateRole)
        self.assertFalse(m.rows[0].selected)
        self.assertFalse(self.window.move_button.isEnabled())
    def test_search_and_no_extension_filter(self):
        self.window.search.setText('체육'); self.assertEqual(self.window.proxy.rowCount(),1)
        self.window.search.clear()
        self.window.extension_filter.setCurrentIndex(self.window.extension_filter.findData('__NO_EXTENSION__'))
        self.assertEqual(self.window.proxy.rowCount(),1)
    def test_temporary_exclusion_not_persisted(self):
        m=self.window.model; m.setData(m.index(0,6),EXCLUDE)
        self.assertEqual(m.rows[0].final,EXCLUDE)
        self.assertIsNone(self.store.get(m.rows[0].key,None,'choices'))
    def test_api_error_blocks_move(self):
        m=self.window.model; m.setData(m.index(0,6),'수업자료')
        self.window.api_blocked=True; self.window.update_summary()
        self.assertFalse(self.window.move_button.isEnabled())
    def test_invalid_category_rejected(self):
        m=self.window.model
        self.assertFalse(m.setData(m.index(0,6),'임의카테고리'))
        self.assertEqual(m.rows[0].final,HOLD)
    def test_category_order(self):
        d=CategoriesDialog([{'name':'A','path':str(self.root/'a')},{'name':'B','path':str(self.root/'b')}],self.window)
        d.table.selectRow(1); d.reorder(-1)
        self.assertEqual(d.table.item(0,0).text(),'B')
        d.close()
if __name__=='__main__': unittest.main()
