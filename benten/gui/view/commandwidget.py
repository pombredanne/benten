"""This handles the command line bar and the response log.
It consists of a single-line text edit on top. You enter commands into this box.
Below it is a read only multiline text box that shows the response to the commands.
The command is echoed and the response printed below.
"""
import pathlib
from shlex import split

from PySide2.QtCore import Qt, Slot, Signal
from PySide2.QtWidgets import QVBoxLayout, QLineEdit, QTextEdit, QWidget
from PySide2.QtGui import QTextCursor, QFontDatabase, QKeyEvent

from ...editing.edit import Edit, EditMark
from ...editing.lineloader import load_yaml
from ...editing.yamlview import YamlView

from ..ace.editor import Editor

import logging

logger = logging.getLogger(__name__)


# Decorator that we use to add in information for the command, like help etc.
def meta(cmd, cmd_help, syn=None):
    def wrap(func):
        func.is_command = True
        func.cmd = cmd
        func.help = cmd_help
        func.syn = syn or []
        return func
    return wrap


def only_clt(func):
    def cmd(self, args):
        if self.process_type() == "CommandLineTool":
            return func(self, args)
        else:
            return "Can only do this on CommandLineTool: This is {}".format(self.process_type())
    return cmd


def only_wf(func):
    def cmd(self, args):
        if self.process_type() == "Workflow":
            return func(self, args)
        else:
            return "Can only do this on Workflow"
    return cmd


