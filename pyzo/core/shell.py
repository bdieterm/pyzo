"""Module shell

Defines the shell to be used in pyzo.
This is done in a few inheritance steps:
  - BaseShell inherits BaseTextCtrl and adds the typical shell behaviour.
  - PythonShell makes it specific to Python.
This module also implements ways to communicate with the shell and to run
code in it.

"""

import sys
import time
import re

import yoton

import pyzo
from pyzo.util import zon as ssdf  # zon is ssdf-light
from pyzo.qt import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

from pyzo.codeeditor.highlighter import Highlighter
from pyzo.codeeditor import parsers

from pyzo.core.baseTextCtrl import BaseTextCtrl
from pyzo.core.pyzoLogging import print
from pyzo.core.kernelbroker import KernelInfo, Kernelmanager
from pyzo.core.menu import ShellContextMenu


# Interval for polling messages. Timer for each kernel.
POLL_TIMER_INTERVAL_IDLE = 100  # in ms; energy saving mode
POLL_TIMER_INTERVAL_BUSY = 10  # in ms; faster updates

# Maximum number of lines in the shell
MAXBLOCKCOUNT = pyzo.config.advanced.shellMaxLines


# todo: we could make command shells to, with autocompletion and coloring...


class YotonEmbedder(QtCore.QObject):
    """Embed the Yoton event loop."""

    def __init__(self):
        super().__init__()
        yoton.app.embed_event_loop(self.postYotonEvent)

    def postYotonEvent(self):
        try:
            QtWidgets.qApp.postEvent(self, QtCore.QEvent(QtCore.QEvent.Type.User))
        except Exception:
            pass  # If pyzo is shutting down, the app may be None

    def customEvent(self, event):
        """This is what gets called by Qt."""
        yoton.process_events(False)


yotonEmbedder = YotonEmbedder()


# Short constants for cursor movement
A_KEEP = QtGui.QTextCursor.MoveMode.KeepAnchor
A_MOVE = QtGui.QTextCursor.MoveMode.MoveAnchor

# Instantiate a local kernel broker upon loading this module
pyzo.localKernelManager = Kernelmanager(public=False)


def finishKernelInfo(info, scriptFile=None):
    """Get a copy of the kernel info struct, with the scriptFile
    and the projectPath set.
    """

    # Make a copy, we do not want to change the original
    info = ssdf.copy(info)

    # Set scriptFile (if '', the kernel will run in interactive mode)
    if scriptFile:
        info.scriptFile = scriptFile
    else:
        info.scriptFile = ""

    # If the file browser is active, and has the check box
    #'add path to Python path' set, set the PROJECTPATH variable
    fileBrowser = pyzo.toolManager.getTool("pyzofilebrowser")
    info.projectPath = ""
    if fileBrowser:
        info.projectPath = fileBrowser.getAddToPythonPath()

    return info


class ShellHighlighter(Highlighter):
    """This highlighter implements highlighting for a shell;
    only the input lines are highlighted with this highlighter.
    """

    def highlightBlock(self, line):
        # Get previous state
        previousState = self.previousBlockState()

        # Get parser
        parser = None
        if hasattr(self._codeEditor, "parser"):
            parser = self._codeEditor.parser()

        # Get function to get format
        nameToFormat = self._codeEditor.getStyleElementFormat

        # Last line?
        cursor1 = self._codeEditor._cursor1
        cursor2 = self._codeEditor._cursor2
        commandCursor = self._codeEditor._lastCommandCursor
        curBlock = self.currentBlock()
        #
        atLastPrompt, atCurrentPrompt = False, False
        if curBlock.position() == 0:
            pass
        elif curBlock.position() == commandCursor.block().position():
            atLastPrompt = True
        elif curBlock.position() >= cursor1.block().position():
            atCurrentPrompt = True

        if not atLastPrompt and not atCurrentPrompt:
            # Do not highlight anything but current and last prompts
            return

        # Get user data
        bd = self.getCurrentBlockUserData()

        if parser:
            if atCurrentPrompt:
                pos1, pos2 = cursor1.positionInBlock(), cursor2.positionInBlock()
            else:
                pos1, pos2 = 0, commandCursor.positionInBlock()

            # Check if we should *not* format this line.
            # This is the case for special "executing" text
            # A bit of a hack though ... is there a better way to signal this?
            specialinput = (not atCurrentPrompt) and line[pos2:].startswith(
                "(executing "
            )

            self.setCurrentBlockState(0)
            if specialinput:
                pass  # Let the kernel decide formatting
            else:
                tokens = parser.parseLine(line, previousState)
                bd.tokens = tokens
                for token in tokens:
                    # Handle block state
                    if isinstance(token, parsers.BlockState):
                        self.setCurrentBlockState(token.state)
                    else:
                        # Get format
                        try:
                            format = nameToFormat(token.name).textCharFormat
                        except KeyError:
                            # print(repr(nameToFormat(token.name)))
                            continue
                        # Set format
                        # format.setFontWeight(QtGui.QFont.Weight.Bold)
                        if token.start >= pos2:
                            self.setFormat(token.start, token.end - token.start, format)

            # Set prompt to bold
            if atCurrentPrompt:
                format = QtGui.QTextCharFormat()
                format.setFontWeight(QtGui.QFont.Weight.Bold)
                self.setFormat(pos1, pos2 - pos1, format)

        # Get the indentation setting of the editors
        indentUsingSpaces = self._codeEditor.indentUsingSpaces()

        leadingWhitespace = line[: len(line) - len(line.lstrip())]
        if "\t" in leadingWhitespace and " " in leadingWhitespace:
            # Mixed whitespace
            bd.indentation = 0
            format = QtGui.QTextCharFormat()
            format.setUnderlineStyle(
                QtGui.QTextCharFormat.UnderlineStyle.SpellCheckUnderline
            )
            format.setUnderlineColor(QtCore.Qt.GlobalColor.red)
            format.setToolTip("Mixed tabs and spaces")
            self.setFormat(0, len(leadingWhitespace), format)
        elif ("\t" in leadingWhitespace and indentUsingSpaces) or (
            " " in leadingWhitespace and not indentUsingSpaces
        ):
            # Whitespace differs from document setting
            bd.indentation = 0
            format = QtGui.QTextCharFormat()
            format.setUnderlineStyle(
                QtGui.QTextCharFormat.UnderlineStyle.SpellCheckUnderline
            )
            format.setUnderlineColor(QtCore.Qt.GlobalColor.blue)
            format.setToolTip("Whitespace differs from document setting")
            self.setFormat(0, len(leadingWhitespace), format)
        else:
            # Store info for indentation guides
            # amount of tabs or spaces
            bd.indentation = len(leadingWhitespace)


