"""
This module is an example of a barebones function plugin for napari

It implements the ``napari_experimental_provide_function`` hook specification.
see: https://napari.org/docs/dev/plugins/hook_specifications.html

Replace code below according to your needs.
"""
import os
import re
from typing import Callable
import imagej
from scyjava import config, jimport
from qtpy.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QPushButton,
    QLineEdit,
    QTableWidget,
    QAbstractItemView,
    QHeaderView,
    QTableWidgetItem,
    QLabel,
)
from magicgui import magicgui
from napari import Viewer
from napari_imagej._ptypes import PTypes
from inspect import signature, Parameter, Signature


import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # TEMP

# TEMP: Avoid issues caused by https://github.com/imagej/pyimagej/issues/160
config.add_repositories(
    {"scijava.public": "https://maven.scijava.org/content/groups/public"}
)
config.endpoints.append("io.scif:scifio:0.43.1")

logger.debug("Initializing ImageJ2")
config.add_option(f"-Dimagej.dir={os.getcwd()}")  # TEMP
ij = imagej.init(headless=False)
logger.debug(f"Initialized at version {ij.getVersion()}")
ij.log().setLevel(4)

Object = jimport("java.lang.Object")
getClass = Object.class_.getMethod("getClass")


def which_class(o):
    return getClass.invoke(o)


PreprocessorPlugin = jimport("org.scijava.module.process.PreprocessorPlugin")
preprocessors = ij.plugin().createInstancesOfType(PreprocessorPlugin)
PostprocessorPlugin = jimport("org.scijava.module.process.PostprocessorPlugin")
postprocessors = ij.plugin().createInstancesOfType(PostprocessorPlugin)
InputHarvester = jimport("org.scijava.widget.InputHarvester")
LoadInputsPreprocessor = jimport("org.scijava.module.process.LoadInputsPreprocessor")
Initializable = jimport("net.imagej.ops.Initializable")

_ptypes = PTypes()


# TODO: Move this function to scyjava.convert and/or ij.py.
def _ptype(java_type):
    for jtype, ptype in _ptypes.ptypes.items():
        if jtype.class_.isAssignableFrom(java_type):
            return ptype
    for jtype, ptype in _ptypes.ptypes.items():
        if ij.convert().supports(java_type, jtype):
            return ptype
    raise ValueError(f"Unsupported Java type: {java_type}")


def _return_type(info):
    out_types = [o.getType() for o in info.outputs()]
    if len(out_types) == 0:
        return None
    if len(out_types) == 1:
        return _ptype(out_types[0])
    return dict


def _preprocess_non_inputs(module):
    """Uses all preprocessors up to the InputHarvesters."""
    # preprocess using plugin preprocessors
    logging.debug("Preprocessing...")
    # for i in preprocessors:
    for preprocessor in preprocessors:
        if isinstance(preprocessor, InputHarvester) or isinstance(
            preprocessor, LoadInputsPreprocessor
        ):
            # STOP AT INPUT HARVESTING
            break
        else:
            preprocessor.process(module)


def _preprocess_remaining_inputs(
    module, inputs, unresolved_inputs, user_resolved_inputs
):
    """Resolves each input in unresolved_inputs"""
    resolved_java_args = ij.py.jargs(*user_resolved_inputs)
    # resolve remaining inputs
    for i in range(len(unresolved_inputs)):
        name = unresolved_inputs[i].getName()
        obj = resolved_java_args[i]
        module.setInput(name, obj)
        module.resolveInput(name)

    # sanity check: ensure all inputs resolved
    for input in inputs:
        if not module.isInputResolved(input.getName()):
            print("Input ", input.getName(), " is not resolved!")

    return resolved_java_args


def _filter_unresolved_inputs(module, inputs):
    """Returns a list of all inputs that can only be resolved by the user."""
    # Grab all unresolved inputs
    unresolved_inputs = list(
        filter(lambda i: not module.isResolved(i.getName()), inputs)
    )
    # Delegate optional output construction to the module
    # We will leave those unresolved
    unresolved_inputs = list(
        filter(lambda i: not (i.isOutput() and not i.isRequired()), unresolved_inputs)
    )
    return unresolved_inputs


