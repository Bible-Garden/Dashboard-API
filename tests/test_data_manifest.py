"""
`GET /api/data/manifest` — the resync plan Bible-API walks (ClickUp 86cbbq5zp).

The manifest exists because Bible-API's full import must never download the
whole export again: on 2026-08-30 that 147 MB document OOM-killed the
production VM, and it was materialised in *this* process too, on the same VM.
The importer now asks for this plan first and then fetches one translation at
a time.

Two things are worth testing and are tested here: the manifest is *small and
complete* (the reference tables plus a work list, not the corpus), and its
counts are aggregated per translation as well as in total — those totals are
the expected side of the importer's post-import verification, so an
off-by-one here would be reported as data loss there.
"""

from unittest.mock import Mock, patch


V1024 = 'c3:BAAI/bge-m3@1024'
V768 = 'c3:gemini-embedding-001@768'

LANGUAGES = [
    {'alias': 'ru', 'name_en': 'Russian', 'name_national': 'Русский'},
    {'alias': 'en', 'name_en': 'English', 'name_national': 'English'},
]
BIBLE_BOOKS = [{'number': 1, 'alias': 'gen'}, {'number': 40, 'alias': 'mat'}]
TRANSLATIONS = [{'code': 1, 'alias': 'syn'}, {'code': 11, 'alias': 'bti'}]

# What each per-table GROUP BY query answers, in the order data.py runs them.
COUNT_ANSWERS = [
    [{'t': 1, 'n': 66}, {'t': 11, 'n': 60}],       # translation_books
    [{'t': 1, 'n': 31361}, {'t': 11, 'n': 31111}],  # translation_verses
    [{'t': 1, 'n': 3200}, {'t': 11, 'n': 2449}],    # translation_titles
    [{'t': 1, 'n': 15707}, {'t': 11, 'n': 11361}],  # translation_notes
    [{'t': 1, 'n': 2}, {'t': 11, 'n': 1}],          # voices
    [{'t': 1, 'n': 59437}, {'t': 11, 'n': 31111}],  # voice_alignments
]


# The `index` block's reads, in the order data.py runs them (ClickUp
# 86cbegwqg): the cep_public probe, the three version lists, then the counts
# and the digest. `syn` is indexed, `bti` is published but not indexed — the
# real state of the local database.
INDEX_ANSWERS = [
    [{'ok': 1}],                                  # cep_public reachable
    [{'v': V1024}, {'v': V768}],                  # embedding versions
    [{'v': 3}],                                   # chunking versions
    [{'v': 1}],                                   # mapping versions
    [{'t': 1, 'n': 3963}],                        # translation_chunks
    [{'t': 1, 'n': 2532}, {'t': 11, 'n': 2524}],  # psalm_verse_mappings
    [{'t': 1, 'v': V1024, 'n': 3963},             # chunk_embeddings
     {'t': 1, 'v': V768, 'n': 3963}],
    [{'t': 1, 'digest': 18030424974330788968}],   # chunks_digest
]
# Without translations the block stops after the version lists.
INDEX_ANSWERS_NO_TRANSLATIONS = INDEX_ANSWERS[:4]


def _manifest(translations=TRANSLATIONS, count_answers=COUNT_ANSWERS,
              index_answers=INDEX_ANSWERS):
    cursor = Mock()
    cursor.fetchall.side_effect = (
        [LANGUAGES, BIBLE_BOOKS, translations]
        + list(count_answers)
        + list(index_answers)
    )
    connection = Mock()
    connection.cursor.return_value = cursor
    with patch('data.create_connection', return_value=connection):
        from data import get_data_manifest
        return get_data_manifest(api_key=True), cursor


def _statements(cursor) -> list[str]:
    """The SQL actually sent, in order."""
    return [call.args[0] for call in cursor.execute.call_args_list]


def test_manifest_carries_the_reference_tables_and_the_work_list():
    manifest, _ = _manifest()

    assert manifest['languages'] == LANGUAGES
    assert manifest['bible_books'] == BIBLE_BOOKS
    assert manifest['translations'] == TRANSLATIONS
    # No corpus: the whole point is that this response is kilobytes.
    assert 'translation_verses' not in manifest
    assert 'voice_alignments' not in manifest


def test_manifest_totals_are_the_sum_of_the_per_translation_counts():
    manifest, _ = _manifest()
    totals = manifest['counts']['totals']
    per = manifest['counts']['per_translation']

    assert totals['translation_verses'] == 31361 + 31111
    assert totals['voice_alignments'] == 59437 + 31111
    assert totals['translations'] == 2
    assert totals['languages'] == 2
    assert totals['bible_books'] == 2

    for table in ('translation_books', 'translation_verses', 'voices',
                  'voice_alignments', 'translation_titles', 'translation_notes'):
        assert totals[table] == per['syn'][table] + per['bti'][table]

    assert per['syn']['translations'] == 1
    assert per['bti']['translation_verses'] == 31111