class BaseShell(BaseTextCtrl):
    """The BaseShell implements functionality to make a generic shell."""

    def __init__(self, parent, **kwds):
        super().__init__(
            parent,
            wrap=True,
            showLineNumbers=False,
            showBreakPoints=False,
            highlightCurrentLine=False,
            parser="python",
            **kwds,
        )

        # Use a special highlighter that only highlights the input.
        self._setHighlighter(ShellHighlighter)

        # No undo in shells
        self.setUndoRedoEnabled(False)

        # variables we need
        self._more = False

        # We use two cursors to keep track of where the prompt is
        # cursor1 is in front, and cursor2 is at the end of the prompt.
        # They can be in the same position.
        # Further, we store a cursor that selects the last given command,
        # so it can be styled.
        self._cursor1 = self.textCursor()
        self._cursor2 = self.textCursor()
        self._lastCommandCursor = self.textCursor()
        self._lastline_had_cr = False
        self._lastline_had_lf = False

        # When inserting/removing text at the edit line (thus also while typing)
        # keep cursor2 at its place. Only when text is written before
        # the prompt (i.e. in write), this flag is temporarily set to False.
        # Same for cursor1, because sometimes (when there is no prompt) it
        # is at the same position.
        self._cursor1.setKeepPositionOnInsert(True)
        self._cursor2.setKeepPositionOnInsert(True)

        # Similarly, we use the _lastCommandCursor cursor really for pointing.
        self._lastCommandCursor.setKeepPositionOnInsert(True)

        self.resetShellWriters()

        # Variables to keep track of the command history usage
        self._historyNeedle = None  # None means none, "" means look in all
        self._historyStep = 0

        # Set minimum width so 80 lines do fit in smallest font size
        self.setMinimumWidth(200)

        # Hard wrapping. QTextEdit allows hard wrapping at a specific column.
        # Unfortunately, QPlainTextEdit does not.
        self.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAnywhere)

        # Limit number of lines
        self.setMaximumBlockCount(MAXBLOCKCOUNT)

        # Keep track of position, so we can disable editing if the cursor
        # is before the prompt
        self.cursorPositionChanged.connect(self.onCursorPositionChanged)

        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # See remark at mousePressEvent

    ## Cursor stuff

    def onCursorPositionChanged(self):
        # If the end of the selection (or just the cursor if there is no selection)
        # is before the beginning of the line. make the document read-only
        cursor = self.textCursor()
        promptpos = self._cursor2.position()
        if cursor.position() < promptpos or cursor.anchor() < promptpos:
            self.setReadOnly(True)
        else:
            self.setReadOnly(False)

    def ensureCursorAtEditLine(self):
        """If the text cursor is before the beginning of the edit line,
        move it to the end of the edit line
        """
        cursor = self.textCursor()
        promptpos = self._cursor2.position()
        if cursor.position() < promptpos or cursor.anchor() < promptpos:
            cursor.movePosition(
                cursor.MoveOperation.End, A_MOVE
            )  # Move to end of document
            self.setTextCursor(cursor)
            self.onCursorPositionChanged()

    def mousePressEvent(self, event):
        """
        - Focus policy
            If a user clicks this shell, while it has no focus, we do
            not want the cursor position to change (since generally the
            user clicks the shell to give it the focus). We do this by
            setting the focus-policy to Qt::TabFocus, and we give the
            widget its focus manually from the mousePressedEvent event
            handler
        """
        if not self.hasFocus():
            self.setFocus()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton:
            if sys.platform == "linux":
                # On Linux, pasting with the middle button would insert text at the
                # clicked position, even if this is before the prompt (if readOnly is False
                # when the current text cursor is at the edit line).
                # We will ignore the pasting if the click position is not after the prompt.
                cursor = self.cursorForPosition(event.position().toPoint())
                promptpos = self._cursor2.position()
                if cursor.position() < promptpos:
                    return
                else:
                    # The current text cursor might be before the end of the prompt.
                    # But the text is inserted after the prompt (mouse event position).
                    # Therefore we move the text cursor to the edit line so that we are
                    # not in read-only mode.
                    # The text will be inserted at the event's position, no matter where
                    # the text cursor is.
                    self.setTextCursor(cursor)
                    self.onCursorPositionChanged()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """Do not show context menu."""
        pass

    def mouseDoubleClickEvent(self, event):
        super().mouseDoubleClickEvent(event)
        self._handleClickOnFilename(event.position().toPoint())

    def _handleClickOnFilename(self, mousepos):
        """Check whether the text that is clicked is a filename
        and open the file in the editor. If a line number can also be
        detected, open the file at that line number.
        """

        # Get cursor and its current pos
        cursor = self.cursorForPosition(mousepos)
        ocursor = QtGui.QTextCursor(cursor)  # Make a copy to use below
        pos = cursor.positionInBlock()

        # Define some abbreviations for movePosition arguments
        MO = cursor.MoveOperation
        MM = cursor.MoveMode

        # Get the whole line where the double click occurred
        cursor.movePosition(MO.EndOfBlock, MM.MoveAnchor)
        cursor.movePosition(MO.StartOfBlock, MM.KeepAnchor)
        line = cursor.selectedText()
        if len(line) > 1024:
            return  # safety

        if sys.platform == "win32":
            # e.g. "C:\somefile" or "\\abc" or "c:/abc"
            patFile = r"(?:\\\\|[a-zA-Z]:[\\/]).+?"
        else:
            patFile = r"/.+?"

        patFileOrTmp = r"(" + patFile + r"|<tmp \d+>)"

        # There could be a line offset after the filename, e.g.:
        # <tmp 3>+2:3: SyntaxWarning: invalid escape sequence '\s'
        patFileOrTmpWithOffset = patFileOrTmp + r"(\+\d+)?"  # groups: filepath, offset

        filename = None
        offset = None
        linenr = None

        patWarning = patFileOrTmpWithOffset + r":(\d+): [a-zA-Z]+Warning: .*"
        # /tmp/aa.py+5:4: SyntaxWarning: invalid escape sequence '\s'

        patIPython = r"\s*File " + patFileOrTmpWithOffset + r":(\d+)(?:, in .*)?"
        # IPython examples (leading spaces occur when running file as script):
        # File /tmp/aa.py:12
        # File /tmp/aa.py:4, in myfunc1()
        #   File /tmp/aa.py:14

        for pattern in [patWarning, patIPython]:
            mo = re.fullmatch(pattern, line)
            if mo:
                i1, i2 = mo.span(1)
                if i1 <= pos < i2:
                    filename = mo[1]
                    offset = mo[2]
                    linenr = int(mo[3])
                    break
                else:
                    # the pattern matches, but the double click was outside the filepath
                    return

        if filename is None:
            # Expand string to left and right, starting at the clicked position, and
            # stopping only when reaching the ends or encountering a (single) quote.
            line2 = line.replace("'", '"')
            i1 = line2.rfind('"', 0, pos) + 1
            i2 = line2.find('"', pos)
            if i2 == -1:
                i2 = len(line)

            mo = re.fullmatch(patFileOrTmpWithOffset, line[i1:i2])
            if mo:
                filename, offset = mo.groups()

                # Split in parts for getting line number
                mo = re.search(r"\b(?:line|linenr|lineno)\b\s+(\d+)", line[i2:])
                if mo:
                    linenr = int(mo[1])

        if filename:
            if offset and offset.startswith("+") and linenr is not None:
                linenr += int(offset[1:])

            # Select the whole filename in the shell
            cursor = ocursor
            cursor.movePosition(MO.Left, MM.MoveAnchor, pos - i1)
            cursor.movePosition(MO.Right, MM.KeepAnchor, len(filename))
            self.setTextCursor(cursor)

            # Try opening the file (at the line number if we have one)
            result = pyzo.editors.loadFile(filename)
            if result:
                editor = result._editor
                if linenr is not None:
                    editor.gotoLine(linenr)
                    cursor = editor.textCursor()
                    cursor.movePosition(MO.StartOfBlock)
                    cursor.movePosition(MO.EndOfBlock, MM.KeepAnchor)
                    editor.setTextCursor(cursor)
                editor.setFocus()

    ## Indentation: override code editor behaviour
    def indentSelection(self):
        pass

    def dedentSelection(self):
        pass

    ## Key handlers
    def keyPressEvent(self, event):
        if event.key() in [Qt.Key.Key_Return, Qt.Key.Key_Enter]:
            # First check if autocompletion triggered
            if self.potentiallyAutoComplete(event):
                return
            else:
                # Enter: execute line
                # Remove calltip and autocomp if shown
                self.autocompleteCancel()
                self.calltipCancel()

                # reset history needle
                self._historyNeedle = None

                # process
                self.processLine()
                return

        if event.key() == Qt.Key.Key_Escape:
            # Escape clears command
            if not (self.autocompleteActive() or self.calltipActive()):
                self.clearCommand()

        if event.key() == Qt.Key.Key_Home:
            # Home goes to the prompt.
            cursor = self.textCursor()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                cursor.setPosition(self._cursor2.position(), A_KEEP)
            else:
                cursor.setPosition(self._cursor2.position(), A_MOVE)
            #
            self.setTextCursor(cursor)
            self.autocompleteCancel()
            return

        if event.key() == Qt.Key.Key_Insert:
            # Don't toggle between insert mode and overwrite mode.
            return True

        # Ensure to not backspace / go left beyond the prompt
        if event.key() in [Qt.Key.Key_Backspace, Qt.Key.Key_Left]:
            self._historyNeedle = None
            if self.textCursor().position() == self._cursor2.position():
                if event.key() == Qt.Key.Key_Backspace:
                    self.textCursor().removeSelectedText()
                return  # Ignore the key, don't go beyond the prompt

        if (
            event.key() in [Qt.Key.Key_Up, Qt.Key.Key_Down]
            and not self.autocompleteActive()
        ):
            # needle
            if self._historyNeedle is None:
                # get partly-written-command
                #
                # Select text
                cursor = self.textCursor()
                cursor.setPosition(self._cursor2.position(), A_MOVE)
                cursor.movePosition(cursor.MoveOperation.End, A_KEEP)
                # Update needle text
                self._historyNeedle = cursor.selectedText()
                self._historyStep = 0

            # Browse through history
            if event.key() == Qt.Key.Key_Up:
                self._historyStep += 1
            else:  # Key_Down
                self._historyStep -= 1
                if self._historyStep < 1:
                    self._historyStep = 1

            # find the command
            c = pyzo.command_history.find_starting_with(
                self._historyNeedle, self._historyStep
            )
            if c is None:
                # found nothing-> reset
                self._historyStep = 0
                c = self._historyNeedle

            # Replace text
            cursor = self.textCursor()
            cursor.setPosition(self._cursor2.position(), A_MOVE)
            cursor.movePosition(cursor.MoveOperation.End, A_KEEP)
            cursor.insertText(c)

            self.ensureCursorAtEditLine()
            return

        else:
            # Reset needle
            self._historyNeedle = None

        # if a 'normal' key is pressed, ensure the cursor is at the edit line
        if event.text():
            self.ensureCursorAtEditLine()

        # Default behaviour: BaseTextCtrl
        super().keyPressEvent(event)

    ## Cut / Copy / Paste / Drag & Drop

    def cut(self):
        """Reimplement cut to only copy if part of the selected text
        is not at the prompt.
        """

        if self.isReadOnly():
            return self.copy()
        else:
            return super().cut()

    # def copy(self): # no overload needed

    def paste(self):
        """Reimplement paste to paste at the end of the edit line when
        the position is at the prompt.
        """
        self.ensureCursorAtEditLine()
        # Paste normally
        return super().paste()

    def dragEnterEvent(self, event):
        """We only support copying of the text"""
        if event.mimeData().hasText():
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        """The shell supports only a single line but the text may contain multiple
        lines. We insert at the editLine only the first non-empty line of the text
        """
        if event.mimeData().hasText():
            text = event.mimeData().text()
            insertText = ""
            for line in text.splitlines():
                if line.strip():
                    insertText = line
                    break

            # Move the cursor to the position indicated by the drop location, but
            # ensure it is at the edit line
            self.setTextCursor(self.cursorForPosition(event.position().toPoint()))
            self.ensureCursorAtEditLine()

            # Now insert the text
            cursor = self.textCursor()
            cursor.insertText(insertText)
            self.setFocus()

    ## Basic commands to control the shell

    def clearScreen(self):
        """Clear all the previous output from the screen."""
        # Select from beginning of prompt to start of document
        self._cursor1.clearSelection()
        self._cursor1.movePosition(
            self._cursor1.MoveOperation.Start, A_KEEP
        )  # Keep anchor
        self._cursor1.removeSelectedText()
        # Wrap up
        self.ensureCursorAtEditLine()
        self.ensureCursorVisible()
        self._cursor1_lastStreamStart = None

    def deleteLines(self):
        """Called from the menu option "delete lines", just execute self.clearCommand()"""
        self.clearCommand()

    def clearCommand(self):
        """Clear the current command, move the cursor right behind
        the prompt, and ensure it's visible.
        """
        # Select from prompt end to length and delete selected text.
        cursor = self.textCursor()
        cursor.setPosition(self._cursor2.position(), A_MOVE)
        cursor.movePosition(cursor.MoveOperation.End, A_KEEP)
        cursor.removeSelectedText()
        # Wrap up
        self.ensureCursorAtEditLine()
        self.ensureCursorVisible()

    def write(self, text, prompt=0, color=None, streamIdentifier=None):
        """Write to the shell.

        If prompt is 0 (default) the text is printed before the prompt. If
        prompt is 1, the text is printed after the prompt, the new prompt
        becomes null. If prompt is 2, the given text becomes the new prompt.

        The color of the text can also be specified (as a hex-string).

        The streamIdentifier is a hash-able value that serves to recognize the
        stream, especially when we are writing before the prompt. For example:
        When we print 'SOMETEXT' to stream stdout it might be split into
        smaller fragments, e.g. 'SOME' and 'TEXT'. Between writing these two
        fragments to the shell widget, some other text 'error' might be printed
        to stream stderr. So the final output could be 'SOMEerrorTEXT'.
        When writing these three strings we have to know if they are from the
        same stream or not. This is important when a format escape segment is
        split into two fragments or when a backspace character wants to delete
        a character from the previous fragment of the same stream.
        """

        # From The Qt docs: Note that a cursor always moves when text is
        # inserted before the current position of the cursor, and it always
        # keeps its position when text is inserted after the current position
        # of the cursor.

        # Make sure there's text and make sure it's a string
        if not text:
            return
        if isinstance(text, bytes):
            text = text.decode("utf-8")

        # Prepare format
        format = QtGui.QTextCharFormat()
        if color:
            format.setForeground(QtGui.QColor(color))

        # pos1, pos2 = self._cursor1.position(), self._cursor2.position()

        # Just in case, clear any selection of the cursors
        self._cursor1.clearSelection()
        self._cursor2.clearSelection()

        if prompt == 0:
            # Insert text before prompt (normal streams)
            self._cursor1.setKeepPositionOnInsert(False)
            self._cursor2.setKeepPositionOnInsert(False)

            # We want to know if we are allowed to remove some characters from the
            # current line (left to the cursor), and if yes, till which column.
            # If the current line started with a different stream, we cannot delete
            # from that.
            leftLimit = None
            if self._cursor1_lastStreamStart is not None:
                if self._cursor1_lastStreamStart[0] == streamIdentifier:
                    leftLimit = self._cursor1_lastStreamStart[1]

            shellWriter = self._shellWriters.setdefault(streamIdentifier, ShellWriter())
            newLeftLimit = shellWriter.writeText(self._cursor1, leftLimit, text, format)
            self._cursor1_lastStreamStart = (streamIdentifier, newLeftLimit)
        elif prompt == 1:
            # Insert command text after prompt, prompt becomes null (input)
            self._lastCommandCursor.setPosition(self._cursor2.position())
            self._cursor1.setKeepPositionOnInsert(False)
            self._cursor2.setKeepPositionOnInsert(False)
            ShellWriter().writeText(self._cursor2, None, text, format)
            self._cursor1.setPosition(self._cursor2.position(), A_MOVE)
            self._cursor1_lastStreamStart = None
        elif prompt == 2 and text == "\b":
            # Remove prompt (used when closing the kernel)
            self._cursor1.setPosition(self._cursor2.position(), A_KEEP)
            self._cursor1.removeSelectedText()
            self._cursor2.setPosition(self._cursor1.position(), A_MOVE)
        elif prompt == 2:
            # text becomes new prompt
            self._cursor1.setPosition(self._cursor2.position(), A_KEEP)
            self._cursor1.removeSelectedText()
            self._cursor1.setKeepPositionOnInsert(True)
            self._cursor2.setKeepPositionOnInsert(False)
            ShellWriter().writeText(self._cursor1, None, text, format)

        # Reset cursor states for the user to type his/her commands
        self._cursor1.setKeepPositionOnInsert(True)
        self._cursor2.setKeepPositionOnInsert(True)

        # Make sure that cursor is visible (only when cursor is at edit line)
        if not self.isReadOnly():
            self.ensureCursorVisible()

        # Scroll along with the text if lines are popped from the top
        elif self.blockCount() == MAXBLOCKCOUNT:
            n = text.count("\n")
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() - n)

    ## Executing stuff

    def processLine(self, line=None, execute=True):
        """Process the given line or the current line at the prompt if not given.

        Called when the user presses enter.

        If execute is False will not execute the command. This way
        a message can be written while other ways are used to process
        the command.
        """

        # Can we do this?
        if self.isReadOnly() and not line:
            return

        if line:
            # remove trailing newline(s)
            command = line.rstrip("\n")
        else:
            # Select command
            cursor = self.textCursor()
            cursor.setPosition(self._cursor2.position(), A_MOVE)
            cursor.movePosition(cursor.MoveOperation.End, A_KEEP)

            # Sample the text from the prompt and remove it
            command = cursor.selectedText().replace("\u2029", "\n").rstrip("\n")
            cursor.removeSelectedText()

            # Auto-indent. Note: this is rather Python-specific
            command_s = command.lstrip()
            indent = " " * (len(command) - len(command_s))
            if command.strip().endswith(":"):
                indent += "    "
            elif not command_s:
                indent = ""
            if indent:
                cursor.insertText(indent)

            if command:
                # Remember the command in this global history
                pyzo.command_history.append(command)

        if execute:
            command = command.replace("\r\n", "\n")
            self.executeCommand(command + "\n")

    def executeCommand(self, command):
        """Execute the given command.
        Should be overridden.
        """
        # this is a stupid simulation version
        self.write("you executed: " + command + "\n")
        self.write(">>> ", prompt=2)

    def resetShellWriters(self):
        # init shell writer objects (for handling terminal escape sequences etc.)
        self._shellWriters = {}
        self._cursor1_lastStreamStart = None