def _initialize_module(module):
    """Initializes the passed module."""
    try:
        module.initialize()
        # HACK: module.initialize() does not seem to call Initializable.initialize()
        if isinstance(module.getDelegateObject(), Initializable):
            module.getDelegateObject().initialize()
    except Exception as e:
        print("Initialization Error")
        print(e.stacktrace())


def _run_module(module):
    """Runs the passed module."""
    try:
        module.run()
    except Exception as e:
        print("Run Error")
        print(e.stacktrace())


def _postprocess_module(module):
    """Runs all known postprocessors on the passed module."""
    for postprocessor in postprocessors:
        postprocessor.process(module)


# Credit: https://gist.github.com/xhlulu/95117e225b7a1aa806e696180a72bdd0

def _param_from_module_item(item):
    kwargs = {
        "name": ij.py.from_java(item.getName()),
        "kind": Parameter.POSITIONAL_OR_KEYWORD,
        "annotation": _ptype(item.getType())
    }
    if not item.isRequired():
        kwargs["default"] = ij.py.from_java(item.getDefaultValue())
    p = Parameter(**kwargs)
    return p


def _modify_function_signature(function, inputs, module_info):
    """Rewrites the passed function with type annotations for all I/O items in the module."""
    try:
        sig: Signature = signature(function)
        # Grab all options after the module inputs
        module_params = [_param_from_module_item(i) for i in inputs]
        other_params = list(sig.parameters.values())[1:]
        all_params = module_params + other_params
        function.__signature__ = sig.replace(
            parameters=all_params, return_annotation=_return_type(module_info)
        )
    except Exception as e:
        print(e)


def _module_output(module):
    """Gets the output of the module, or None if the module has no output."""
    outputs = module.getOutputs()
    output_entry = outputs.entrySet().stream().findFirst()
    if not output_entry.isPresent():
        return None
    output_value = output_entry.get().getValue()
    return output_value


def _functionify_module_execution(module, info, viewer: Viewer) -> Callable:
    """Converts a module into a Widget that can be added to napari."""
    # Run preprocessors until we hit input harvesting
    _preprocess_non_inputs(module)

    # Determine which inputs must be resolved by the user
    unresolved_inputs = _filter_unresolved_inputs(module, info.inputs())

    # Package the rest of the execution into a widget
    def execute_module(*user_resolved_inputs, display_results_in_new_window: bool):

        # Resolve remaining inputs
        resolved_java_args = _preprocess_remaining_inputs(
            module, info.inputs(), unresolved_inputs, user_resolved_inputs
        )

        # run module
        logger.debug(
            f"run_module: {execute_module.__qualname__} \
                ({resolved_java_args}) -- {info.getIdentifier()}"
        )
        _initialize_module(module)
        _run_module(module)

        # postprocess
        _postprocess_module(module)

        # get output
        logger.debug("run_module: execution complete")
        result = _module_output(module)
        logger.debug(f"run_module: result = {result}")
        if not _ptypes.displayable_in_napari(result):

            from inspect import signature, Signature

            def show_tabular_output():
                return ij.py.from_java(result.getRealDouble())

            sig: Signature = signature(show_tabular_output)
            show_tabular_output.__signature__ = sig.replace(
                return_annotation=_return_type(info)
            )
            result_widget = magicgui(
                show_tabular_output, result_widget=True, auto_call=True
            )

            if display_results_in_new_window:
                result_widget.show(run=True)
            else:
                name = "Result: " + ij.py.from_java(info.getTitle())
                viewer.window.add_dock_widget(result_widget, name=name)
            result_widget.update()

        return (
            ij.py.from_java(result) if _ptypes.displayable_in_napari(result) else result
        )

    # Format napari metadata
    menu_string = " > ".join(str(p) for p in info.getMenuPath())
    execute_module.__doc__ = f"Invoke ImageJ2's {menu_string} command"
    execute_module.__name__ = re.sub("[^a-zA-Z0-9_]", "_", menu_string)
    execute_module.__qualname__ = menu_string

    # Rewrite the function signature to match the module inputs.
    _modify_function_signature(execute_module, unresolved_inputs, info)

    # Add the type hints as annotations metadata as well.
    # Without this, magicgui doesn't pick up on the types.
    type_hints = {str(i.getName()): _ptype(i.getType()) for i in unresolved_inputs}
    out_types = [o.getType() for o in info.outputs()]
    type_hints["return"] = _ptype(out_types[0]) if len(out_types) == 1 else dict
    execute_module.__annotation__ = type_hints

    execute_module._info = info
    return execute_module


