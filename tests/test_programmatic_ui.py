"""Tests for ProgrammaticUI and the QuestionPending replay mechanism."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from plumbum import local

from copier._main import Worker
from copier._user_data import (
    GlobalState,
    ProgrammaticUI,
    QuestionnaireUI,
    QuestionNode,
)
from copier.errors import QuestionPending

from .helpers import build_file_tree, git_save


@pytest.fixture()
def basic_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A minimal template with 3 simple questions."""
    src = tmp_path_factory.mktemp("src")
    with local.cwd(src):
        build_file_tree(
            {
                "copier.yml": """\
                    project_name:
                        type: str
                        default: my-project
                        help: What is your project name?
                    version:
                        type: str
                        default: "1.0.0"
                    description:
                        type: str
                        default: A great project
                """,
                "{{ _copier_conf.answers_file }}.jinja": (
                    "{{ _copier_answers|to_nice_yaml }}"
                ),
            }
        )
        git_save(tag="v1")
    return src


@pytest.fixture()
def conditional_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A template with conditional questions (when clause)."""
    src = tmp_path_factory.mktemp("src")
    with local.cwd(src):
        build_file_tree(
            {
                "copier.yml": """\
                    use_database:
                        type: bool
                        default: false
                        help: Do you need a database?
                    db_host:
                        type: str
                        default: localhost
                        help: Database host
                        when: "{{ use_database }}"
                    db_port:
                        type: int
                        default: 5432
                        help: Database port
                        when: "{{ use_database }}"
                """,
                "{{ _copier_conf.answers_file }}.jinja": (
                    "{{ _copier_answers|to_nice_yaml }}"
                ),
            }
        )
        git_save(tag="v1")
    return src


@pytest.fixture()
def dict_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A template with dict (grouped) questions."""
    src = tmp_path_factory.mktemp("src")
    with local.cwd(src):
        build_file_tree(
            {
                "copier.yml": """\
                    project_name:
                        type: str
                        default: my-project
                    database:
                        type: dict
                        help: Database configuration
                        items:
                            host:
                                type: str
                                default: localhost
                            port:
                                type: int
                                default: 5432
                """,
                "{{ _copier_conf.answers_file }}.jinja": (
                    "{{ _copier_answers|to_nice_yaml }}"
                ),
            }
        )
        git_save(tag="v1")
    return src


def _make_state(worker: Worker, ui: ProgrammaticUI | None = None) -> GlobalState:
    """Create a GlobalState from a Worker, mimicking Worker._ask() setup."""
    from copier._user_data import AnswersMap

    worker.answers = AnswersMap(
        user_defaults=worker.user_defaults,
        init=worker.data,
        last=worker.subproject.last_answers,
        metadata=worker.template.metadata,
    )
    return GlobalState(
        _context_renderer=worker._render_context,
        template=worker.template,
        answers=worker.answers,
        jinja_env=worker.jinja_env,
        ui=ui or ProgrammaticUI(),
        settings=worker.settings,
        defaults=worker.defaults,
        skip_answered=worker.skip_answered,
    )