def test_a_translation_with_no_rows_of_a_table_counts_zero_not_missing():
    """`npu` publishes no notes; the importer must still see the key."""
    answers = [list(a) for a in COUNT_ANSWERS]
    answers[3] = [{'t': 1, 'n': 15707}]  # bti contributes no notes at all
    manifest, _ = _manifest(count_answers=answers)

    assert manifest['counts']['per_translation']['bti']['translation_notes'] == 0
    assert manifest['counts']['totals']['translation_notes'] == 15707


def test_no_active_translations_yields_zero_counts_and_no_count_queries():
    """An empty work list must be an honest empty answer, not a crash.

    Bible-API turns this into a refusal to resync; this endpoint's job is
    only to report it truthfully.
    """
    manifest, cursor = _manifest(translations=[], count_answers=[],
                                 index_answers=INDEX_ANSWERS_NO_TRANSLATIONS)

    assert manifest['translations'] == []
    assert manifest['counts']['per_translation'] == {}
    assert manifest['counts']['totals']['translation_verses'] == 0
    assert manifest['counts']['totals']['voice_alignments'] == 0
    assert manifest['index']['counts']['per_translation'] == {}
    # Three reads of cep_admin only: languages, bible_books, translations —
    # no per-table counting. (The index block still probes cep_public and
    # reads its version lists; those statements name the other database.)
    assert len([s for s in _statements(cursor) if 'cep_public' not in s]) == 3


def test_counts_use_the_same_predicates_as_the_export():
    """A count that disagrees with what /api/data ships is worse than none.

    The importer compares these numbers with `SELECT COUNT(*)` on cep_public
    after writing, so `voices`/`voice_alignments` must carry the export's
    `active = 1` filter and the notes query its two-sided join.
    """
    from data import MANIFEST_COUNT_SQL

    assert 'active = 1' in MANIFEST_COUNT_SQL['voices']
    assert 'v.active = 1' in MANIFEST_COUNT_SQL['voice_alignments']
    assert 'INNER JOIN voices v' in MANIFEST_COUNT_SQL['voice_alignments']
    notes = ' '.join(MANIFEST_COUNT_SQL['translation_notes'].split())
    assert 'LEFT JOIN translation_verses tv2' in notes
    assert 'tv.translation IN ({ph}) OR tv2.translation IN ({ph})' in notes


# --------------------------------------------------------------------------
# The `index` block (ClickUp 86cbegwqg) — the same report for the RAG index.
# --------------------------------------------------------------------------


def test_index_block_reports_the_versions_in_use_and_stays_small():
    manifest, _ = _manifest()
    index = manifest['index']

    assert index['available_versions'] == [V1024, V768]
    assert index['chunking_version'] == 3
    assert index['mapping_version'] == 1
    assert index['error'] is None
    # Still a plan, not a corpus: no chunk text, no vectors.
    assert 'translation_chunks' not in manifest
    assert 'chunk_embeddings' not in manifest


def test_index_counts_are_per_translation_and_per_embedding_version():
    """Global index totals would pass on compensating errors exactly as the
    text totals do, and an embedding version is only meaningful one at a
    time — the importer downloads one version of one translation."""
    per = _manifest()[0]['index']['counts']['per_translation']

    assert per['syn']['translation_chunks'] == 3963
    assert per['syn']['psalm_verse_mappings'] == 2532
    assert per['syn']['chunk_embeddings'] == {V1024: 3963, V768: 3963}


def test_a_published_but_unindexed_translation_counts_zero_not_missing():
    """`bti` has Psalm mappings and no chunks. The importer must be able to
    tell that from a manifest that simply forgot to mention it."""
    per = _manifest()[0]['index']['counts']['per_translation']

    assert per['bti']['translation_chunks'] == 0
    assert per['bti']['psalm_verse_mappings'] == 2524
    assert per['bti']['chunk_embeddings'] == {V1024: 0, V768: 0}
    assert _manifest()[0]['index']['chunks_digest']['bti'] is None


def test_chunks_digest_is_order_independent_and_per_translation():
    """One number per translation instead of downloading 3963 chunks to
    diff them — so it must not depend on row order or on AUTO_INCREMENT
    codes, neither of which survives a rebuild."""
    from data import INDEX_CHUNKS_DIGEST_SQL

    assert _manifest()[0]['index']['chunks_digest']['syn'] == 18030424974330788968

    sql = ' '.join(INDEX_CHUNKS_DIGEST_SQL.split())
    assert 'BIT_XOR(' in sql          # XOR is commutative: order cannot matter
    assert 'GROUP BY translation' in sql
    for column in ('canonical_id', 'char_count', "COALESCE(title,'')", 'text'):
        assert column in sql


def test_index_counts_use_the_same_predicates_as_the_index_export():
    """The `MANIFEST_COUNT_SQL` rule, applied to the index: these numbers are
    what `GET /api/data/index` must ship, table for table."""
    from data import INDEX_COUNT_SQL, INDEX_EXPORT_SQL

    for table in ('translation_chunks', 'psalm_verse_mappings', 'chunk_embeddings'):
        export = ' '.join(INDEX_EXPORT_SQL[table].split())
        count = ' '.join(INDEX_COUNT_SQL[table].split())
        assert f'FROM {{db}}.{table} WHERE translation IN ({{ph}})' in export
        assert f'FROM {{db}}.{table} WHERE translation IN ({{ph}})' in count

    # The export ships one version; the manifest reports every one of them.
    assert 'AND embedding_version = %s' in INDEX_EXPORT_SQL['chunk_embeddings']
    assert 'GROUP BY translation, embedding_version' in \
        ' '.join(INDEX_COUNT_SQL['chunk_embeddings'].split())