class ShellWriter:
    # normal colors:
    # COLORS = "#000 #F00 #0F0 #FF0 #00F #F0F #0FF #FFF".split()

    # solarised color theme:
    COLORS = "#657b83 #dc322f #859900 #b58900 #268bd2 #d33682 #2aa198 #eee8d5".split()

    _linebreaks = "\n\u2028\u2029\ufdd0\ufdd1"
    _reSplit = re.compile("([" + _linebreaks + "\t\x1b]|\v+|\r+|\b+)")
    _reFormatPattern = re.compile(r"(\x1b\[(\d+(?:;\d+)*)m)")

    def __init__(self):
        self._currentFormat = None
        self._unfinishedTail = ""  # either "" or "\r" or "\x1b"+...
        self._lineShorteningActive = False

    def writeText(self, cursor, leftLimitFirstRow, text, defaultFormat):
        if leftLimitFirstRow is None:
            leftLimitFirstRow = cursor.positionInBlock()
            self._lineShorteningActive = False

        assert cursor.positionInBlock() >= leftLimitFirstRow

        leftLimit = leftLimitFirstRow
        posInBlock = cursor.positionInBlock()
        lowestPosInFirstBlock = posInBlock
        unfinishedTailNew = ""

        text2 = text
        if self._unfinishedTail.startswith("\x1b"):
            posInBlock = max(0, posInBlock - len(self._unfinishedTail))
            lowestPosInFirstBlock = posInBlock
            text2 = self._unfinishedTail + text

        if self._currentFormat is None:
            self._currentFormat = defaultFormat
        currentFormat = self._currentFormat

        finishedLines = []
        # each element in finishedLines is a list like this for each line:
        #   [*elems, startPos, endPos]
        #   0 to n elems ... either of type str or QTextCharFormat
        #   endPos ... end column (0-based), after the last character
        #
        #   Format elems have length zero.
        #   The last elem (before endPos) can be a '\n'.
        #   There is no other '\n' anywhere else in the line.

        ll = []
        splitText = [s for s in self._reSplit.split(text2) if s != ""]
        if self._unfinishedTail == "\r" and splitText[:1] != ["\n"]:
            # the last text ended with '\r', and it is not a '\r\n'
            # --> clear this line
            splitText.insert(0, "\r")
        lenSplit = len(splitText)
        for i, s in enumerate(splitText):
            c = s[:1]
            if c == "":
                continue
            elif c in self._linebreaks:  # new line
                s = "\n"
                ll.append(s)
                posInBlock += len(s)
                ll.append(posInBlock)
                finishedLines.append(ll)
                ll = []
                leftLimit = 0
                posInBlock = leftLimit
            elif c == "\t":  # horizontal tab --> moves to next multiple of tabwidth
                tabwidth = 8
                numSpaces = tabwidth - posInBlock % tabwidth
                s = " " * numSpaces
                posInBlock += len(s)
                ll.append(s)
            elif c == "\v":  # vertical tab --> moves to next line, but same column
                n = len(s)
                ll.append("\n")
                ll.append(posInBlock + 1)
                finishedLines.append(ll)
                leftLimit = 0
                finishedLines.extend(["\n", leftLimit + 1] * (n - 1))
                ll = [" " * posInBlock]
            elif c == "\r":
                if i == lenSplit - 1:
                    # we don't know yet if this is a CR LF or a CR to delete the line
                    unfinishedTailNew = c
                    continue
                elif splitText[i + 1] == "\n":
                    # we have a CR LF sequence --> ignore the CR
                    continue
                else:
                    # delete the current line
                    ll = []
                    posInBlock = leftLimit
                    if len(finishedLines) == 0:
                        lowestPosInFirstBlock = leftLimit
            elif c == "\b":  # backspace
                # Backspace normally would move the cursor one character left without
                # deleting anything. In our implementation, backspace removes the
                # previous character, even if it was from a text fragment of the same
                # stream that was already written before. We can only delete till the
                # start of the current line, and we cannot move further left than
                # column leftLimit.
                n = len(s)
                for i, v in list(enumerate(ll))[::-1]:
                    if isinstance(v, str):
                        numBackspace = min(n, len(v))
                        ll[i] = v[: len(v) - numBackspace]
                        n -= numBackspace
                        posInBlock -= numBackspace
                        if n == 0:
                            break
                posInBlock = max(leftLimit, posInBlock - n)
                if len(finishedLines) == 0 and posInBlock < lowestPosInFirstBlock:
                    lowestPosInFirstBlock = posInBlock
            elif c == "\x1b":  # could be the start of an ANSI-like escape sequence
                # to format the text, see http://en.wikipedia.org/wiki/ANSI_escape_code
                # A full escape sequence consists of the start elem '\x1b' and the
                # remaining string elem, which might be incomplete in this fragment.
                if i + 1 < lenSplit:
                    textAndNext = s + splitText[i + 1]
                    format, matchLen = self.parseFormat(
                        textAndNext, currentFormat, defaultFormat
                    )
                    if format is not None:
                        # escape sequence is complete and correct
                        splitText[i + 1] = splitText[i + 1][matchLen - 1 :]
                        ll.append(format)
                        currentFormat = format
                    else:
                        # escape sequence is incomplete or invalid
                        if i == lenSplit - 2:
                            # check if the split text ends with an incomplete format
                            for s2 in ("m", "0m", "[0m"):
                                if self._reFormatPattern.match(textAndNext + s2):
                                    unfinishedTailNew = textAndNext
                                    break
                        ll.append(s)
                        posInBlock += len(s)
                elif i == lenSplit - 1:
                    # escape sequence is incomplete or invalid
                    unfinishedTailNew = s
                    ll.append(s)
                    posInBlock += len(s)
            else:
                ll.append(s)
                posInBlock += len(s)

        self._unfinishedTail = unfinishedTailNew
        ll.append(posInBlock)
        finishedLines.append(ll)
        # the last line in finishedLines does not end with "\n"

        numCharsToRemove = cursor.positionInBlock() - lowestPosInFirstBlock

        # If necessary, make a new cursor that moves along. We insert
        # the text in pieces, so we need to move along with the text!
        if cursor.keepPositionOnInsert():
            cursor = QtGui.QTextCursor(cursor)
            cursor.setKeepPositionOnInsert(False)

        # delete characters from the previous fragment
        cursor.beginEditBlock()
        if numCharsToRemove > 0:
            cursor.movePosition(cursor.MoveOperation.Left, A_KEEP, numCharsToRemove)
            cursor.removeSelectedText()

        # shorten very long lines
        finishedLines = self._shortenLines(finishedLines, cursor)

        # write the text with proper formatting to the shell widget
        format = self._currentFormat
        for line in finishedLines:
            # lastPosInBlock = line[-1]
            for elem in line[:-1]:
                if isinstance(elem, str):
                    cursor.insertText(elem, format)
                elif isinstance(elem, QtGui.QTextCharFormat):
                    format = elem
        cursor.endEditBlock()

        self._currentFormat = currentFormat
        newLeftLimit = leftLimit
        return newLeftLimit

    @staticmethod
    def _splitStringIntoChunks(s, offset, chunkSize):
        n = len(s)
        numChunks, rem = divmod(n - offset, chunkSize)
        tillOffset = s[:offset]
        chunkList = [
            s[offset + i * chunkSize : offset + (i + 1) * chunkSize]
            for i in range(numChunks)
        ]
        remnant = s[offset + numChunks * chunkSize :]
        return tillOffset, chunkList, remnant

    def _shortenLines(self, finishedLines, cursor):
        posInBlock = cursor.positionInBlock()
        finishedLinesShortened = []
        shortLineLength = 80  # length is without the newline char
        for indLine, line in enumerate(finishedLines):
            lastPosInBlock = line[-1]

            # hysteresis for line shortening
            if lastPosInBlock > 1024:
                self._lineShorteningActive = True
            elif lastPosInBlock <= shortLineLength + 1:  # +1 means including newline
                if indLine < len(finishedLines) - 1:
                    self._lineShorteningActive = False

            if self._lineShorteningActive:
                # Given a text, split the text in lines. Lines that are extremely
                # long are split in pieces of 80 characters to increase performance for
                # wrapping. This is kind of a failsafe for when the user accidentally
                # prints a bitmap or huge list. See https://github.com/pyzo/pyzo/issues/98

                if indLine == 0:
                    charsRightOfCursor = posInBlock
                    if posInBlock > shortLineLength:
                        # we also shorten the existing line
                        cursor.movePosition(
                            cursor.MoveOperation.Left, A_MOVE, posInBlock
                        )
                        multiples, rem = divmod(charsRightOfCursor, shortLineLength)
                        for i in range(multiples):
                            cursor.movePosition(
                                cursor.MoveOperation.Right, A_MOVE, shortLineLength
                            )
                            cursor.insertText("\n")
                        cursor.movePosition(cursor.MoveOperation.Right, A_MOVE, rem)
                        posInBlock = rem
                else:
                    posInBlock = 0

                lineNew = []
                for elem in line[:-1]:
                    if isinstance(elem, str):
                        n = len(elem)
                        if posInBlock + n > shortLineLength:
                            i = shortLineLength - posInBlock
                            tillOffset, chunkList, remnant = (
                                self._splitStringIntoChunks(elem, i, shortLineLength)
                            )
                            lineNew.append(tillOffset)
                            lineNew.append("\n")
                            lineNew.append(posInBlock + len(tillOffset) + 1)
                            posInBlock = 0
                            finishedLinesShortened.append(lineNew)

                            for chunk in chunkList:
                                finishedLinesShortened.append(
                                    [chunk, "\n", shortLineLength + 1]
                                )

                            lineNew = [remnant]
                            posInBlock = len(remnant)
                        else:
                            lineNew.append(elem)
                            posInBlock += n
                    else:
                        lineNew.append(elem)  # this elem is a QTextCharFormat
                lineNew.append(posInBlock)
                finishedLinesShortened.append(lineNew)
            else:
                finishedLinesShortened.append(line)

        return finishedLinesShortened

    def parseFormat(self, text, currentFormat, defaultFormat):
        """process ANSI escape codes for text formatting

        We only support a small subset of:
        http://en.wikipedia.org/wiki/ANSI_escape_code
        """
        mo = self._reFormatPattern.match(text)
        if not mo:
            return None, None
        format = QtGui.QTextCharFormat(currentFormat)
        params = [int(i) for i in mo[2].split(";")]
        matchLen = len(mo[1])
        for param in params:
            if param == 0:
                format = QtGui.QTextCharFormat(defaultFormat)
            elif param == 1:
                format.setFontWeight(QtGui.QFont.Weight.Bold)
            elif param == 2:
                format.setFontWeight(QtGui.QFont.Weight.Light)
            elif param == 3:
                format.setFontItalic(True)  # italic
            elif param == 4:
                format.setFontUnderline(True)  # underline
            elif param == 22:
                format.setFontWeight(QtGui.QFont.Weight.Normal)  # not bold or light
            elif param == 23:
                format.setFontItalic(False)  # not italic
            elif param == 24:
                format.setFontUnderline(False)  # not underline
            elif 30 <= param <= 37:  # set foreground color
                clr = self.COLORS[param - 30]
                format.setForeground(QtGui.QColor(clr))
            elif param == 39:  # reset the foreground color
                format.setForeground(defaultFormat.foreground().color())
            elif 40 <= param <= 47:
                pass  # cannot set background text in QPlainTextEdit
            else:
                pass  # not supported
        return format, matchLen