def _collect_questions_via_replay(
    state: GlobalState,
    answers: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Drive the tree walk via QuestionPending replay, returning all questions asked.

    For each QuestionPending, uses the provided answers dict or falls back
    to the question's default value.
    """
    if answers is None:
        answers = {}

    questions_asked: list[dict[str, object]] = []

    while True:
        try:
            for qname, details in state.template.questions_data.items():
                node = QuestionNode(qname, details, state)
                node.process()
            break  # All questions answered
        except QuestionPending as qp:
            questions_asked.append(qp.question_info)
            var_name = qp.question_info["var_name"]
            if var_name in answers:
                state.answers.init[var_name] = answers[var_name]
            else:
                # Use default
                state.answers.init[var_name] = qp.question_info["default"]

    return questions_asked


# -- Protocol conformance --


def test_programmatic_ui_implements_protocol() -> None:
    """ProgrammaticUI must satisfy the QuestionnaireUI protocol."""
    ui = ProgrammaticUI()
    assert isinstance(ui, QuestionnaireUI)


# -- Basic replay mechanism --


def test_question_pending_raised_on_first_unanswered(
    basic_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The first unanswered question raises QuestionPending."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(src_path=str(basic_template), dst_path=dst, defaults=False) as worker:
        state = _make_state(worker)

        with pytest.raises(QuestionPending) as exc_info:
            for qname, details in state.template.questions_data.items():
                node = QuestionNode(qname, details, state)
                node.process()

        qp = exc_info.value.question_info
        assert qp["var_name"] == "project_name"
        assert qp["type"] == "str"
        assert qp["default"] == "my-project"
        assert qp["help"] == "What is your project name?"


def test_replay_collects_all_questions(
    basic_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Replay loop should collect all 3 questions from the basic template."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(src_path=str(basic_template), dst_path=dst, defaults=False) as worker:
        state = _make_state(worker)
        questions = _collect_questions_via_replay(state)

        assert len(questions) == 3
        assert [q["var_name"] for q in questions] == [
            "project_name",
            "version",
            "description",
        ]


def test_replay_with_custom_answers(
    basic_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Custom answers are stored and don't trigger QuestionPending again."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(src_path=str(basic_template), dst_path=dst, defaults=False) as worker:
        state = _make_state(worker)
        custom: dict[str, object] = {"project_name": "custom-name", "version": "2.0.0"}
        questions = _collect_questions_via_replay(state, custom)

        assert len(questions) == 3
        assert state.answers.init["project_name"] == "custom-name"
        assert state.answers.init["version"] == "2.0.0"


# -- Conditional questions --


def test_conditional_skipped_when_false(
    conditional_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Questions with when=false should not raise QuestionPending."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(
        src_path=str(conditional_template), dst_path=dst, defaults=False
    ) as worker:
        state = _make_state(worker)
        # use_database = false → db_host and db_port should be skipped
        questions = _collect_questions_via_replay(state, {"use_database": False})

        asked_names = [q["var_name"] for q in questions]
        assert "use_database" in asked_names
        assert "db_host" not in asked_names
        assert "db_port" not in asked_names


def test_conditional_asked_when_true(
    conditional_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Questions with when=true should raise QuestionPending."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(
        src_path=str(conditional_template), dst_path=dst, defaults=False
    ) as worker:
        state = _make_state(worker)
        # use_database = true → db_host and db_port should be asked
        questions = _collect_questions_via_replay(state, {"use_database": True})

        asked_names = [q["var_name"] for q in questions]
        assert asked_names == ["use_database", "db_host", "db_port"]


# -- Dict (grouped) questions --


def test_dict_questions_have_level(
    dict_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Questions inside a dict group should report level > 0."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(src_path=str(dict_template), dst_path=dst, defaults=False) as worker:
        state = _make_state(worker)
        questions = _collect_questions_via_replay(state)

        # project_name is top-level
        assert questions[0]["var_name"] == "project_name"
        assert questions[0]["level"] == 0

        # host and port are inside "database" dict → level 1
        nested = [q for q in questions if cast(int, q["level"]) > 0]
        assert len(nested) == 2
        nested_names = [q["var_name"] for q in nested]
        assert "database.host" in nested_names
        assert "database.port" in nested_names


# -- QuestionPending metadata --


def test_question_pending_metadata_complete(
    basic_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """QuestionPending should carry all required metadata fields."""
    dst = tmp_path_factory.mktemp("dst")
    with Worker(src_path=str(basic_template), dst_path=dst, defaults=False) as worker:
        state = _make_state(worker)

        with pytest.raises(QuestionPending) as exc_info:
            for qname, details in state.template.questions_data.items():
                node = QuestionNode(qname, details, state)
                node.process()

        info = exc_info.value.question_info
        expected_keys = {
            "var_name",
            "type",
            "help",
            "default",
            "choices",
            "multiselect",
            "secret",
            "level",
        }
        assert set(info.keys()) == expected_keys
