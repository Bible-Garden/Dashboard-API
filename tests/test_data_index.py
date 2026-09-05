"""
`GET /api/data/index` — the RAG index export (ClickUp 86cbegwqg).

Until now `translation_chunks`, `chunk_embeddings` and `psalm_verse_mappings`
reached production as a hand-made MySQL dump over an SSH tunnel: a step outside
the import, done by a human, at a time unrelated to the text it indexes. This
endpoint makes the index part of the resync, read straight from the local
`cep_public` where Bible-API's CLIs write it.

What is worth testing, and is tested here:

- the **vector survives the trip byte for byte** — it is shipped as base64 of
  the stored BLOB, not as a list of floats, and `dims * 4` is the contract the
  reader checks;
- **pagination is exact and non-overlapping** — a page boundary must not
  duplicate or drop an embedding, and the corpus tables must be unmistakably
  absent from later pages rather than look empty;
- **refusals are named before any selection** — a version nobody wrote is a
  409 listing the ones that exist, not an empty page that reads like an
  unindexed translation;
- **a translation with no chunks is a 200 with zeros** — `bti`, `npu`,
  `webbe`, `webus` are published but not indexed, and that is normal data.

Mocked `create_connection`, no database. Run it the way `test_data_manifest.py`
is run (the suite's `conftest.py` wants `API_KEY`, an admin login and a test
DB, and is also what puts `app/` on `sys.path`):

    docker exec dashboard-api sh -c \
      'cd /code && PYTHONPATH=app pytest tests/test_data_index.py -q --noconftest'
"""

import base64
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException


V1024 = 'c3:BAAI/bge-m3@1024'
V768 = 'c3:gemini-embedding-001@768'
VERSIONS = [{'v': V1024}, {'v': V768}]
CHUNKING_VERSIONS = [{'v': 3}]
MAPPING_VERSIONS = [{'v': 1}]

TRANSLATION_SYN = [{'code': 1, 'alias': 'syn'}]

# Two vectors of 4 dims each — the shape of the real ones, not their size.
VECTORS = [bytes(range(16)), bytes(range(16, 32))]


def _chunk(canonical_id):
    return {'code': 7, 'canonical_id': canonical_id, 'chunking_version': 3,
            'translation': 1, 'book_number': 1, 'chapter_number': 1,
            'verse_number_start': 1, 'verse_number_end': 8, 'verse_count': 8,
            'title': None, 'text': 'In the beginning', 'char_count': 16}


def _mapping():
    return {'code': 3, 'mapping_version': 1, 'translation': 1, 'book_number': 19,
            'chapter_number': 3, 'verse_number': 1, 'canonical_chapter': 3,
            'canonical_verse_start': 1, 'canonical_verse_end': 1}


def _embedding(canonical_id, vector, dims=4, version=V1024):
    return {'code': 11, 'canonical_id': canonical_id, 'translation': 1,
            'embedding_version': version, 'dims': dims, 'vector': vector}


def _index(answers, **kwargs):
    """Call the endpoint over a cursor that answers `answers` in order."""
    cursor = Mock()
    cursor.fetchall.side_effect = list(answers)
    connection = Mock()
    connection.cursor.return_value = cursor
    with patch('data.create_connection', return_value=connection):
        from data import get_data_index
        params = {'translation': 'syn', 'embedding_version': V1024,
                  'chunking_version': None, 'mapping_version': None,
                  'limit': 2000, 'offset': 0, 'api_key': True}
        params.update(kwargs)
        return get_data_index(**params), cursor


def _statements(cursor) -> list[str]:
    """The SQL actually sent, in order (not its repr — newlines matter)."""
    return [call.args[0] for call in cursor.execute.call_args_list]


def _first_page_answers(chunks, mappings, embeddings, total):
    """The reads a first page makes, in order."""
    return [
        [{'ok': 1}],            # public-db probe
        TRANSLATION_SYN,        # alias -> code
        VERSIONS,               # embedding_version validation
        [{'t': 1, 'v': V1024, 'n': total}],  # total for this version
        chunks,
        mappings,
        embeddings,
    ]


def test_the_vector_is_base64_of_the_stored_blob_not_a_list_of_floats():
    """`dims * 4` bytes back, byte for byte — that is the whole contract.

    A JSON array of floats would cost ~6x the bytes and could not promise
    identity: the reader mmaps these bytes into a numpy matrix.
    """
    body, _ = _index(_first_page_answers(
        [_chunk('v3:01.001.001-008')], [_mapping()],
        [_embedding('v3:01.001.001-008', VECTORS[0])], total=1,
    ))

    row = body['chunk_embeddings'][0]
    assert isinstance(row['vector'], str)
    assert base64.b64decode(row['vector']) == VECTORS[0]
    assert len(base64.b64decode(row['vector'])) == row['dims'] * 4


def test_the_first_page_carries_the_corpus_and_says_where_to_continue():
    body, _ = _index(_first_page_answers(
        [_chunk('a'), _chunk('b')], [_mapping()],
        [_embedding('a', VECTORS[0]), _embedding('b', VECTORS[1])], total=5,
    ), limit=2)

    assert len(body['translation_chunks']) == 2
    assert len(body['psalm_verse_mappings']) == 1
    assert len(body['chunk_embeddings']) == 2
    assert body['chunk_embeddings_total'] == 5
    assert body['next_offset'] == 2
    assert body['translation'] == 'syn'
    assert body['translation_code'] == 1