def test_an_unreachable_index_database_does_not_break_the_text_resync():
    """Production has no index tables until this feature ships there.

    The block reports why it is empty — a named refusal in a field, not a
    silent default — and every existing field of the manifest survives.
    """
    unreachable = Exception("1049 (42000): Unknown database 'cep_public'")
    cursor = Mock()
    cursor.fetchall.side_effect = (
        [LANGUAGES, BIBLE_BOOKS, TRANSLATIONS] + list(COUNT_ANSWERS) + [unreachable]
    )
    connection = Mock()
    connection.cursor.return_value = cursor
    with patch('data.create_connection', return_value=connection):
        from data import get_data_manifest
        manifest = get_data_manifest(api_key=True)

    assert manifest['counts']['totals']['translation_verses'] == 31361 + 31111
    assert manifest['index']['error'] is not None
    assert 'PUBLIC_DB_NAME' in manifest['index']['error']
    assert manifest['index']['available_versions'] == []
    assert manifest['index']['counts']['per_translation'] == {}


def test_the_index_block_is_an_addition_and_moves_nothing_that_existed():
    """Bible-API reads this manifest with the fields it already knows.

    The index block is new information for a new importer step (9b); the
    text resync must keep parsing the same document it parsed yesterday, so
    the top level gains exactly one key and loses none.
    """
    manifest, _ = _manifest()

    assert set(manifest) == {'languages', 'bible_books', 'translations',
                             'counts', 'index'}
    assert set(manifest['counts']) == {'per_translation', 'totals'}
    assert set(manifest['counts']['per_translation']['syn']) == {
        'translations', 'translation_books', 'translation_verses',
        'translation_titles', 'translation_notes', 'voices', 'voice_alignments',
    }
    # The index counts live in their own block, never merged into the text
    # ones: an importer summing `per_translation` must not pick up chunks.
    assert 'translation_chunks' not in manifest['counts']['per_translation']['syn']
    assert set(manifest['index']) == {
        'chunking_version', 'chunking_versions', 'mapping_version',
        'mapping_versions', 'available_versions', 'counts', 'chunks_digest',
        'error',
    }


def test_manifest_index_counts_agree_with_what_the_export_ships():
    """The two endpoints answer the same question and must answer it the
    same way: the manifest count is the importer's expected value and the
    export body is the observed one. Here both are driven from one set of
    rows, so a manifest that counted a different predicate than the export
    selects would show up as a mismatch rather than as data loss in 9b.
    """
    from data import get_data_index

    chunks = [{'canonical_id': f'v3:19.{n:03d}.001-004', 'translation': 1,
               'chunking_version': 3, 'title': None, 'text': 'x', 'char_count': 1}
              for n in range(1, 4)]
    mappings = [{'translation': 1, 'mapping_version': 1, 'book_number': 19,
                 'chapter_number': n, 'verse_number': 1} for n in range(1, 3)]
    embeddings = [{'canonical_id': c['canonical_id'], 'translation': 1,
                   'embedding_version': V1024, 'dims': 4, 'vector': b'\x00' * 16}
                  for c in chunks]

    # The manifest counts the very rows the export will ship.
    index_answers = [
        [{'ok': 1}],
        [{'v': V1024}],
        [{'v': 3}],
        [{'v': 1}],
        [{'t': 1, 'n': len(chunks)}],
        [{'t': 1, 'n': len(mappings)}],
        [{'t': 1, 'v': V1024, 'n': len(embeddings)}],
        [{'t': 1, 'digest': 42}],
    ]
    manifest, _ = _manifest(index_answers=index_answers)
    expected = manifest['index']['counts']['per_translation']['syn']

    cursor = Mock()
    cursor.fetchall.side_effect = [
        [{'ok': 1}],
        [{'code': 1, 'alias': 'syn'}],
        [{'v': V1024}],
        [{'t': 1, 'v': V1024, 'n': len(embeddings)}],
        chunks,
        mappings,
        embeddings,
    ]
    connection = Mock()
    connection.cursor.return_value = cursor
    with patch('data.create_connection', return_value=connection):
        body = get_data_index(
            translation='syn', embedding_version=V1024, chunking_version=None,
            mapping_version=None, limit=2000, offset=0, api_key=True,
        )

    assert expected['translation_chunks'] == len(body['translation_chunks'])
    assert expected['psalm_verse_mappings'] == len(body['psalm_verse_mappings'])
    assert expected['chunk_embeddings'][V1024] == body['chunk_embeddings_total']
    assert body['chunk_embeddings_total'] == len(body['chunk_embeddings'])
    assert body['next_offset'] is None
