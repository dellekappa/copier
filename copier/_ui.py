# from copy import deepcopy
import json
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from prompt_toolkit.lexers import PygmentsLexer
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass
from pygments.lexers.data import JsonLexer, YamlLexer
from questionary import unsafe_prompt
from questionary.prompts.common import Choice as QChoice

from ._tools import force_str_end
from ._types import MISSING, AnyByStrDict
from .errors import InteractiveSessionError


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class Choice:
    """One choice between many.

    Args:
        name: Text shown in the selection list.

        value: Value returned, when the choice is selected.

        disabled: If set, the choice can not be selected by the user. The
            provided text is used to explain, why the selection is
            disabled.
    """

    name: str
    value: Any
    disabled: str | None


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class Question:
    name: str
    message: str
    qmark: str | None
    secret: bool
    default: Any
    type: str
    choices: Sequence[Choice]
    multiselect: bool
    multiline: bool
    placeholder: str | None
    silent: bool
    transform_answer: Callable[[Any], Any]
    validate_answer: Callable[[str], str | Literal[True]]


@runtime_checkable
class QuestionnaireUI(Protocol):
    """Abstraction for how questions are presented and answers collected.

    This protocol decouples the questionnaire business logic (hierarchy,
    conditions, validation) from the I/O mechanism. Implementations include:

    - InteractiveUI: terminal-based prompts via questionary (current behavior)
    - ProgrammaticUI: no I/O, for use by MCP servers and other programmatic clients
    """

    def ask_question(self, question: Question, level: int = 0) -> Any:
        """Present a question and return the answer.

        The implementation is responsible for rendering the question
        (message, choices, default, type) and collecting a raw answer.
        Type casting and validation are handled by the caller.

        Args:
            question: The Question object with all metadata (type, choices,
                default, help, validator, etc.).
            level: Nesting depth of the question in the hierarchy (0 = top-level).

        Returns:
            The raw answer value.
        """
        ...

    def show_group_message(self, message: str, level: int = 0) -> None:
        """Display a group/section header message.

        Called when entering a DICT node to show the group's help text.

        Args:
            message: The rendered help text for this group.
            level: Nesting depth of the group in the hierarchy (0 = top-level).
        """
        ...


class InteractiveUI:
    """Terminal-based questionnaire UI using questionary.

    This is the default implementation of QuestionnaireUI, preserving
    the existing interactive behavior of Copier.
    """

    @staticmethod
    def _level_to_padding(level: int) -> str:
        return " " * (level * 2) + " " if level else ""

    @staticmethod
    def _render_default_value(question: Question) -> Any:
        """Get default answer rendered for the questionary lib.

        The questionary lib expects some specific data types, and returns
        it when the user answers. Sometimes you need to compare the response
        to the rendered one, or vice-versa.

        This helper allows such usages.
        """
        default = question.default
        if default is MISSING:
            return MISSING
        if question.choices:
            # if question.multiselect and not isinstance(default, list):
            #     default = [default]
            return default

        # Yes/No questions expect and return bools
        if question.type == "bool" and isinstance(default, bool):
            return default
        # Emptiness is expressed as an empty str
        if default is None:
            return ""
        # JSON and YAML dumped depending on multiline setting
        if question.type == "json":
            return json.dumps(default, indent=2 if question.multiline else None)
        if question.type == "yaml":
            return yaml.safe_dump(
                default, default_flow_style=not question.multiline, width=2147483647
            ).strip()
        # All other data has to be str
        return str(default)

    @staticmethod
    def _to_questionary_structure(question: Question, level: int) -> AnyByStrDict:  # noqa: C901
        """Get the question in a format that the questionary lib understands."""
        padding = InteractiveUI._level_to_padding(level)

        msg = f"{force_str_end(question.message)}  {padding}"
        lexer = None
        qmark = question.qmark or ("🕵️" if question.secret else "🎤")
        result: AnyByStrDict = {
            "filter": question.transform_answer,
            "message": msg,
            "mouse_support": True,
            "name": question.name,
            "qmark": f"{padding}{qmark}",
            "when": lambda _: not question.silent,
        }
        default = InteractiveUI._render_default_value(question)
        if default is not MISSING:
            result["default"] = default
        questionary_type = "input"
        type_name = question.type
        if type_name == "bool":
            questionary_type = "confirm"
            # For backwards compatibility
            if default is MISSING:
                result["default"] = False
        if question.choices:
            questionary_type = "checkbox" if question.multiselect else "select"

            q_choices: list[QChoice] = []
            for choice in question.choices:
                checked = False
                if (
                    questionary_type == "checkbox"
                    and default is not MISSING
                    and len(default) > 0
                ):
                    checked = question.transform_answer(choice.value) in default
                q_choices.append(
                    QChoice(
                        title=choice.name,
                        value=choice.value,
                        disabled=choice.disabled,
                        checked=checked,
                    )
                )

            if questionary_type == "checkbox":
                # Checkbox questions defaults are handled by the "checked"
                # property in questionary.
                # We must remove the default value to avoid errors.
                result["default"] = None
            result["choices"] = q_choices
        if questionary_type == "input":
            if question.secret:
                questionary_type = "password"
            elif type_name == "yaml":
                lexer = PygmentsLexer(YamlLexer)
            elif type_name == "json":
                lexer = PygmentsLexer(JsonLexer)
            if lexer:
                result["lexer"] = lexer
            result["multiline"] = question.multiline
            if question.placeholder:
                result["placeholder"] = question.placeholder
        if type_name == "path":
            questionary_type = "path"
        if questionary_type in {"input", "checkbox", "password", "path"}:
            result["validate"] = question.validate_answer
        result.update({"type": questionary_type})
        return result

    @staticmethod
    def ask_question(question: Question, level: int = 0) -> Any:
        """Prompt the user interactively via questionary and return the answer."""
        def_value = question.default
        try:
            new_answer = unsafe_prompt(
                [InteractiveUI._to_questionary_structure(question, level)],
                answers={
                    question.name: def_value if def_value is not MISSING else None
                },
            )[question.name]
        except EOFError as err:
            raise InteractiveSessionError(
                "Use `--defaults` and/or `--data`/`--data-file`"
            ) from err
        return new_answer

    @staticmethod
    def show_group_message(message: str, level: int = 0) -> None:
        """Print a group header message to the terminal."""
        padding = InteractiveUI._level_to_padding(level)
        unsafe_prompt(
            [
                {
                    "type": "print",
                    "message": f"{padding} ▷ {message}",
                    "when": lambda _: True,
                }
            ],
            style="bold",
        )