def test_later_pages_omit_the_corpus_as_null_never_as_an_empty_list():
    """`[]` would read as "this translation has no chunks", which is a real
    and different state (`bti`). `null` says "not on this page"."""
    body, cursor = _index([
        [{'ok': 1}],
        TRANSLATION_SYN,
        VERSIONS,
        [{'t': 1, 'v': V1024, 'n': 5}],
        [_embedding('c', VECTORS[1])],
    ], limit=2, offset=2)

    assert body['translation_chunks'] is None
    assert body['psalm_verse_mappings'] is None
    assert len(body['chunk_embeddings']) == 1
    assert body['next_offset'] == 4
    # Nothing was even asked of the corpus tables on a later page — the only
    # mention of them is the probe that names the database.
    statements = _statements(cursor)
    assert not any('SELECT * FROM cep_public.psalm_verse_mappings' in s
                   for s in statements)
    assert not any('SELECT * FROM cep_public.translation_chunks' in s
                   for s in statements)


def test_the_last_page_ends_the_walk_instead_of_offering_an_empty_one():
    """`next_offset` comes from the counted total, so `offset + limit` past
    the end is `null` — the importer never fetches a page of nothing."""
    body, _ = _index([
        [{'ok': 1}], TRANSLATION_SYN, VERSIONS,
        [{'t': 1, 'v': V1024, 'n': 3}],
        [_embedding('c', VECTORS[1])],
    ], limit=2, offset=2)

    assert body['next_offset'] is None


def test_the_first_page_is_capped_because_it_carries_the_corpus_as_well():
    """Measured 2026-09-05: `limit=2000` at `offset=0` weighed 18.5 MiB — the
    corpus (7.8 MiB for `syn`) plus 2000 embeddings at ~5.6 KB each, against a
    12 MiB budget set by a production VM that parses the body into Python.

    So the first page is clipped, and says so: `limit` is what was asked,
    `limit_applied` is what the page may carry, and `next_offset` follows the
    applied value — a walk driven by `next_offset` cannot skip the rows the
    clipping withheld.
    """
    from data import FIRST_PAGE_EMBEDDINGS

    body, cursor = _index(_first_page_answers(
        [_chunk('a')], [_mapping()], [_embedding('a', VECTORS[0])], total=3963,
    ), limit=2000, offset=0)

    assert body['limit'] == 2000
    assert body['limit_applied'] == FIRST_PAGE_EMBEDDINGS
    # The window sent to MySQL is the applied one, not the asked one.
    assert cursor.execute.call_args_list[-1].args[1][2] == FIRST_PAGE_EMBEDDINGS
    assert body['next_offset'] == FIRST_PAGE_EMBEDDINGS


def test_later_pages_have_their_own_cap_and_no_corpus_to_carry():
    from data import PAGE_EMBEDDINGS_MAX

    body, cursor = _index([
        [{'ok': 1}], TRANSLATION_SYN, VERSIONS,
        [{'t': 1, 'v': V1024, 'n': 3963}],
        [_embedding('c', VECTORS[1])],
    ], limit=10000, offset=600)

    assert body['limit_applied'] == PAGE_EMBEDDINGS_MAX
    assert cursor.execute.call_args_list[-1].args[1][2] == PAGE_EMBEDDINGS_MAX
    assert body['next_offset'] == 600 + PAGE_EMBEDDINGS_MAX


def test_a_limit_below_the_cap_is_obeyed_exactly_as_asked():
    """The cap is a ceiling, not a page size: a caller on a small machine can
    still ask for less."""
    body, cursor = _index(_first_page_answers(
        [], [], [_embedding('a', VECTORS[0])], total=3963,
    ), limit=5, offset=0)

    assert body['limit_applied'] == 5
    assert cursor.execute.call_args_list[-1].args[1][2] == 5
    assert body['next_offset'] == 5


def test_the_page_caps_stay_inside_the_twelve_mebibyte_budget():
    """The arithmetic behind the two constants, so that raising one without
    re-measuring fails here instead of on the production VM."""
    from data import FIRST_PAGE_EMBEDDINGS, PAGE_EMBEDDINGS_MAX

    EMBEDDING_ROW_BYTES = 5631   # measured on the wire, 1024 dims, 2026-09-05
    LARGEST_CORPUS_BYTES = 8_118_623   # syn: every chunk + every Psalm mapping
    BUDGET = 12 * 1024 * 1024

    assert LARGEST_CORPUS_BYTES + FIRST_PAGE_EMBEDDINGS * EMBEDDING_ROW_BYTES \
        <= BUDGET
    assert PAGE_EMBEDDINGS_MAX * EMBEDDING_ROW_BYTES <= BUDGET