class ImageJWidget(QWidget):
    """The top-level ImageJ widget for napari."""
    def __init__(self, napari_viewer: Viewer):
        super().__init__()
        self.viewer = napari_viewer

        self.setLayout(QVBoxLayout())

        # Search Bar
        searchWidget = QWidget()
        searchWidget.setLayout(QHBoxLayout())
        searchWidget.layout().addWidget(self._generate_searchbar())

        self.layout().addWidget(searchWidget)

        self.searcher = self._generate_searcher()
        self.searchService = self._generate_search_service()

        # Results box
        labels = ["Module: "]
        self.results = []
        self.maxResults = 12
        self.tableWidget = QTableWidget(self.maxResults, len(labels))
        self.tableWidget.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tableWidget.setHorizontalHeaderLabels(labels)
        self.tableWidget.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tableWidget.verticalHeader().hide()
        self.tableWidget.setShowGrid(False)
        self.tableWidget.cellClicked.connect(self._highlight_module)
        self.layout().addWidget(self.tableWidget)

        # Module highlighter
        self.focus_widget = QWidget()
        self.focus_widget.setLayout(QVBoxLayout())
        self.focused_module = QLabel()
        self.focus_widget.layout().addWidget(self.focused_module)
        self.focused_module.setText("Display Module Here")
        self.layout().addWidget(self.focus_widget)
        self.focused_action_buttons = []

    def _generate_searchbar(self):
        searchbar = QLineEdit()
        searchbar.textChanged.connect(self._search)
        searchbar.returnPressed.connect(lambda: self._highlight_module(0, 0))
        return searchbar

    def _generate_searcher(self):
        pluginService = ij.get("org.scijava.plugin.PluginService")
        moduleServiceCls = jimport("org.scijava.search.module.ModuleSearcher")
        searcherCls = jimport("org.scijava.search.Searcher")
        info = pluginService.getPlugin(moduleServiceCls, searcherCls)
        searcher = info.createInstance()
        ij.context().inject(searcher)
        return searcher

    def _generate_search_service(self):
        return ij.get("org.scijava.search.SearchService")

    def _search(self, text):
        # TODO: Consider adding a button to toggle fuzziness
        breakpoint()
        self.results = self.searcher.search(text, True)
        for i in range(len(self.results)):
            name = ij.py.from_java(self.results[i].name())
            self.tableWidget.setItem(i, 0, QTableWidgetItem(name))
        for i in range(len(self.results), self.maxResults):
            self.tableWidget.setItem(i, 0, QTableWidgetItem(""))

    def _highlight_module(self, row: int, col: int):
        # Print highlighted module
        name = ij.py.from_java(self.results[row].name())
        self.focused_module.setText(name)

        # Create buttons for each action
        self.focused_actions = self.searchService.actions(self.results[row])
        activated_actions = len(self.focused_action_buttons)
        # Hide buttons if we have more than needed
        while activated_actions > len(self.focused_actions):
            activated_actions = activated_actions - 1
            self.focused_action_buttons[activated_actions].hide()
        # Create buttons if we need more than we have
        while len(self.focused_action_buttons) < len(self.focused_actions):
            button = QPushButton()
            self.focused_action_buttons.append(button)
            self.focus_widget.layout().addWidget(button)
        # Rename buttons to reflect focused module's actions
        for i in range(len(self.focused_actions)):
            action_name = ij.py.from_java(self.focused_actions[i].toString())
            self.focused_action_buttons[i].show()
            self.focused_action_buttons[i].setText(action_name)
            self.focused_action_buttons[i].disconnect()
            if action_name == "Run":
                self.focused_action_buttons[i].clicked.connect(
                    lambda: self._execute_module(self.results[row].info())
                )
            else:
                self.focused_action_buttons[i].clicked.connect(
                    self.focused_actions[i].run
                )
            self.focused_action_buttons[i].show()

    def _execute_module(self, moduleInfo):
        logging.debug("Creating module...")
        module = ij.module().createModule(moduleInfo)

        # preprocess using napari GUI
        logging.debug("Processing...")
        func = _functionify_module_execution(module, moduleInfo, self.viewer)
        self.viewer.window.add_function_widget(
            func, name=ij.py.from_java(moduleInfo.getTitle())
        )
