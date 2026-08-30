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


def _manifest(translations=TRANSLATIONS, count_answers=COUNT_ANSWERS):
    cursor = Mock()
    cursor.fetchall.side_effect = [LANGUAGES, BIBLE_BOOKS, translations] + list(
        count_answers
    )
    connection = Mock()
    connection.cursor.return_value = cursor
    with patch('data.create_connection', return_value=connection):
        from data import get_data_manifest
        return get_data_manifest(api_key=True), cursor


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
    manifest, cursor = _manifest(translations=[], count_answers=[])

    assert manifest['translations'] == []
    assert manifest['counts']['per_translation'] == {}
    assert manifest['counts']['totals']['translation_verses'] == 0
    assert manifest['counts']['totals']['voice_alignments'] == 0
    # Three reads only: languages, bible_books, translations.
    assert cursor.execute.call_count == 3


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