def test_the_index_database_name_is_an_identifier_before_it_reaches_sql():
    """A schema name cannot be a query parameter, so it is interpolated — and
    therefore checked at startup, where the value comes from."""
    from config import valid_identifier

    assert valid_identifier('cep_public')
    assert not valid_identifier('cep_public; DROP TABLE translation_chunks')
    assert not valid_identifier('cep`public')
    assert not valid_identifier('')


def test_pages_are_ordered_and_windowed_by_a_natural_key():
    """AUTO_INCREMENT `code` is renumbered by a rebuild; `canonical_id` is
    not. A page boundary must mean the same thing on two calls."""
    _, cursor = _index(_first_page_answers([], [], [], total=0))

    embedding_sql = _statements(cursor)[-1]
    assert 'ORDER BY canonical_id' in embedding_sql
    assert 'LIMIT %s OFFSET %s' in embedding_sql


def test_unknown_embedding_version_is_409_listing_the_ones_that_exist():
    """An empty page would be indistinguishable from an unindexed
    translation, so a version nobody wrote is refused by name."""
    with pytest.raises(HTTPException) as exc:
        _index([[{'ok': 1}], TRANSLATION_SYN, VERSIONS],
               embedding_version='c3:nope@42')

    assert exc.value.status_code == 409
    assert exc.value.detail['parameter'] == 'embedding_version'
    assert exc.value.detail['available_versions'] == [V1024, V768]


def test_unknown_chunking_version_is_409_before_anything_is_selected():
    with pytest.raises(HTTPException) as exc:
        _index([[{'ok': 1}], TRANSLATION_SYN, VERSIONS, CHUNKING_VERSIONS],
               chunking_version=99)

    assert exc.value.status_code == 409
    assert exc.value.detail['parameter'] == 'chunking_version'
    assert exc.value.detail['available_versions'] == [3]


def test_unknown_mapping_version_is_409_too():
    with pytest.raises(HTTPException) as exc:
        _index([[{'ok': 1}], TRANSLATION_SYN, VERSIONS, CHUNKING_VERSIONS,
                MAPPING_VERSIONS], chunking_version=3, mapping_version=9)

    assert exc.value.status_code == 409
    assert exc.value.detail['parameter'] == 'mapping_version'
    assert exc.value.detail['available_versions'] == [1]


def test_unknown_translation_alias_is_404_like_the_data_export():
    with pytest.raises(HTTPException) as exc:
        _index([[{'ok': 1}], []], translation='zzz')

    assert exc.value.status_code == 404
    assert 'zzz' in exc.value.detail


def test_an_unindexed_translation_is_200_with_zero_chunks_not_an_error():
    """`bti`, `npu`, `webbe` and `webus` are published and not indexed.

    They still have Psalm mappings, which is exactly why the answer must be
    an honest 200: an error here would read as a broken export.
    """
    body, _ = _index([
        [{'ok': 1}],
        [{'code': 11, 'alias': 'bti'}],
        VERSIONS,
        [],                      # no embeddings of any version
        [],                      # no chunks
        [_mapping()],            # but the Psalm map is there
        [],
    ], translation='bti')

    assert body['translation_chunks'] == []
    assert body['chunk_embeddings'] == []
    assert body['chunk_embeddings_total'] == 0
    assert body['next_offset'] is None
    assert len(body['psalm_verse_mappings']) == 1


def test_an_unreadable_index_database_names_the_database_and_the_variable():
    """Not a bare traceback: the operator has to know which schema is
    missing AND which variable configures it."""
    cursor = Mock()
    cursor.execute.side_effect = Exception("1049 (42000): Unknown database 'cep_nope'")
    connection = Mock()
    connection.cursor.return_value = cursor
    with patch('data.create_connection', return_value=connection):
        from data import get_data_index
        with pytest.raises(HTTPException) as exc:
            get_data_index(translation='syn', embedding_version=V1024,
                           chunking_version=None, mapping_version=None,
                           limit=2000, offset=0, api_key=True)

    assert exc.value.status_code == 500
    assert 'PUBLIC_DB_NAME' in exc.value.detail
    assert 'cep_public' in exc.value.detail


def test_the_index_is_read_across_databases_and_never_copied_into_cep_admin():
    """The index is written only by Bible-API's CLIs into `cep_public`.

    A copy in `cep_admin` would be a second source of truth for the same
    rows, and the one that nothing rebuilds.
    """
    _, cursor = _index(_first_page_answers([], [], [], total=0))

    joined = ' '.join(_statements(cursor))
    assert 'cep_public.translation_chunks' in joined
    assert 'cep_public.chunk_embeddings' in joined
    assert 'cep_public.psalm_verse_mappings' in joined
    for verb in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'REPLACE'):
        assert verb not in joined.upper()


def test_the_page_total_is_the_manifest_count_statement_narrowed():
    """The number that decides the paging is the number the importer
    verifies against — the same statement, not a second one that could
    drift away from it."""
    from data import INDEX_COUNT_SQL

    _, cursor = _index(_first_page_answers([], [], [], total=0))

    count_sql = _statements(cursor)[3]
    expected = INDEX_COUNT_SQL['chunk_embeddings'].format(db='cep_public', ph='%s')
    assert count_sql.startswith(expected)
    assert count_sql.endswith('HAVING v = %s')