class CommandLine(QLineEdit):
    """Adds history and history navigation"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = [""]
        self.history_n = 0

        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setToolTip("Enter commands here")
        self.returnPressed.connect(self.command_entered)

    def keyPressEvent(self, event:QKeyEvent):
        if event.key() == Qt.Key_Up:
            self.history_n -= 1
            if self.history_n < 0: self.history_n = len(self.history) - 1
        elif event.key() == Qt.Key_Down:
            self.history_n += 1
            if self.history_n > len(self.history) - 1: self.history_n = 0
        else:
            return super().keyPressEvent(event)

        if -1 < self.history_n < len(self.history):
            self.setText(self.history[self.history_n])

    @Slot()
    def command_entered(self):
        if self.text() not in self.history:
            self.history.append(self.text())
        self.history_n = len(self.history)


class CommandWidget(QWidget):

    proposed_edit = Signal(object)
    step_selected = Signal(str)

    def __init__(self, editor: Editor, parent=None):
        QWidget.__init__(self, parent=parent)

        self.editor = editor
        # We need a reference to the editor since several commands operate on the raw text both
        # reading and writing back to the original document

        self.command_line = CommandLine()
        self.command_line.returnPressed.connect(self.command_entered)

        self.command_log = QTextEdit()
        self.command_log.setReadOnly(True)
        self.command_line.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

        self.command_log.setToolTip("Command and response history")

        layout = QVBoxLayout()
        layout.addWidget(self.command_line)
        layout.addWidget(self.command_log)
        layout.setMargin(0)
        layout.setSpacing(0)
        self.setLayout(layout)

        self.dispatch_table, self.help_table = self.set_up_dispatcher()

    def set_up_dispatcher(self):
        table = {
            "help": {
                "synonyms": ["?", "h"],
                "help": "help: Print help",
                "call": self.print_help
            }
        }

        for func_name in sorted(self.__dir__()):
            fn = getattr(self, func_name)
            if hasattr(fn, "is_command"):
                logger.debug("Installed command  '{}'".format(fn.cmd))
                table[fn.cmd] = {
                    "synonyms": fn.syn,
                    "help": fn.help,
                    "call": fn
                }

        expanded_table = {}
        for k, v in table.items():
            for _k in [k] + v.get("synonyms", []):
                expanded_table[_k] = v

        return expanded_table, table

    @Slot()
    def command_entered(self):
        cmd_line = self.command_line.text()
        cmd, *arguments = split(cmd_line)

        if cmd in self.dispatch_table:
            try:
                response = str(self.dispatch_table[cmd]["call"](arguments))
            except Exception as e:
                response = f"Command error:\n{e}"
        else:
            response = "Unknown command: {}".format(cmd)

        self.command_line.clear()
        self.response_received(cmd_line, response)

    @Slot(str, str)
    def response_received(self, cmd_line, response):
        self.command_log.moveCursor(QTextCursor.End)
        self.command_log.insertPlainText("\n" + f"$> {cmd_line}\n")
        self.command_log.moveCursor(QTextCursor.End)
        self.command_log.insertPlainText(response)
        self.command_log.moveCursor(QTextCursor.End)

    def print_help(self, args):
        return "\n".join("{}\n{}".format(
            v["help"], "Synonyms: {}\n".format(v.get("synonyms")) if v.get("synonyms") else "")
                         for k, v in self.help_table.items())

    #
    # All the built-in commands
    #

    @meta(
        cmd="sbpush",
        cmd_help="sbpush <message> [project/id]  \nPush this app to the platform")
    def sbpush(self, args):

        if not (0 < len(args) < 3):
            return "Arguments are <message> [project/id]"

        commit_message = args[0]
        app_path = args[1] if len(args) > 1 else None

        from ...sbg.push import push

        app = push(self.editor.config.api, load_yaml(self.editor.cached_text), commit_message, app_path)
        logger.debug("Pushed app and git back app id: {}".format(app.raw["sbg:id"]))
        return "Pushed app to {}\n".format(app.id)

    @meta(
        cmd="docker",
        cmd_help="docker <docker_image> : Add DockerRequirement to CLT")
    @only_clt
    def docker_scaffold(self, args):
        if len(args) != 1:
            return "Incorrect number of arguments"

        docker_image = args[0]
        edit = self.attached_view.doc.insert_into_lom(
            path=("requirements",),
            key="DockerRequirement",
            key_field="class",
            entry="dockerPull: {}\n".format(docker_image)
        )

        self.proposed_edit.emit(edit)
        return "Added DockerRequirement"

    @meta(
        cmd="add",
        cmd_help="add <step_id> [path] : Create scaffold for new inline step, "
                 "or, if path is given new linked step.")
    @only_wf
    def add_step_scaffold(self, args):
        if not 0 < len(args) < 3:
            return "Arguments are <step_id> [path]"

        step_id = args[0]
        path = pathlib.Path(args[1]) if len(args) > 1 else None

        scaffold_path = pathlib.Path(
            self.config.getpath("cwl", "template_dir"), "step-template.cwl")

        if not scaffold_path.exists():
            return "Couldn't find step scaffold file {}".format(scaffold_path)

        scaffold = scaffold_path.open("r").read()
        rel_path = self.attached_view.get_root().get_rel_path(path) if path else None

        edit = self.process_model.add_step(
            step_scaffold=scaffold, rel_path=rel_path, path=path, step_id=step_id)

        self.proposed_edit.emit(edit)
        return "Added step"




    # def list_steps(self, args):
    #     if self.bw.cwl_doc.process_type() in ["CommandLineTool", "ExpressionTool"]:
    #         return "This process type does not contain steps"
    #
    #     return "Steps:\n" + "\t\n".join(step for step in self.bw.cwl_doc.cwl_dict["steps"].keys())
    #
    # def goto_step(self, args):
    #     if self.bw.cwl_doc.process_type() in ["CommandLineTool", "ExpressionTool"]:
    #         return "This process type does not contain steps"
    #
    #     return self.bw.highlight_step(args[0], focus_conn=False)
    #
    # def get_app_revisions(self, args):
    #     return "\n".join(
    #         "v{}: {} - {}".format(rev["sbg:revision"], rev["sbg:revisionNotes"], rev["sbg:modifiedBy"])
    #         for rev in self.bw.cwl_doc.get_app_revisions())
