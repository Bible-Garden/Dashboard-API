"""
The book alias of `GET /api/excerpt_with_alignment` (ClickUp 86cbehfqx).

Maria's decision of 2026-09-05: the client copies the alias out of
`GET /api/translations/{code}/books`, so the contract is that catalogue alias,
in Latin letters, in any case — and nothing else. A book name in another
script is a `422` naming the expected format; a Latin token that is no alias
of this translation is a `404`. `Gen 1:1` used to match the substring `en` and
answer "Book with alias 'en' not found".

Mirrors Bible-API's `tests/test_translation_books.py`, which covers the same
fix in the same words against its own copy of this endpoint.

No database and no admin password: what is under test is the grammar, the case
folding and the two status codes, so the endpoint's data access is stubbed.
That is also why this file runs **without** the suite's `conftest.py` (which
requires a database, `API_KEY` and `TEST_ADMIN_PASSWORD`), the same way
`test_data_manifest.py` and `test_data_index.py` do:

    docker run --rm --env-file .env -e AUDIO_DIR=/tmp -v "$PWD":/code -w /code \
      dashboard-api-dashboard-api \
      sh -c 'PYTHONPATH=app pytest tests/test_excerpt_alias.py -q --noconftest'
"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import excerpt
from auth import verify_api_key
from main import app

client = TestClient(app)

GENESIS = {
    'code': 1,
    'number': 1,
    'alias': 'gen',
    'name': 'Бытие',
    'chapters_count': 50,
}
EMPTY_CHAPTER = {'verses': [], 'titles': [], 'notes': [], 'audio_link': ''}


@pytest.fixture(autouse=True)
def public_endpoint():
    """The API key is not what this file tests."""
    app.dependency_overrides[verify_api_key] = lambda: True
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def catalogue(monkeypatch):
    """A one-book catalogue: `gen` resolves, nothing else does.

    Yields the list of aliases the lookup was actually asked for, which is
    where the case folding is visible.
    """
    looked_up = []

    def get_books_info(cursor, translation, alias=None):
        looked_up.append(alias)
        return [dict(GENESIS)] if alias == 'gen' else []

    monkeypatch.setattr(excerpt, 'create_connection', lambda: Mock())
    monkeypatch.setattr(excerpt, 'get_translation_name', lambda cursor, translation: 'SYNO')
    monkeypatch.setattr(excerpt, 'get_books_info', get_books_info)
    monkeypatch.setattr(excerpt, 'get_chapter_data', lambda *args, **kwargs: dict(EMPTY_CHAPTER))
    monkeypatch.setattr(excerpt, 'get_prev_excerpt', lambda *args, **kwargs: '')
    monkeypatch.setattr(excerpt, 'get_next_excerpt', lambda *args, **kwargs: 'gen 2')
    return looked_up


def _excerpt(reference: str, translation: int = 1):
    return client.get(
        '/api/excerpt_with_alignment',
        params={'translation': translation, 'excerpt': reference},
    )


# --------------------------------------------------------------------------
# The grammar itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize('reference, book', [
    ('gen 1', 'gen'),
    ('Gen 1:1', 'Gen'),
    ('GEN 1:1-3', 'GEN'),
    ('1jn 2:3', '1jn'),
    ('psa 150', 'psa'),
])
def test_the_grammar_takes_the_whole_book_token(reference, book):
    """`[0-9a-z]+` without a boundary matched `en` inside `Gen`."""
    match = excerpt.EXCERPT_PATTERN.search(reference)

    assert match is not None
    assert match.group('book') == book


@pytest.mark.parametrize('reference', ['Бут 1:1', 'Быт 1:1', 'Мф 1:1', 'gen', '1:1', ''])
def test_the_grammar_rejects_what_is_not_a_latin_reference(reference):
    assert excerpt.EXCERPT_PATTERN.search(reference) is None


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


@pytest.mark.parametrize('reference', ['gen 1:1', 'Gen 1:1', 'GEN 1:1', 'gEn 1:1'])
def test_the_alias_is_case_insensitive(catalogue, reference):
    response = _excerpt(reference)

    assert response.status_code == 200, response.text
    part = response.json()['parts'][0]
    assert part['book']['alias'] == 'gen'
    assert part['chapter_number'] == 1
    assert catalogue == ['gen']  # folded before the lookup, not after it


def test_every_case_of_one_alias_returns_the_same_book(catalogue):
    lower, upper, title = (_excerpt(r).json() for r in ('gen 1:1', 'GEN 1:1', 'Gen 1:1'))

    assert lower == upper == title


def test_an_unknown_latin_alias_is_a_404_that_names_it(catalogue):
    """The answer must name `genesis`, not the `en` the old grammar found
    inside it."""
    response = _excerpt('Genesis 1:1')

    assert response.status_code == 404, response.text
    detail = response.json()['detail']
    assert 'genesis' in detail
    assert "'en'" not in detail


def test_a_book_name_in_another_script_is_a_422_that_names_the_format(catalogue):
    response = _excerpt('Бут 1:1', translation=20)

    assert response.status_code == 422, response.text
    detail = response.json()['detail']
    assert 'Invalid excerpt format' in detail
    assert 'GET /api/translations/{code}/books' in detail
    assert 'case does not matter' in detail
    assert catalogue == []  # refused before any query


@pytest.mark.parametrize('reference', ['Mt 1:1', 'mt 1:1', 'jn 1:1', 'ex 1:1'])
def test_a_short_name_is_not_an_alias(catalogue, reference):
    """`short_name_en` / `short_name_ru` left the `WHERE` clause on 2026-09-05:
    they are display names the books catalogue never publishes."""
    assert _excerpt(reference).status_code == 404, reference


def test_several_references_in_one_value_still_parse(catalogue):
    response = _excerpt('gen 1:1 gen 2:1')

    assert response.status_code == 200, response.text
    assert [part['chapter_number'] for part in response.json()['parts']] == [1, 2]


# --------------------------------------------------------------------------
# The lookup query
# --------------------------------------------------------------------------


def test_the_lookup_matches_the_catalogue_codes_only():
    """`short_name_en` matched 14 books under a second, undocumented name
    (`mt`, `jn`, `ex`, `1kings`, …); `short_name_ru` was unreachable through
    the Latin-only grammar. Both are gone from the clause."""
    cursor = Mock()
    cursor.fetchall.return_value = []

    excerpt.get_books_info(cursor, 1, 'gen')

    sql, params = cursor.execute.call_args[0]
    assert params['alias'] == 'gen'
    for column in ('code1', 'code2', 'code3', 'code4', 'code5'):
        assert f'bb.{column} = %(alias)s' in sql
    assert 'short_name' not in sql


def test_the_openapi_description_states_the_alias_contract():
    operation = app.openapi()['paths']['/api/excerpt_with_alignment']['get']

    assert '/api/translations/{code}/books' in operation['description']
    assert 'case-insensitive' in operation['description']
    assert '404' in operation['responses']