class PythonShell(BaseShell):
    """The PythonShell class implements the python part of the shell
    by connecting to a remote process that runs a Python interpreter.
    """

    # Emits when the status string has changed or when receiving a new prompt
    stateChanged = QtCore.Signal(BaseShell)

    # Emits when the debug status is changed
    debugStateChanged = QtCore.Signal(BaseShell)

    def __init__(self, parent, info):
        super().__init__(parent)

        # Get standard info if not given.
        if info is None and pyzo.config.shellConfigs2:
            info = pyzo.config.shellConfigs2[0]
        if not info:
            info = KernelInfo(None)

        # Store info so we can reuse it on a restart
        self._info = info

        # For the editor to keep track of attempted imports
        self._importAttempts = []

        # To keep track of the response for introspection
        self._currentCTO = None
        self._currentACO = None

        # Write buffer to store messages in for writing
        self._write_buffer = None

        # Create timer to keep polling any results
        # todo: Maybe use yoton events to process messages as they arrive.
        # I tried this briefly, but it seemd to be less efficient because
        # messages are not so much bach-processed anymore. We should decide
        # on either method.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_TIMER_INTERVAL_IDLE)  # ms
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

        # Add context menu
        self._menu = ShellContextMenu(shell=self, parent=self)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p: self._menu.popup(self.mapToGlobal(p + QtCore.QPoint(0, 3)))
        )

        # Keep track of breakpoints
        pyzo.editors.breakPointsChanged.connect(self.sendBreakPoints)

        # Start!
        self.resetVariables()
        self.connectToKernel(info)

    def resetVariables(self):
        """Resets some variables."""

        # Reset read state
        self.setReadOnly(False)

        # Variables to store state, python version, builtins and keywords
        self._state = ""
        self._debugState = {}
        self._version = ""
        self._builtins = []
        self._keywords = []
        self._softKeywords = []
        self._startup_info = {}
        self._start_time = 0

        # (re)set import attempts
        self._importAttempts[:] = []

        # Update
        self.stateChanged.emit(self)

    def connectToKernel(self, info):
        """Create kernel and connect to it."""

        # Create yoton context
        self._context = ct = yoton.Context()

        # Create stream channels
        self._strm_out = yoton.SubChannel(ct, "strm-out")
        self._strm_err = yoton.SubChannel(ct, "strm-err")
        self._strm_raw = yoton.SubChannel(ct, "strm-raw")
        self._strm_echo = yoton.SubChannel(ct, "strm-echo")
        self._strm_prompt = yoton.SubChannel(ct, "strm-prompt")
        self._strm_broker = yoton.SubChannel(ct, "strm-broker")
        self._strm_action = yoton.SubChannel(ct, "strm-action", yoton.OBJECT)

        # Set channels to sync mode. This means that if Pyzo cannot process
        # the messages fast enough, the sending side is blocked for a short
        # while. We don't want our users to miss any messages.
        for c in [self._strm_out, self._strm_err]:
            c.set_sync_mode(True)

        # Create control channels
        self._ctrl_command = yoton.PubChannel(ct, "ctrl-command")
        self._ctrl_code = yoton.PubChannel(ct, "ctrl-code", yoton.OBJECT)
        self._ctrl_broker = yoton.PubChannel(ct, "ctrl-broker")

        # Create status channels
        self._stat_interpreter = yoton.StateChannel(ct, "stat-interpreter")
        self._stat_cd = yoton.StateChannel(ct, "stat-cd")
        self._stat_debug = yoton.StateChannel(ct, "stat-debug", yoton.OBJECT)
        self._stat_startup = yoton.StateChannel(ct, "stat-startup", yoton.OBJECT)
        self._stat_breakpoints = yoton.StateChannel(
            ct, "stat-breakpoints", yoton.OBJECT
        )

        self._stat_startup.received.bind(self._onReceivedStartupInfo)

        # Create introspection request channel
        self._request = yoton.ReqChannel(ct, "reqp-introspect")

        # Connect! The broker will only start the kernel AFTER
        # we connect, so we do not miss out on anything.
        slot = pyzo.localKernelManager.createKernel(finishKernelInfo(info))
        self._brokerConnection = ct.connect("localhost:{}".format(slot))
        self._brokerConnection.closed.bind(self._onConnectionClose)

        # Force updating of breakpoints
        pyzo.editors.updateBreakPoints()

        # todo: see polling vs events

    #         # Detect incoming messages
    #         for c in [self._strm_out, self._strm_err, self._strm_raw,
    #                 self._strm_echo, self._strm_prompt, self._strm_broker,
    #                 self._strm_action,
    #                 self._stat_interpreter, self._stat_debug]:
    #             c.received.bind(self.poll)

    def get_kernel_cd(self):
        """Get current working dir of kernel."""
        return self._stat_cd.recv()

    def _onReceivedStartupInfo(self, channel):
        startup_info = channel.recv()

        # Store the whole dict
        self._startup_info = startup_info

        # Store when we received this
        self._start_time = time.time()

        # Set version
        version = startup_info.get("version", None)
        if isinstance(version, tuple):
            if version < (3,):
                self.setParser("python2")
            else:
                self.setParser("python3")
            self._version = "{}.{}".format(*version[:2])

        # Set keywords
        L = startup_info.get("keywords", None)
        if isinstance(L, list):
            self._keywords = L

        # Set soft keywords
        L = startup_info.get("softkeywords", None)
        if isinstance(L, list):
            self._softKeywords = L

        # Set builtins
        L = startup_info.get("builtins", None)
        if isinstance(L, list):
            self._builtins = L

        # Notify
        self.stateChanged.emit(self)

    ## Introspection processing methods

    def processCallTip(self, cto):
        """Processes a calltip request using a CallTipObject instance."""

        # Try using buffer first (not if we're not the requester)
        if self is cto.textCtrl:
            if cto.tryUsingBuffer():
                return

        # Clear buffer to prevent doing a second request
        # and store cto to see whether the response is still wanted.
        cto.setBuffer("")
        self._currentCTO = cto

        # Post request
        if cto.useIntermediateResult:
            future = self._request.signatureWithIntermediateResult(cto.name)
        else:
            future = self._request.signature(cto.name)
        future.add_done_callback(self._processCallTip_response)
        future.cto = cto

    def _processCallTip_response(self, future):
        """Process response of shell to show signature."""

        # Process future
        if future.cancelled():
            # print('Introspect cancelled')  # No kernel
            return
        elif future.exception():
            print("Introspect-exception: ", future.exception())
            return
        else:
            response = future.result()
            cto = future.cto

        # First see if this is still the right editor (can also be a shell)
        editor1 = pyzo.editors.getCurrentEditor()
        editor2 = pyzo.shells.getCurrentShell()
        if cto.textCtrl not in [editor1, editor2]:
            # The editor or shell starting the autocomp is no longer active
            cto.textCtrl.autocompleteCancel()
            return

        # Invalid response
        if not response:
            cto.textCtrl.autocompleteCancel()
            return

        if cto.useIntermediateResult:
            assert response.startswith("__pyzo__calltip")
            response = cto.name + response[len("__pyzo__calltip") :]
        # If still required, show tip, otherwise only store result
        if cto is self._currentCTO:
            cto.finish(response)
        else:
            cto.setBuffer(response)

    def processAutoComp(self, aco):
        """Processes an autocomp request using an AutoCompObject instance."""

        # Try using buffer first (not if we're not the requester)
        if self is aco.textCtrl:
            if aco.tryUsingBuffer():
                return

        # Include builtins and keywords?
        if not aco.name:
            aco.addNames(self._builtins)
            if pyzo.config.settings.autoComplete_keywords:
                aco.addNames(self._keywords)
                aco.addNames(self._softKeywords)
        elif aco.name == "[":
            return

        # Set buffer to prevent doing a second request
        # and store aco to see whether the response is still wanted.
        aco.setBuffer()
        self._currentACO = aco

        # Post request
        if aco.useIntermediateResult:
            if aco.name.endswith("["):  # if key auto-completion
                future = self._request.dir2WithIntermediateResult(aco.name[:-1])
            else:
                future = self._request.dirWithIntermediateResult(aco.name)
        else:
            if aco.name.endswith("["):  # if attribute or name auto-completion
                future = self._request.dir2(aco.name[:-1])
            else:
                future = self._request.dir(aco.name)
        future.add_done_callback(self._processAutoComp_response)
        future.aco = aco

    def _processAutoComp_response(self, future):
        """Process the response of the shell for the auto completion."""

        # Process future
        if future.cancelled():
            # print('Introspect cancelled')  # No living kernel
            return
        elif future.exception():
            print("Introspect-exception: ", future.exception())
            return
        else:
            response = future.result()
            aco = future.aco

        # First see if this is still the right editor (can also be a shell)
        editor1 = pyzo.editors.getCurrentEditor()
        editor2 = pyzo.shells.getCurrentShell()
        if aco.textCtrl not in [editor1, editor2]:
            # The editor or shell starting the autocomp is no longer active
            aco.textCtrl.autocompleteCancel()
            return

        # Add result to the list
        foundNames = []
        if response is not None:
            if aco.name.endswith("["):  # if key auto-completion
                foundNames = [
                    name[1:-1]
                    for name, type_, kind, repr_ in response
                    if name[0] == "["
                ]
                maxKeyEntries = 200
                if len(foundNames) > maxKeyEntries:
                    foundNames = (
                        foundNames[: maxKeyEntries - 1]
                        + ["# ... too many entries"]
                        + foundNames[-1:]
                    )
            else:  # if attribute or name auto-completion
                foundNames = response
        aco.addNames(foundNames)

        # Process list
        if aco.name and not foundNames and not aco.names:
            # No names found for the requested name, and no names from
            # fictive class members (via code parser in the editor).
            # Let's try to import it.
            importNames, importLines = pyzo.parser.getFictiveImports(editor1)
            baseName = aco.nameInImportNames(importNames)
            if baseName:
                line = importLines[baseName].strip()
                if line not in self._importAttempts:
                    # Do import
                    self.processLine(line + "  # auto-import")
                    self._importAttempts.append(line)
                    # Wait a barely noticable time to increase the chances
                    # that the import is complete when we repost the request.
                    time.sleep(0.2)
                    # To be sure, decrease the expiration date on the buffer
                    aco.setBuffer(timeout=1)

                    # repost request; self._importAttempts will prevent infinite loop
                    self.processAutoComp(aco)
        else:
            # If still required, show list, otherwise only store result
            if self._currentACO is aco:
                aco.finish()
            else:
                aco.setBuffer()

    ## Methods for executing code

    def executeCommand(self, text):
        """Execute one-line command in the remote Python session."""

        # Ensure edit line is selected (to reset scrolling to end)
        self.ensureCursorAtEditLine()

        self._ctrl_command.send(text)

    def executeCode(self, text, fname, lineno=None, cellName=None, changeDir=False):
        """Execute (run) a large piece of code in the remote shell.

        text: the source code to execute
        filename: the file from which the source comes
        lineno: the first lineno of the text in the file, where 0 would be
        the first line of the file...

        The text (source code) is first pre-processed:
        - convert all line-endings to \n
        - remove all empty lines at the end
        - remove commented lines at the end
        - convert tabs to spaces
        - dedent so minimal indentation is zero
        """

        # Convert tabs to spaces
        text = text.replace("\t", " " * 4)

        # Make sure there is always *some* text
        if not text:
            text = " "

        if lineno is None:
            lineno = 0
            cellName = fname  # run all

        # Examine the text line by line...
        # - check for empty/commented lined at the end
        # - calculate minimal indentation
        lines = text.splitlines()
        lastLineOfCode = 0
        minIndent = 99
        for linenr, line in enumerate(lines):
            # Check if empty (can be commented, but nothing more)
            tmp = line.split("#", 1)[0]  # get part before first #
            if tmp.count(" ") == len(tmp):
                continue  # empty line, proceed
            else:
                lastLineOfCode = linenr
            # Calculate indentation
            tmp = line.lstrip(" ")
            indent = len(line) - len(tmp)
            if indent < minIndent:
                minIndent = indent

        # Copy all proper lines to a new list,
        # remove minimal indentation, but only if we then would only remove
        # spaces (in the case of commented lines)
        lines2 = []
        for linenr in range(lastLineOfCode + 1):
            line = lines[linenr]
            # Remove indentation,
            if line[:minIndent].count(" ") == minIndent:
                line = line[minIndent:]
            else:
                line = line.lstrip(" ")
            lines2.append(line)

        # Ensure edit line is selected (to reset scrolling to end)
        self.ensureCursorAtEditLine()

        # Send message
        text = "\n".join(lines2)
        msg = {
            "source": text,
            "fname": fname,
            "lineno": lineno,
            "cellName": cellName,
            "changeDir": int(changeDir),
        }
        self._ctrl_code.send(msg)

    def sendBreakPoints(self, breaks):
        """Send all breakpoints."""
        # breaks is a dict of filenames to integers
        self._stat_breakpoints.send(breaks)

    ## The polling methods and terminating methods

    def poll(self, channel=None):
        """To keep the shell up-to-date. Call this periodically."""
        idle = True

        if self._write_buffer:
            # There is still data in the buffer
            sub, M = self._write_buffer
        else:
            # Check what subchannel has the latest message pending
            sub = yoton.select_sub_channel(
                self._strm_out,
                self._strm_err,
                self._strm_echo,
                self._strm_raw,
                self._strm_broker,
                self._strm_prompt,
            )
            # Read messages from it
            if sub:
                M = sub.recv_selected()
                # M = [sub.recv()] # Slow version (for testing)
            # New prompt?
            if sub is self._strm_prompt:
                self.stateChanged.emit(self)

        # Write all pending messages that are later than any other message
        if sub:
            idle = False
            # Select messages to process
            N = 256
            M, buffer = M[:N], M[N:]
            # Buffer the rest
            if buffer:
                self._write_buffer = sub, buffer
            else:
                self._write_buffer = None
            # Get how to deal with prompt
            prompt = 0
            if sub is self._strm_echo:
                prompt = 1
            elif sub is self._strm_prompt:
                prompt = 2
                M = M[-1:]  # only use the newest prompt
            # Get color
            color = None
            if sub is self._strm_broker:
                color = "#fff" if pyzo.darkSyntax else "#000"
            elif sub is self._strm_raw:
                color = "#bbb" if pyzo.darkSyntax else "#888888"
            elif sub is self._strm_err:
                color = "#f00"
            # Write
            self.write("".join(M), prompt, color, sub)

        # Do any actions?
        action = self._strm_action.recv(False)
        if action:
            idle = False
            if action == "cls":
                self.clearScreen()
            elif action.startswith("open "):
                parts = action.split(" ")
                parts.pop(0)
                try:
                    linenr = int(parts[0])
                    parts.pop(0)
                except ValueError:
                    linenr = None
                fname = " ".join(parts)
                editor = pyzo.editors.loadFile(fname)
                if editor and linenr:
                    editor._editor.gotoLine(linenr)
            else:
                print("Unknown action: {}".format(action))

        # ----- status

        newInterval = POLL_TIMER_INTERVAL_IDLE if idle else POLL_TIMER_INTERVAL_BUSY
        if self._timer.interval() != newInterval:
            self._timer.setInterval(newInterval)

        # Do not update status when the kernel is not really up and running
        # self._version is set when the startup info is received
        if not self._version:
            return

        # Update status
        state = self._stat_interpreter.recv()
        if state != self._state:
            self._state = state
            self.stateChanged.emit(self)

        # Update debug status
        state = self._stat_debug.recv()
        if state != self._debugState:
            self._debugState = state
            self.debugStateChanged.emit(self)

    def interrupt(self):
        """Send a Keyboard interrupt signal to the main thread of the remote process."""
        # Ensure edit line is selected (to reset scrolling to end)
        self.ensureCursorAtEditLine()

        self._ctrl_broker.send("INT")

    def pause(self):
        """Send a pause signal to the main thread of the remote process."""
        # Ensure edit line is selected (to reset scrolling to end)
        self.ensureCursorAtEditLine()

        self._ctrl_broker.send("PAUSE")

    def restart(self, scriptFile=None):
        """Terminate the shell, after which it is restarted.

        Args can be a filename, to execute as a script as soon as the
        shell is back up.
        """
        self.resetShellWriters()

        # Ensure edit line is selected (to reset scrolling to end)
        self.ensureCursorAtEditLine()

        # Get info
        info = finishKernelInfo(self._info, scriptFile)

        # Create message and send
        msg = "RESTART\n" + ssdf.saves(info)
        self._ctrl_broker.send(msg)

        # Reset
        self.resetVariables()

    def terminate(self):
        """Terminates the python process.

        It will first try gently, but if that does not work, the process
        shall be killed. To be notified of the termination, connect to the
        "terminated" signal of the shell.
        """
        # Ensure edit line is selected (to reset scrolling to end)
        self.ensureCursorAtEditLine()

        self._ctrl_broker.send("TERM")

    def closeShell(self):  # do not call it close(); that is a reserved method.
        """This closes the shell.

        If possible, we will first tell the broker to terminate the kernel.

        The broker will be cleaned up if there are no clients connected
        and if there is no active kernel. In a multi-user environment,
        we should thus be able to close the shell without killing the
        kernel. But in a closed 1-to-1 environment we really want to
        prevent loose brokers and kernels dangling around.

        In both cases however, it is the responsibility of the broker to
        terminate the kernel, and the shell will simply assume that this
        will work :)
        """

        pyzo.editors.breakPointsChanged.disconnect(self.sendBreakPoints)

        # If we can, try to tell the broker to terminate the kernel
        if self._context and self._context.connection_count:
            self.terminate()
            self._context.flush()  # Important, make sure the message is sent!
            self._context.close()

        # Adios
        pyzo.shells.removeShell(self)

        # This object (self) still stays in memory after closing the shell (memory leak).
        # At least, we try to clean up as much as possible.
        self._timer.stop()
        self._timer.timeout.disconnect()
        self.customContextMenuRequested.disconnect()

    def _onConnectionClose(self, c, why):
        """To be called after disconnecting.

        In general, the broker will not close the connection, so it can
        be considered an error-state if this function is called.
        """

        # Stop context
        if self._context:
            self._context.close()

        # New (empty prompt)
        self._cursor1.movePosition(self._cursor1.MoveOperation.End, A_MOVE)
        self._cursor2.movePosition(self._cursor2.MoveOperation.End, A_MOVE)

        self.write("\n\n")
        self.write("Lost connection with broker:\n")
        self.write(why)
        self.write("\n\n")

        # Set style to indicate dead-ness
        self.setReadOnly(True)

        # Goto end such that the closing message is visible
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End, A_MOVE)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


if __name__ == "__main__":
    b = BaseShell(None)
    b.show()
