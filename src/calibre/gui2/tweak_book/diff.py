#!/usr/bin/env python
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2014, Kovid Goyal <kovid at kovidgoyal.net>'

import sys, re, unicodedata
from functools import partial
from collections import namedtuple
from difflib import SequenceMatcher
from future_builtins import zip

from PyQt4.Qt import (
    QSplitter, QApplication, QPlainTextDocumentLayout, QTextDocument, QTimer,
    QTextCursor, QTextCharFormat, Qt, QRect, QPainter, QPalette, QPen,
    QBrush, QColor, QTextLayout, QCursor, QFont, QSplitterHandle, QStyle,
    QPainterPath, QHBoxLayout, QWidget, QScrollBar, QEventLoop, pyqtSignal)

from calibre.ebooks.oeb.polish.utils import guess_type
from calibre.gui2.tweak_book import tprefs
from calibre.gui2.tweak_book.editor import syntax_from_mime
from calibre.gui2.tweak_book.editor.text import PlainTextEdit, get_highlighter, default_font_family, LineNumbers
from calibre.gui2.tweak_book.editor.themes import THEMES, default_theme, theme_color
from calibre.utils.diff import get_sequence_matcher

Change = namedtuple('Change', 'ltop lbot rtop rbot kind')

class BusyCursor(object):

    def __enter__(self):
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))

    def __exit__(self, *args):
        QApplication.restoreOverrideCursor()

def get_theme():
    theme = THEMES.get(tprefs['editor_theme'], None)
    if theme is None:
        theme = THEMES[default_theme()]
    return theme

class TextBrowser(PlainTextEdit):  # {{{

    resized = pyqtSignal()
    wheel_event = pyqtSignal(object)

    def __init__(self, right=False, parent=None):
        PlainTextEdit.__init__(self, parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.right = right
        self.setReadOnly(True)
        w = self.fontMetrics()
        self.number_width = max(map(lambda x:w.width(str(x)), xrange(10)))
        self.space_width = w.width(' ')
        self.setLineWrapMode(self.WidgetWidth if tprefs['editor_line_wrap'] else self.NoWrap)
        self.setTabStopWidth(tprefs['editor_tab_stop_width'] * self.space_width)
        font = self.font()
        ff = tprefs['editor_font_family']
        if ff is None:
            ff = default_font_family()
        font.setFamily(ff)
        font.setPointSize(tprefs['editor_font_size'])
        self.setFont(font)
        font = self.heading_font = QFont(self.font())
        font.setPointSize(int(tprefs['editor_font_size'] * 1.5))
        font.setBold(True)
        theme = get_theme()
        pal = self.palette()
        pal.setColor(pal.Base, theme_color(theme, 'Normal', 'bg'))
        pal.setColor(pal.AlternateBase, theme_color(theme, 'CursorLine', 'bg'))
        pal.setColor(pal.Text, theme_color(theme, 'Normal', 'fg'))
        pal.setColor(pal.Highlight, theme_color(theme, 'Visual', 'bg'))
        pal.setColor(pal.HighlightedText, theme_color(theme, 'Visual', 'fg'))
        self.setPalette(pal)
        self.viewport().setCursor(Qt.ArrowCursor)
        self.line_number_area = LineNumbers(self)
        self.blockCountChanged[int].connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.line_number_palette = pal = QPalette()
        pal.setColor(pal.Base, theme_color(theme, 'LineNr', 'bg'))
        pal.setColor(pal.Text, theme_color(theme, 'LineNr', 'fg'))
        pal.setColor(pal.BrightText, theme_color(theme, 'LineNrC', 'fg'))
        self.line_number_map = {}
        self.changes = []
        self.headers = []
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.diff_backgrounds = {
            'replace' : theme_color(theme, 'DiffReplace', 'bg'),
            'insert'  : theme_color(theme, 'DiffInsert', 'bg'),
            'delete'  : theme_color(theme, 'DiffDelete', 'bg'),
            'replacereplace': theme_color(theme, 'DiffReplaceReplace', 'bg'),
            'boundary': QBrush(theme_color(theme, 'Normal', 'fg'), Qt.Dense7Pattern),
        }
        self.diff_foregrounds = {
            'replace' : theme_color(theme, 'DiffReplace', 'fg'),
            'insert'  : theme_color(theme, 'DiffInsert', 'fg'),
            'delete'  : theme_color(theme, 'DiffDelete', 'fg'),
            'boundary': QColor(0, 0, 0, 0),
        }
        for x in ('replacereplace', 'insert', 'delete'):
            f = QTextCharFormat()
            f.setBackground(self.diff_backgrounds[x])
            setattr(self, '%s_format' % x, f)

    def clear(self):
        PlainTextEdit.clear(self)
        self.line_number_map.clear()
        del self.changes[:]
        del self.headers[:]
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def update_line_number_area_width(self, block_count=0):
        if self.right:
            self.setViewportMargins(0, 0, self.line_number_area_width(), 0)
        else:
            self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def line_number_area_width(self):
        digits = 1
        limit = max(1, self.blockCount())
        while limit >= 10:
            limit /= 10
            digits += 1

        return 8 + self.number_width * digits

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width()

    def resizeEvent(self, ev):
        PlainTextEdit.resizeEvent(self, ev)
        cr = self.contentsRect()
        if self.right:
            self.line_number_area.setGeometry(QRect(cr.right() - self.line_number_area_width(), cr.top(), cr.right(), cr.height()))
        else:
            self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))
        self.resized.emit()

    def paint_line_numbers(self, ev):
        painter = QPainter(self.line_number_area)
        painter.fillRect(ev.rect(), self.line_number_palette.color(QPalette.Base))

        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        painter.setPen(self.line_number_palette.color(QPalette.Text))

        while block.isValid() and top <= ev.rect().bottom():
            r = ev.rect()
            if block.isVisible() and bottom >= r.top():
                text = unicode(self.line_number_map.get(num, ''))
                if text == '-':
                    painter.drawLine(r.left() + 2, (top + bottom)//2, r.right() - 2, (top + bottom)//2)
                else:
                    if self.right:
                        painter.drawText(r.left() + 3, top, r.right(), self.fontMetrics().height(),
                                Qt.AlignLeft, text)
                    else:
                        painter.drawText(r.left(), top, r.right() - 5, self.fontMetrics().height(),
                                Qt.AlignRight, text)
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            num += 1

    def paintEvent(self, event):
        w = self.width()
        painter = QPainter(self.viewport())
        painter.setClipRect(event.rect())
        floor = event.rect().bottom()
        ceiling = event.rect().top()
        fv = self.firstVisibleBlock().blockNumber()
        origin = self.contentOffset()
        doc = self.document()
        lines = []

        for num, text in self.headers:
            top, bot = num, num + 3
            if bot < fv:
                continue
            y_top = self.blockBoundingGeometry(doc.findBlockByNumber(top)).translated(origin).y()
            y_bot = self.blockBoundingGeometry(doc.findBlockByNumber(bot)).translated(origin).y()
            if max(y_top, y_bot) < ceiling:
                continue
            if min(y_top, y_bot) > floor:
                break
            painter.setFont(self.heading_font)
            br = painter.drawText(3, y_top, w, y_bot - y_top - 5, Qt.TextSingleLine, text)
            painter.setPen(QPen(self.palette().text(), 2))
            painter.drawLine(0, br.bottom()+3, w, br.bottom()+3)

        for top, bot, kind in self.changes:
            if bot < fv:
                continue
            y_top = self.blockBoundingGeometry(doc.findBlockByNumber(top)).translated(origin).y()
            y_bot = self.blockBoundingGeometry(doc.findBlockByNumber(bot)).translated(origin).y()
            if max(y_top, y_bot) < ceiling:
                continue
            if min(y_top, y_bot) > floor:
                break
            if y_top != y_bot:
                painter.fillRect(0,  y_top, w, y_bot - y_top, self.diff_backgrounds[kind])
            lines.append((y_top, y_bot, kind))
        painter.end()
        PlainTextEdit.paintEvent(self, event)
        painter = QPainter(self.viewport())
        painter.setClipRect(event.rect())
        for top, bottom, kind in sorted(lines, key=lambda (t, b, k):{'replace':0}.get(k, 1)):
            painter.setPen(QPen(self.diff_foregrounds[kind], 1))
            painter.drawLine(0, top, w, top)
            painter.drawLine(0, bottom - 1, w, bottom - 1)

    def wheelEvent(self, ev):
        if ev.orientation() == Qt.Vertical:
            self.wheel_event.emit(ev)
        else:
            return PlainTextEdit.wheelEvent(self, ev)

# }}}

class Highlight(QTextDocument):  # {{{

    def __init__(self, parent, text, syntax):
        QTextDocument.__init__(self, parent)
        self.l = QPlainTextDocumentLayout(self)
        self.setDocumentLayout(self.l)
        self.highlighter = get_highlighter(syntax)(self)
        self.highlighter.apply_theme(get_theme())
        self.highlighter.setDocument(self)
        self.setPlainText(text)

    def copy_lines(self, lo, hi, cursor):
        ''' Copy specified lines from the syntax highlighted buffer into the
        destination cursor, preserving all formatting created by the syntax
        highlighter. '''
        num = hi - lo
        if num > 0:
            block = self.findBlockByNumber(lo)
            while num > 0:
                num -= 1
                cursor.insertText(block.text())
                dest_block = cursor.block()
                c = QTextCursor(dest_block)
                for af in block.layout().additionalFormats():
                    start = dest_block.position() + af.start
                    c.setPosition(start), c.setPosition(start + af.length, c.KeepAnchor)
                    c.setCharFormat(af.format)
                cursor.insertBlock()
                cursor.setCharFormat(QTextCharFormat())
                block = block.next()
# }}}

class DiffSplitHandle(QSplitterHandle):  # {{{

    WIDTH = 30  # px
    wheel_event = pyqtSignal(object)

    def paintEvent(self, event):
        QSplitterHandle.paintEvent(self, event)
        left, right = self.parent().left, self.parent().right
        painter = QPainter(self)
        painter.setClipRect(event.rect())
        w = self.width()
        h = self.height()
        painter.setRenderHints(QPainter.Antialiasing, True)
        fw = QApplication.style().pixelMetric(QStyle.PM_DefaultFrameWidth)

        C = 16  # Curve factor.

        def create_line(ly, ry, right_to_left=False):
            ' Create path that represents upper or lower line of change marker '
            line = QPainterPath()
            if not right_to_left:
                line.moveTo(0, ly)
                line.cubicTo(C, ly, w - C, ry, w, ry)
            else:
                line.moveTo(w, ry)
                line.cubicTo(w - C, ry, C, ly, 0, ly)
            return line

        ldoc, rdoc = left.document(), right.document()
        lorigin, rorigin = left.contentOffset(), right.contentOffset()
        lfv, rfv = left.firstVisibleBlock().blockNumber(), right.firstVisibleBlock().blockNumber()
        lines = []

        for (ltop, lbot, kind), (rtop, rbot, kind) in zip(left.changes, right.changes):
            if lbot < lfv and rbot < rfv:
                continue
            ly_top = left.blockBoundingGeometry(ldoc.findBlockByNumber(ltop)).translated(lorigin).y() + fw
            ly_bot = left.blockBoundingGeometry(ldoc.findBlockByNumber(lbot)).translated(lorigin).y() + fw
            ry_top = right.blockBoundingGeometry(rdoc.findBlockByNumber(rtop)).translated(rorigin).y() + fw
            ry_bot = right.blockBoundingGeometry(rdoc.findBlockByNumber(rbot)).translated(rorigin).y() + fw
            if max(ly_top, ly_bot, ry_top, ry_bot) < 0:
                continue
            if min(ly_top, ly_bot, ry_top, ry_bot) > h:
                break

            upper_line = create_line(ly_top, ry_top)
            lower_line = create_line(ly_bot, ry_bot, True)

            region = QPainterPath()
            region.moveTo(0, ly_top)
            region.connectPath(upper_line)
            region.lineTo(w, ry_bot)
            region.connectPath(lower_line)
            region.closeSubpath()

            painter.fillPath(region, left.diff_backgrounds[kind])
            for path, aa in zip((upper_line, lower_line), (ly_top != ry_top, ly_bot != ry_bot)):
                lines.append((kind, path, aa))

        for kind, path, aa in sorted(lines, key=lambda x:{'replace':0}.get(x[0], 1)):
            painter.setPen(left.diff_foregrounds[kind])
            painter.setRenderHints(QPainter.Antialiasing, aa)
            painter.drawPath(path)

        painter.setFont(left.heading_font)
        for (lnum, text), (rnum, text) in zip(left.headers, right.headers):
            ltop, lbot, rtop, rbot = lnum, lnum + 3, rnum, rnum + 3
            if lbot < lfv and rbot < rfv:
                continue
            ly_top = left.blockBoundingGeometry(ldoc.findBlockByNumber(ltop)).translated(lorigin).y()
            ly_bot = left.blockBoundingGeometry(ldoc.findBlockByNumber(lbot)).translated(lorigin).y()
            ry_top = right.blockBoundingGeometry(rdoc.findBlockByNumber(rtop)).translated(rorigin).y()
            ry_bot = right.blockBoundingGeometry(rdoc.findBlockByNumber(rbot)).translated(rorigin).y()
            if max(ly_top, ly_bot, ry_top, ry_bot) < 0:
                continue
            if min(ly_top, ly_bot, ry_top, ry_bot) > h:
                break
            ly = painter.boundingRect(3, ly_top, left.width(), ly_bot - ly_top - 5, Qt.TextSingleLine, text).bottom() + 3
            ry = painter.boundingRect(3, ry_top, right.width(), ry_bot - ry_top - 5, Qt.TextSingleLine, text).bottom() + 3
            line = create_line(ly + fw, ry + fw)
            painter.setPen(QPen(left.palette().text(), 2))
            painter.setRenderHints(QPainter.Antialiasing, ly != ry)
            painter.drawPath(line)

        painter.end()

    def sizeHint(self):
        ans = QSplitterHandle.sizeHint(self)
        ans.setWidth(self.WIDTH)
        return ans

    def wheelEvent(self, ev):
        if ev.orientation() == Qt.Vertical:
            self.wheel_event.emit(ev)
        else:
            return QSplitterHandle.wheelEvent(self, ev)
# }}}

class DiffSplit(QSplitter):  # {{{

    def __init__(self, parent=None):
        QSplitter.__init__(self, parent)

        self.left, self.right = TextBrowser(parent=self), TextBrowser(right=True, parent=self)
        self.addWidget(self.left), self.addWidget(self.right)
        self.split_words = re.compile(r"\w+|\W", re.UNICODE)
        self.clear()

    def createHandle(self):
        return DiffSplitHandle(self.orientation(), self)

    def clear(self):
        self.left.clear(), self.right.clear()
        self.changes = []

    def finalize(self):
        # check horizontal scrollbars and force both if scrollbar visible only at one side
        if self.left.horizontalScrollBar().isVisible() or self.right.horizontalScrollBar().isVisible():
            self.left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            self.right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        for v in (self.left, self.right):
            c = v.textCursor()
            c.movePosition(c.Start)
            v.setTextCursor(c)
        self.update()

    def add_diff(self, left_name, right_name, left_text, right_text, context=None, syntax=None):
        is_text = isinstance(left_text, type('')) or isinstance(right_text, type(''))
        self.left.headers.append((self.left.blockCount() - 1, left_name))
        self.right.headers.append((self.right.blockCount() - 1, right_name))
        for v in (self.left, self.right):
            c = v.textCursor()
            c.movePosition(c.End)
            (c.insertBlock(), c.insertBlock(), c.insertBlock())

        with BusyCursor():
            if is_text:
                self.add_text_diff(left_text, right_text, context, syntax)

    def add_text_diff(self, left_text, right_text, context, syntax):
        left_text = unicodedata.normalize('NFC', left_text)
        right_text = unicodedata.normalize('NFC', right_text)
        left_lines = self.left_lines = left_text.splitlines()
        right_lines = self.right_lines = right_text.splitlines()

        cruncher = get_sequence_matcher()(None, left_lines, right_lines)

        left_highlight, right_highlight = Highlight(self, left_text, syntax), Highlight(self, right_text, syntax)
        cl, cr = self.left_cursor, self.right_cursor = self.left.textCursor(), self.right.textCursor()
        cl.beginEditBlock(), cr.beginEditBlock()
        cl.movePosition(cl.End), cr.movePosition(cr.End)
        self.left_insert = partial(self.do_insert, cl, left_highlight, self.left.line_number_map)
        self.right_insert = partial(self.do_insert, cr, right_highlight, self.right.line_number_map)

        ochanges = []
        self.changes = []

        if context is None:
            for tag, alo, ahi, blo, bhi in cruncher.get_opcodes():
                getattr(self, tag)(alo, ahi, blo, bhi)
                QApplication.processEvents(QEventLoop.ExcludeUserInputEvents | QEventLoop.ExcludeSocketNotifiers)
        else:
            for i, group in enumerate(cruncher.get_grouped_opcodes(context)):
                if i > 0:
                    self.changes.append(Change(
                        ltop=cl.block().blockNumber()-1, lbot=cl.block().blockNumber(),
                        rtop=cr.block().blockNumber()-1, rbot=cr.block().blockNumber(), kind='boundary'))
                    self.left.line_number_map[self.changes[-1].ltop] = '-'
                    self.right.line_number_map[self.changes[-1].rtop] = '-'
                for tag, alo, ahi, blo, bhi in group:
                    getattr(self, tag)(alo, ahi, blo, bhi)
                    QApplication.processEvents(QEventLoop.ExcludeUserInputEvents | QEventLoop.ExcludeSocketNotifiers)
                cl.insertBlock(), cr.insertBlock()

        cl.endEditBlock(), cr.endEditBlock()
        del self.left_lines
        del self.right_lines
        del self.left_insert
        del self.right_insert

        self.coalesce_changes()

        for ltop, lbot, rtop, rbot, kind in self.changes:
            if kind != 'equal':
                self.left.changes.append((ltop, lbot, kind))
                self.right.changes.append((rtop, rbot, kind))

        self.changes = ochanges + self.changes

    def coalesce_changes(self):
        'Merge neighboring changes of the same kind, if any'
        changes = []
        for x in self.changes:
            if changes and changes[-1].kind == x.kind:
                changes[-1] = changes[-1]._replace(lbot=x.lbot, rbot=x.rbot)
            else:
                changes.append(x)
        self.changes = changes

    def do_insert(self, cursor, highlighter, line_number_map, lo, hi):
        start_block = cursor.block()
        highlighter.copy_lines(lo, hi, cursor)
        for num, i in enumerate(xrange(start_block.blockNumber(), cursor.blockNumber())):
            line_number_map[i] = lo + num + 1
        return start_block.blockNumber(), cursor.block().blockNumber()

    def equal(self, alo, ahi, blo, bhi):
        lsb, lcb = self.left_insert(alo, ahi)
        rsb, rcb = self.right_insert(blo, bhi)
        self.changes.append(Change(
            rtop=rsb, rbot=rcb, ltop=lsb, lbot=lcb, kind='equal'))

    def delete(self, alo, ahi, blo, bhi):
        start_block, current_block = self.left_insert(alo, ahi)
        r = self.right_cursor.block().blockNumber()
        self.changes.append(Change(
            ltop=start_block, lbot=current_block, rtop=r, rbot=r, kind='delete'))

    def insert(self, alo, ahi, blo, bhi):
        start_block, current_block = self.right_insert(blo, bhi)
        l = self.left_cursor.block().blockNumber()
        self.changes.append(Change(
            rtop=start_block, rbot=current_block, ltop=l, lbot=l, kind='insert'))

    def replace(self, alo, ahi, blo, bhi):
        ''' When replacing one block of lines with another, search the blocks
        for *similar* lines; the best-matching pair (if any) is used as a synch
        point, and intraline difference marking is done on the similar pair.
        Lots of work, but often worth it.  '''
        if ahi + bhi - alo - blo > 100:
            # Too many lines, this will be too slow
            # http://bugs.python.org/issue6931
            return self.do_replace(alo, ahi, blo, bhi)
        # don't synch up unless the lines have a similarity score of at
        # least cutoff; best_ratio tracks the best score seen so far
        best_ratio, cutoff = 0.74, 0.75
        cruncher = SequenceMatcher()
        eqi, eqj = None, None   # 1st indices of equal lines (if any)
        a, b = self.left_lines, self.right_lines

        # search for the pair that matches best without being identical
        # (identical lines must be junk lines, & we don't want to synch up
        # on junk -- unless we have to)
        for j in xrange(blo, bhi):
            bj = b[j]
            cruncher.set_seq2(bj)
            for i in xrange(alo, ahi):
                ai = a[i]
                if ai == bj:
                    if eqi is None:
                        eqi, eqj = i, j
                    continue
                cruncher.set_seq1(ai)
                # computing similarity is expensive, so use the quick
                # upper bounds first -- have seen this speed up messy
                # compares by a factor of 3.
                # note that ratio() is only expensive to compute the first
                # time it's called on a sequence pair; the expensive part
                # of the computation is cached by cruncher
                if (cruncher.real_quick_ratio() > best_ratio and
                        cruncher.quick_ratio() > best_ratio and
                        cruncher.ratio() > best_ratio):
                    best_ratio, best_i, best_j = cruncher.ratio(), i, j
        if best_ratio < cutoff:
            # no non-identical "pretty close" pair
            if eqi is None:
                # no identical pair either -- treat it as a straight replace
                self.do_replace(alo, ahi, blo, bhi)
                return
            # no close pair, but an identical pair -- synch up on that
            best_i, best_j, best_ratio = eqi, eqj, 1.0
        else:
            # there's a close pair, so forget the identical pair (if any)
            eqi = None

        # a[best_i] very similar to b[best_j]; eqi is None iff they're not
        # identical

        # pump out diffs from before the synch point
        self.replace_helper(alo, best_i, blo, best_j)

        # do intraline marking on the synch pair
        aelt, belt = a[best_i], b[best_j]
        if eqi is None:
            self.do_replace(best_i, best_i+1, best_j, best_j+1)
        else:
            # the synch pair is identical
            self.equal(best_i, best_i+1, best_j, best_j+1)

        # pump out diffs from after the synch point
        self.replace_helper(best_i+1, ahi, best_j+1, bhi)

    def replace_helper(self, alo, ahi, blo, bhi):
        if alo < ahi:
            if blo < bhi:
                self.replace(alo, ahi, blo, bhi)
            else:
                self.delete(alo, ahi, blo, blo)
        elif blo < bhi:
            self.insert(alo, alo, blo, bhi)

    def do_replace(self, alo, ahi, blo, bhi):
        lsb, lcb = self.left_insert(alo, ahi)
        rsb, rcb = self.right_insert(blo, bhi)
        self.changes.append(Change(
            rtop=rsb, rbot=rcb, ltop=lsb, lbot=lcb, kind='replace'))

        l, r = '\n'.join(self.left_lines[alo:ahi]), '\n'.join(self.right_lines[blo:bhi])
        ll, rl = self.split_words.findall(l), self.split_words.findall(r)
        cruncher = get_sequence_matcher()(None, ll, rl)
        lsb, rsb = self.left.document().findBlockByNumber(lsb), self.right.document().findBlockByNumber(rsb)

        def do_tag(block, words, lo, hi, pos, fmts):
            for word in words[lo:hi]:
                if word == '\n':
                    if fmts:
                        block.layout().setAdditionalFormats(fmts)
                    pos, block, fmts = 0, block.next(), []
                    continue

                if tag in {'replace', 'insert', 'delete'}:
                    fmt = getattr(self.left, '%s_format' % ('replacereplace' if tag == 'replace' else tag))
                    f = QTextLayout.FormatRange()
                    f.start, f.length, f.format = pos, len(word), fmt
                    fmts.append(f)
                pos += len(word)
            return block, pos, fmts

        lfmts, rfmts, lpos, rpos = [], [], 0, 0
        for tag, llo, lhi, rlo, rhi in cruncher.get_opcodes():
            lsb, lpos, lfmts = do_tag(lsb, ll, llo, lhi, lpos, lfmts)
            rsb, rpos, rfmts = do_tag(rsb, rl, rlo, rhi, rpos, rfmts)
        for block, fmts in ((lsb, lfmts), (rsb, rfmts)):
            if fmts:
                block.layout().setAdditionalFormats(fmts)
# }}}

class DiffView(QWidget):

    SYNC_POSITION = 0.4

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.changes = []
        self.delta = 0
        self.l = l = QHBoxLayout(self)
        self.setLayout(l)
        l.setMargin(0), l.setSpacing(0)
        self.view = DiffSplit(self)
        l.addWidget(self.view)
        self.scrollbar = QScrollBar(self)
        l.addWidget(self.scrollbar)
        self.syncing = False
        self.bars = []
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.resize_debounced)
        for i, bar in enumerate((self.scrollbar, self.view.left.verticalScrollBar(), self.view.right.verticalScrollBar())):
            self.bars.append(bar)
            bar.valueChanged[int].connect(partial(self.scrolled, i))
        self.view.left.resized.connect(self.resized)
        for v in self.view.left, self.view.right, self.view.handle(1):
            v.wheel_event.connect(self.scrollbar.wheelEvent)

    def resized(self):
        self.resize_timer.start(100)

    def resize_debounced(self):
        self.adjust_range()

    @property
    def syncpos(self):
        return self.scrollbar.value() + int(self.scrollbar.pageStep() * self.SYNC_POSITION)

    def get_position_from_scrollbar(self, which):
        changes = (self.changes, self.view.left.changes, self.view.right.changes)[which]
        bar = self.bars[which]
        syncpos = self.syncpos + bar.value()
        prev = (0, 0, None)
        for i, (top, bot, kind) in enumerate(changes):
            if syncpos <= bot:
                if top <= syncpos and top != bot:
                    # syncpos is inside a change
                    ratio = float(syncpos - top) / (bot - top)
                    return 'in', i, ratio
                else:
                    # syncpos is after the change
                    offset = syncpos - prev[1]
                    return 'after', i - 1, offset
                break
            else:
                prev = (top, bot, kind)
        else:
            offset = syncpos - prev[1]
            return 'after', len(self.changes) - 1, offset

    def scroll_to(self, which, position):
        changes = (self.changes, self.view.left.changes, self.view.right.changes)[which]
        bar = self.bars[which]
        syncpos = self.syncpos
        val = None
        if position[0] == 'in':
            change_idx, ratio = position[1:]
            start, end = changes[change_idx][:2]
            val = start + int((end - start) * ratio)
        else:
            change_idx, offset = position[1:]
            start = 0 if change_idx < 0 else changes[change_idx][1]
            val = start + offset
        bar.setValue(val - syncpos)

    def scrolled(self, which):
        if self.syncing:
            return
        position = self.get_position_from_scrollbar(which)
        with self:
            for x in {0, 1, 2} - {which}:
                self.scroll_to(x, position)
        self.view.handle(1).update()

    def __enter__(self):
        self.syncing = True

    def __exit__(self, *args):
        self.syncing = False

    def clear(self):
        self.view.clear()
        self.changes = []
        self.delta = 0

    def adjust_range(self):
        ls, rs = self.view.left.verticalScrollBar(), self.view.right.verticalScrollBar()
        page_step = self.view.left.verticalScrollBar().pageStep()
        self.scrollbar.setPageStep(min(ls.pageStep(), rs.pageStep()))
        self.scrollbar.setSingleStep(min(ls.singleStep(), rs.singleStep()))
        self.scrollbar.setRange(0, ls.maximum() + self.delta)
        self.scrollbar.setVisible(self.scrollbar.maximum() > page_step)

    def finalize(self):
        self.view.finalize()
        self.changes = []
        self.calculate_length()

    def calculate_length(self):
        left, right = self.view.left, self.view.right
        changes = []
        delta = 0
        for (l_top, l_bot, kind), (r_top, r_bot, kind) in zip(left.changes, right.changes):
            height = max(l_bot - l_top, r_bot - r_top)
            top = delta + l_top
            changes.append((top, top + height, kind))
            delta = top + height - l_bot
        self.changes, self.delta = changes, delta
        self.adjust_range()

    def keyPressEvent(self, ev):
        amount, d = None, 1
        key = ev.key()
        if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_J, Qt.Key_K):
            amount = self.scrollbar.singleStep()
            if key in (Qt.Key_Up, Qt.Key_K):
                d = -1
        elif key in (Qt.Key_PageUp, Qt.Key_PageDown):
            amount = self.scrollbar.pageStep()
            if key in (Qt.Key_PageUp,):
                d = -1
        elif key in (Qt.Key_Home, Qt.Key_End):
            self.scrollbar.setValue(0 if key == Qt.Key_Home else self.scrollbar.maximum())

        if amount is not None:
            self.scrollbar.setValue(self.scrollbar.value() + d * amount)


if __name__ == '__main__':
    app = QApplication([])
    w = DiffView()
    w.show()
    for l, r in zip(sys.argv[1::2], sys.argv[2::2]):
        raw1 = open(l, 'rb').read().decode('utf-8')
        raw2 = open(r, 'rb').read().decode('utf-8')
        w.view.add_diff(l, r, raw1, raw2, syntax=syntax_from_mime(l, guess_type(l)), context=31)
    w.finalize()
    app.exec_()

# TODO: Add diff colors for other color schemes
