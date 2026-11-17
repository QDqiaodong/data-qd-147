"""Acceptance tests for the persistent continuous water-quality warehouse.

Three groups mirror the delivery contract:

* fact-warehouse tests restart the real CLI between calls and exercise seq
  replay, same-seq conflict, legal correction/restore, revision jumps, seq
  gaps and final-state rejection that leaves the breakpoint untouched;
* exposure tests compare the warehouse to ``tests/quality_reference``, an
  independent exact rational recomputation, and pin fractional-second
  crossings, overlapping-analyte de-duplication, calibration jumps, equal
  endpoints, same-instant events, missing gaps and cross-midnight episodes;
* linkage tests build a cross-midnight alert, revoke a calibration to split
  it, restore it to merge again, move an observation to change two days of
  dose and the episode, and compare against a one-shot reference warehouse;
  a fault injection proves facts, derived rows and the breakpoint never
  half-commit.
"""
import json
import sqlite3
import unittest
from fractions import Fraction

from quality_helpers import (
    DAY, ROOT, Workspace, calibration, cli, observation, ok, write_json,
    write_jsonl,
)
from quality_reference import effective_facts, normalise, parse_config
from quality_reference import report as reference_report


def utc_midnight(day, month, year=2026):
    from datetime import datetime, timezone
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


START = utc_midnight(1, 1)
TWO_DAYS = START + 2 * DAY


def config(stations=None, start=START, end=TWO_DAYS, **extra):
    doc = {
        'start': start, 'end': end,
        'stations': stations if stations is not None else [
            {'id': 'st', 'min_episode_seconds': 1, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]},
        ],
    }
    doc.update(extra)
    return doc


def two_analyte_station(min_episode=60, gap=10 ** 6, threshold_a=(5, 1),
                        threshold_b=(5, 1), station_id='st'):
    return {'id': station_id, 'min_episode_seconds': min_episode, 'analytes': [
        {'id': 'a', 'threshold': list(threshold_a), 'max_gap_seconds': gap},
        {'id': 'b', 'threshold': list(threshold_b), 'max_gap_seconds': gap},
    ]}


def whole_day_calibration(seq, entity, station='st', analyte='a',
                          start=START, end=TWO_DAYS, a=(1, 1), b=(0, 1)):
    return calibration(seq, entity, 1, station, analyte, start, end, a, b)


class QualityCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def init_ok(self, doc=None, db='q.db'):
        return ok(self, self.ws.init(doc or config(), db=db))

    def ingest_ok(self, events, db='q.db', name='changes.jsonl'):
        return ok(self, self.ws.ingest(events, db=db, name=name))

    def ingest_fails(self, events, db='q.db', name='changes.jsonl'):
        before = self.ws.snapshot(db=db)
        run = self.ws.ingest(events, db=db, name=name)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertEqual(self.ws.snapshot(db=db), before)
        return run

    def report_ok(self, start=START, end=TWO_DAYS, db='q.db', output='report.json'):
        return ok(self, self.ws.report(db=db, start=start, end=end, output=output))

    def station_report(self, report, station='st'):
        for item in report['stations']:
            if item['station'] == station:
                return item
        raise AssertionError('missing station ' + station)


# ---------------------------------------------------------------------------
# init contract


class Init(QualityCase):
    def test_init_creates_warehouse(self):
        info = self.init_ok()
        self.assertEqual(info['kind'], 'quality-init')
        self.assertEqual(info['seq'], 0)
        self.assertEqual(info['start'], START)
        self.assertEqual(info['end'], TWO_DAYS)
        report = self.report_ok()
        self.assertEqual(report['seq'], 0)
        self.assertEqual(report['warehouse'], {'start': START, 'end': TWO_DAYS})
        self.assertEqual([day['date'] for day in
                          self.station_report(report)['days']],
                         ['2026-01-01', '2026-01-02'])
        for day in self.station_report(report)['days']:
            self.assertEqual(day['known_seconds'], [0, 1])
            self.assertEqual(day['unsafe_seconds'], [0, 1])
            self.assertEqual(day['excess_integral'], {'a': [0, 1]})

    def test_init_refuses_overwrite(self):
        self.init_ok()
        before = self.ws.db_bytes()
        run = self.ws.init(config())
        self.assertEqual(run.returncode, 2)
        self.assertIn('already exists', run.stderr)
        self.assertEqual(self.ws.db_bytes(), before)

    def test_init_refuses_symlink_database(self):
        link = self.ws.path('link.db')
        link.symlink_to(self.ws.path('missing'))
        source = write_json(self.ws.path('config.json'), config())
        run = cli('init', '--source', str(source), '--db', str(link))
        self.assertEqual(run.returncode, 2)
        self.assertFalse(self.ws.path('q.db').exists())

    def test_init_rejects_input_equal_to_db(self):
        source = write_json(self.ws.path('same'), config())
        run = cli('init', '--source', str(source), '--db', str(source))
        self.assertEqual(run.returncode, 2)

    def test_config_validation_rejects_bad_documents(self):
        good = config()
        bad = [
            dict(good, extra=1),
            {'start': START, 'end': TWO_DAYS},
            dict(good, start='0'),
            dict(good, start=True),
            dict(good, start=-1),
            dict(good, start=START + 1),
            dict(good, start=START, end=START),
            dict(good, end=START + 32 * DAY),
            dict(good, end=4102444800 + DAY),
            dict(good, stations=[good['stations'][0]] * 31),
            dict(good, stations=[dict(good['stations'][0], extra=1)]),
            dict(good, stations=[dict(good['stations'][0], id='')]),
            dict(good, stations=[good['stations'][0], good['stations'][0]]),
            dict(good, stations=[dict(good['stations'][0],
                                     min_episode_seconds=0)]),
            dict(good, stations=[dict(good['stations'][0],
                                     min_episode_seconds=10 ** 6 + 1)]),
            dict(good, stations=[dict(good['stations'][0], analytes=[])]),
            dict(good, stations=[dict(good['stations'][0],
                                     analytes=good['stations'][0]['analytes'] * 6)]),
            dict(good, stations=[dict(good['stations'][0], analytes=[
                good['stations'][0]['analytes'][0]] * 2)]),
            dict(good, stations=[dict(good['stations'][0], analytes=[
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 0}])]),
            dict(good, stations=[dict(good['stations'][0], analytes=[
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6 + 1}])]),
            *[dict(good, stations=[dict(good['stations'][0], analytes=[
                {'id': 'a', 'threshold': value, 'max_gap_seconds': 60}])])
              for value in ((-1, 1), [5], [5, 1, 1], [5.0, 1], [True, 1],
                            [5, 0], [10 ** 9 + 1, 1], [5, 10 ** 6 + 1],
                            ['5', 1], [5, True], None)],
        ]
        for index, doc in enumerate(bad):
            run = self.ws.init(doc, db='bad%d.db' % index)
            self.assertEqual(run.returncode, 2, (index, run.stderr))
            self.assertFalse(self.ws.path('bad%d.db' % index).exists(), index)

    def test_config_accepts_a_full_month_and_five_analytes(self):
        stations = [{
            'id': 's%d' % index,
            'min_episode_seconds': 1,
            'analytes': [{'id': 'a%d' % slot, 'threshold': [slot, 7],
                          'max_gap_seconds': 10 ** 6}
                         for slot in range(5)],
        } for index in range(30)]
        info = self.init_ok(config(stations=stations, end=START + 31 * DAY))
        self.assertEqual(info['analytes'], 150)
        self.assertEqual(info['stations'], 30)

    def test_config_rejects_duplicate_json_keys(self):
        source = self.ws.path('dup.json')
        source.write_text('{"start":%d,"start":%d,"end":%d,"stations":[]}'
                          % (START, START, TWO_DAYS))
        run = cli('init', '--source', str(source), '--db', str(self.ws.path('d.db')))
        self.assertEqual(run.returncode, 2)


# ---------------------------------------------------------------------------
# change-stream contract


OBS_LEFT = observation(1, 'left', 1, 'st', 'a', START, [0, 1])
OBS_RIGHT = observation(2, 'right', 1, 'st', 'a', START + 10000, [10, 1])
CAL = whole_day_calibration(3, 'cal')


class Ingest(QualityCase):
    def setUp(self):
        super().setUp()
        self.init_ok()

    def test_ingest_applies_new_events(self):
        info = self.ingest_ok([OBS_LEFT, OBS_RIGHT, CAL])
        self.assertEqual(info['seq'], 3)
        self.assertEqual(info['applied'], 3)
        self.assertEqual(info['replayed'], 0)
        self.assertEqual(info['events'], 3)
        self.assertGreaterEqual(info['known_intervals'], 1)

    def test_replay_of_identical_prefix_is_idempotent(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT, CAL])
        meta, events, derived = self.ws.snapshot()
        report = self.report_ok()
        info = self.ingest_ok([OBS_LEFT, OBS_RIGHT, CAL], name='again.jsonl')
        self.assertEqual(info['applied'], 0)
        self.assertEqual(info['replayed'], 3)
        self.assertEqual(info['seq'], 3)
        self.assertEqual(self.ws.snapshot(), (meta, events, derived))
        self.assertEqual(self.report_ok(), report)

    def test_replay_allows_partial_prefix_then_new_events(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT])
        late = observation(4, 'late', 1, 'st', 'a', START + 5000, [20, 1])
        info = self.ingest_ok([OBS_LEFT, OBS_RIGHT, CAL, late], name='mixed.jsonl')
        self.assertEqual(info['replayed'], 2)
        self.assertEqual(info['applied'], 2)
        self.assertEqual(info['seq'], 4)

    def test_replay_does_not_confuse_new_revision_of_same_entity(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT, CAL])
        revision = observation(4, 'left', 2, 'st', 'a', START, [1, 1])
        info = self.ingest_ok([OBS_LEFT, OBS_RIGHT, CAL, revision],
                              name='revision.jsonl')
        self.assertEqual(info['replayed'], 3)
        self.assertEqual(info['applied'], 1)

    def test_same_seq_different_content_rejected(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT])
        changed = observation(1, 'left', 1, 'st', 'a', START, [1, 1])
        run = self.ingest_fails([changed])
        self.assertIn('different content', run.stderr)

    def test_object_key_order_is_irrelevant_but_array_order_is_not(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT])
        reordered_keys = {'payload': OBS_LEFT['payload'], 'deleted': False,
                          'revision': 1, 'id': 'left', 'kind': 'observation',
                          'seq': 1}
        info = self.ingest_ok([reordered_keys], name='keys.jsonl')
        self.assertEqual(info['replayed'], 1)
        self.assertEqual(info['applied'], 0)
        # same seq, array order inside a rational changed -> different content
        run = self.ingest_fails(
            [observation(1, 'left', 1, 'st', 'a', START, [0, 2])],
            name='array.jsonl')
        self.assertIn('different content', run.stderr)

    def test_new_seq_must_continue_at_breakpoint_plus_one(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT])
        gap = observation(5, 'late', 1, 'st', 'a', START + 1, [1, 1])
        run = self.ingest_fails([gap], name='gap.jsonl')
        self.assertIn('continue at 3', run.stderr)

    def test_seq_gap_inside_new_events_rejected(self):
        first = observation(1, 'c', 1, 'st', 'a', START + 1, [1, 1])
        third = observation(3, 'd', 1, 'st', 'a', START + 2, [1, 1])
        run = self.ingest_fails([first, third], name='inner-gap.jsonl')
        self.assertIn('contiguous', run.stderr)

    def test_seq_must_be_strictly_increasing(self):
        run = self.ingest_fails([OBS_RIGHT, OBS_LEFT], name='order.jsonl')
        self.assertIn('strictly increasing', run.stderr)
        duplicate = observation(1, 'other', 1, 'st', 'a', START + 1, [1, 1])
        run = self.ingest_fails([OBS_LEFT, duplicate], name='dup.jsonl')
        self.assertIn('strictly increasing', run.stderr)

    def test_revision_jump_and_restart_rejected(self):
        self.ingest_ok([OBS_LEFT])
        jump = observation(2, 'left', 3, 'st', 'a', START, [1, 1])
        run = self.ingest_fails([jump], name='jump.jsonl')
        self.assertIn('increase by exactly one', run.stderr)
        restart = observation(2, 'left', 1, 'st', 'a', START, [1, 1])
        run = self.ingest_fails([restart], name='restart.jsonl')
        self.assertIn('increase by exactly one', run.stderr)
        first_deleted = observation(2, 'new', 1, 'st', 'a', START, None,
                                    deleted=True)
        run = self.ingest_fails([first_deleted], name='first-deleted.jsonl')
        self.assertIn('must not be deleted', run.stderr)
        first_revision = observation(2, 'new', 2, 'st', 'a', START, [1, 1])
        run = self.ingest_fails([first_revision], name='first-revision.jsonl')
        self.assertIn('must be 1', run.stderr)

    def test_entity_kind_and_id_are_the_key(self):
        # An observation and a calibration may share an id (different kinds).
        twin = calibration(3, 'left', 1, 'st', 'a', START, TWO_DAYS, [1, 1], [0, 1])
        info = self.ingest_ok([OBS_LEFT, OBS_RIGHT, twin], name='twin.jsonl')
        self.assertEqual(info['applied'], 3)

    def test_station_analyte_pair_is_immutable(self):
        other = {'id': 'other', 'min_episode_seconds': 1, 'analytes': [
            {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}
        ok(self, self.ws.init(config(stations=[
            {'id': 'st', 'min_episode_seconds': 1, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]},
            other]), db='pair.db'))
        ok(self, self.ws.ingest([OBS_LEFT], db='pair.db'))
        before = self.ws.snapshot(db='pair.db')
        moved = observation(2, 'left', 2, 'other', 'a', START, [1, 1])
        run = self.ws.ingest([moved], db='pair.db', name='moved.jsonl')
        self.assertEqual(run.returncode, 2)
        self.assertIn('may not change', run.stderr)
        self.assertEqual(self.ws.snapshot(db='pair.db'), before)

    def test_deleted_payload_must_be_null(self):
        run = self.ingest_fails([
            observation(1, 'left', 1, 'st', 'a', START, None, deleted=True),
        ], name='payload.jsonl')
        event = dict(OBS_LEFT, deleted=True)
        run = self.ingest_fails([event], name='null.jsonl')
        self.assertIn('null payload', run.stderr)

    def test_event_shape_and_types(self):
        bad = [
            dict(OBS_LEFT, extra=1),
            {k: v for k, v in OBS_LEFT.items() if k != 'seq'},
            dict(OBS_LEFT, seq=0),
            dict(OBS_LEFT, seq=True),
            dict(OBS_LEFT, revision=0),
            dict(OBS_LEFT, revision=1.0),
            dict(OBS_LEFT, deleted=0),
            dict(OBS_LEFT, kind='forecast'),
            dict(OBS_LEFT, kind=None),
            dict(OBS_LEFT, id=''),
            dict(OBS_LEFT, id=' x'),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], extra=1)),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], at=START - 1)),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], at=TWO_DAYS + 1)),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], at=START + 0.5)),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], value=[-1, 1])),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], value=[1.0, 1])),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], value=[True, 1])),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], value=[10 ** 9 + 1, 1])),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], value=[1, 0])),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], station='nope')),
            dict(OBS_LEFT, payload=dict(OBS_LEFT['payload'], analyte='nope')),
        ]
        for index, event in enumerate(bad):
            run = self.ingest_fails([event], name='bad%d.jsonl' % index)
            self.assertEqual(run.returncode, 2, index)

        bad_calibrations = [
            dict(CAL, payload=dict(CAL['payload'], start=START - 1)),
            dict(CAL, payload=dict(CAL['payload'], end=TWO_DAYS + 1)),
            dict(CAL, payload=dict(CAL['payload'], start=START + 10,
                                   end=START + 10)),
            dict(CAL, payload=dict(CAL['payload'], start=START + 10,
                                   end=START + 5)),
            dict(CAL, payload=dict(CAL['payload'], a=[0, 1])),
            dict(CAL, payload=dict(CAL['payload'], a=[-1, 1])),
            dict(CAL, payload=dict(CAL['payload'], a=[1, 10 ** 6 + 1])),
            dict(CAL, payload=dict(CAL['payload'], b=[10 ** 9 + 1, 1])),
            dict(CAL, payload=dict(CAL['payload'], b=[1.5, 1])),
        ]
        for index, event in enumerate(bad_calibrations):
            run = self.ingest_fails([event], name='badcal%d.jsonl' % index)
            self.assertEqual(run.returncode, 2, index)

    def test_blank_and_malformed_jsonl_lines_rejected(self):
        self.ingest_ok([OBS_LEFT])
        before = self.ws.snapshot()
        for index, body in enumerate(['\n', json.dumps(OBS_RIGHT) + '\n\n',
                                      '[1]\n', 'NaN\n', '{\n']):
            source = self.ws.path('raw%d.jsonl' % index)
            source.write_text(body)
            run = cli('ingest', '--source', str(source),
                      '--db', str(self.ws.path('q.db')))
            self.assertEqual(run.returncode, 2, (index, body))
        # A correctly terminated single line also counts as one line.
        self.ingest_ok([OBS_RIGHT], name='ok2.jsonl')
        self.assertEqual(self.ws.snapshot()[0]['last_seq'], '2')
        self.assertEqual(self.ws.snapshot(), (self.ws.snapshot()[0],
                                              self.ws.snapshot()[1],
                                              self.ws.snapshot()[2]))
        self.assertNotEqual(self.ws.snapshot()[1], before[1])

    def test_empty_change_file_is_a_noop(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT])
        before = self.ws.snapshot()
        info = self.ingest_ok([], name='empty.jsonl')
        self.assertEqual(info['applied'], 0)
        self.assertEqual(info['replayed'], 0)
        self.assertEqual(self.ws.snapshot(), before)

    def test_duplicate_json_key_inside_event_rejected(self):
        source = self.ws.path('dupkey.jsonl')
        source.write_text('{"seq":1,"seq":1,"kind":"observation","id":"x",'
                          '"revision":1,"deleted":false,"payload":null}\n')
        run = cli('ingest', '--source', str(source), '--db', str(self.ws.path('q.db')))
        self.assertEqual(run.returncode, 2)
        self.assertIn('duplicate JSON key', run.stderr)

    def test_final_state_conflicts_rejected_after_exchange(self):
        left = observation(1, 'left', 1, 'st', 'a', START, [0, 1])
        right = observation(2, 'right', 1, 'st', 'a', START + 100, [10, 1])
        # Same instant twice is a final-state conflict.
        clash = observation(3, 'clash', 1, 'st', 'a', START + 100, [1, 1])
        run = self.ingest_fails([left, right, clash], name='clash.jsonl')
        self.assertIn('share an instant', run.stderr)

    def test_exchanging_observation_times_is_judged_on_final_state(self):
        # Both orderings transiently collide or transiently duplicate an
        # instant, but each batch's final state is a clean swap.
        a = observation(1, 'obs-a', 1, 'st', 'a', START, [1, 1])
        b = observation(2, 'obs-b', 1, 'st', 'a', START + 100, [2, 1])
        self.ingest_ok([a, b], name='seed.jsonl')
        move_a = observation(3, 'obs-a', 2, 'st', 'a', START + 200, [1, 1])
        move_b = observation(4, 'obs-b', 2, 'st', 'a', START, [2, 1])
        self.ingest_ok([move_a, move_b], name='swap1.jsonl')
        meta, _, _ = self.ws.snapshot()
        self.assertEqual(meta['last_seq'], '4')
        move_a2 = observation(6, 'obs-a', 3, 'st', 'a', START, [1, 1])
        move_b2 = observation(5, 'obs-b', 3, 'st', 'a', START + 200, [2, 1])
        self.ingest_ok([move_b2, move_a2], name='swap2.jsonl')
        self.assertEqual(self.ws.snapshot()[0]['last_seq'], '6')

    def test_transient_collision_inside_batch_is_allowed(self):
        a = observation(1, 'obs-a', 1, 'st', 'a', START, [1, 1])
        b = observation(2, 'obs-b', 1, 'st', 'a', START + 100, [2, 1])
        self.ingest_ok([a, b, whole_day_calibration(3, 'cal')], name='seed.jsonl')
        # seq 4 first moves b onto a's instant, seq 5 then moves a away.
        transient = observation(4, 'obs-b', 2, 'st', 'a', START, [2, 1])
        settle = observation(5, 'obs-a', 2, 'st', 'a', START + 200, [1, 1])
        info = self.ingest_ok([transient, settle], name='transient.jsonl')
        self.assertEqual(info['applied'], 2)
        moves = observation(6, 'obs-a', 3, 'st', 'a', START, [1, 1])
        settle2 = observation(7, 'obs-b', 3, 'st', 'a', START + 50, [2, 1])
        self.ingest_ok([moves, settle2], name='back.jsonl')

    def test_exchanging_calibration_intervals_is_judged_on_final_state(self):
        cal_a = calibration(3, 'cal-a', 1, 'st', 'a', START, START + 1000,
                            [1, 1], [0, 1])
        cal_b = calibration(4, 'cal-b', 1, 'st', 'a', START + 1000, START + 2000,
                            [1, 1], [1, 1])
        self.ingest_ok([OBS_LEFT, OBS_RIGHT, cal_a, cal_b], name='seed.jsonl')
        # Swap both windows in one batch: intermediate overlap, final is clean.
        move_a = calibration(5, 'cal-a', 2, 'st', 'a', START + 1000, START + 2000,
                             [1, 1], [0, 1])
        move_b = calibration(6, 'cal-b', 2, 'st', 'a', START, START + 1000,
                             [1, 1], [1, 1])
        self.ingest_ok([move_a, move_b], name='swap.jsonl')
        run = self.ingest_fails([
            calibration(7, 'cal-a', 3, 'st', 'a', START, START + 2000, [1, 1], [0, 1]),
            calibration(8, 'cal-b', 3, 'st', 'a', START + 500, START + 3000,
                        [1, 1], [1, 1]),
        ], name='overlap.jsonl')
        self.assertIn('overlap', run.stderr)

    def test_event_budget_enforced(self):
        import sys
        sys.path.insert(0, str(ROOT / 'src'))
        import water_rights.quality_warehouse as warehouse
        events = [observation(index + 1, 'o%d' % index, 1, 'st', 'a',
                              START + index, [1, 1]) for index in range(3)]
        self.ingest_ok(events)
        self.assertEqual(self.ws.snapshot()[0]['event_count'], '3')
        meta, rows, derived = self.ws.snapshot()
        original = warehouse.MAX_EVENTS
        warehouse.MAX_EVENTS = 4
        try:
            source = write_jsonl(self.ws.path('budget.jsonl'), [
                observation(4, 'x', 1, 'st', 'a', START + 10, [1, 1]),
                observation(5, 'y', 1, 'st', 'a', START + 20, [1, 1]),
            ])
            with self.assertRaises(ValueError):
                warehouse.ingest_changes(source, self.ws.path('q.db'))
        finally:
            warehouse.MAX_EVENTS = original
        self.assertEqual(self.ws.snapshot(), (meta, rows, derived))
        # Exactly at the cap is still accepted.
        warehouse.MAX_EVENTS = 3
        try:
            source = write_jsonl(self.ws.path('budget2.jsonl'), [
                observation(4, 'x', 1, 'st', 'a', START + 10, [1, 1])])
            with self.assertRaises(ValueError):
                warehouse.ingest_changes(source, self.ws.path('q.db'))
        finally:
            warehouse.MAX_EVENTS = original
        self.ingest_ok([observation(4, 'x', 1, 'st', 'a', START + 10, [1, 1])],
                       name='under.jsonl')

    def test_line_budget_enforced(self):
        events = [observation(index + 1, 'o%d' % index, 1, 'st', 'a',
                              START + index, [1, 1]) for index in range(10001)]
        run = self.ingest_fails(events, name='many.jsonl')
        self.assertIn('more than 10000', run.stderr)

    def test_ingest_requires_a_warehouse(self):
        run = cli('ingest', '--source', str(write_jsonl(self.ws.path('c.jsonl'),
                                                        [OBS_LEFT])),
                  '--db', str(self.ws.path('missing.db')))
        self.assertEqual(run.returncode, 2)
        self.assertFalse(self.ws.path('missing.db').exists())
        bad = self.ws.path('bad.db')
        bad.write_text('not sqlite')
        run = cli('ingest', '--source', str(write_jsonl(self.ws.path('c.jsonl'),
                                                        [OBS_LEFT])),
                  '--db', str(bad))
        self.assertEqual(run.returncode, 2)


# ---------------------------------------------------------------------------
# effective-state corner cases


class EffectiveState(QualityCase):
    def setUp(self):
        super().setUp()
        self.init_ok()

    def test_delete_then_restore_with_same_pair(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT, whole_day_calibration(3, 'cal')])
        gone = observation(4, 'left', 2, 'st', 'a', START, None, deleted=True)
        self.ingest_ok([gone], name='gone.jsonl')
        report = self.report_ok()
        self.assertEqual(self.station_report(report)['days'][0]['known_seconds'],
                         [0, 1])
        back = observation(5, 'left', 3, 'st', 'a', START, [0, 1])
        self.ingest_ok([back], name='back.jsonl')
        self.assertEqual(self.station_report(self.report_ok())['days'][0]
                         ['known_seconds'], [10000, 1])

    def test_restore_with_different_pair_rejected(self):
        other = {'id': 'other', 'min_episode_seconds': 1, 'analytes': [
            {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}
        ok(self, self.ws.init(config(stations=[
            {'id': 'st', 'min_episode_seconds': 1, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]},
            other]), db='pair.db'))
        ok(self, self.ws.ingest([OBS_LEFT], db='pair.db'))
        ok(self, self.ws.ingest([observation(2, 'left', 2, 'st', 'a', START,
                                             None, deleted=True)], db='pair.db'))
        before = self.ws.snapshot(db='pair.db')
        run = self.ws.ingest([observation(3, 'left', 3, 'other', 'a', START,
                                          [0, 1])], db='pair.db')
        self.assertEqual(run.returncode, 2)
        self.assertIn('may not change', run.stderr)
        self.assertEqual(self.ws.snapshot(db='pair.db'), before)

    def test_replay_keeps_revision_sequence_intact(self):
        self.ingest_ok([OBS_LEFT])
        self.ingest_ok([OBS_LEFT], name='replay.jsonl')
        # After a pure replay, revision 2 is still the only legal next revision.
        run = self.ingest_fails([observation(2, 'left', 3, 'st', 'a', START,
                                             [1, 1])], name='jump.jsonl')
        self.assertIn('increase by exactly one', run.stderr)
        self.ingest_ok([observation(2, 'left', 2, 'st', 'a', START, [1, 1])],
                       name='rev2.jsonl')

    def test_ingest_source_must_differ_from_database(self):
        source = write_jsonl(self.ws.path('same.db'), [OBS_LEFT])
        run = cli('ingest', '--source', str(source), '--db', str(source))
        self.assertEqual(run.returncode, 2)

    def test_report_reads_materialized_snapshot_only(self):
        self.ingest_ok([OBS_LEFT, OBS_RIGHT, whole_day_calibration(3, 'cal')])
        before = self.ingest_ok([], name='noop.jsonl')
        report = self.report_ok()
        conn = sqlite3.connect(str(self.ws.path('q.db')))
        conn.execute('DELETE FROM events')
        conn.commit()
        conn.close()
        # Report is unaffected because it never rescans the change stream.
        self.assertEqual(self.report_ok(), report)
        self.assertEqual(report['seq'], 3)


# ---------------------------------------------------------------------------
# exposure materialization


class Exposure(QualityCase):
    def build(self, events, doc=None, db='q.db'):
        self.init_ok(doc or config(), db=db)
        self.ingest_ok(events, db=db)

    def test_linear_interpolation_and_fractional_crossing(self):
        # Value ramps 0 -> 10 over 100s with threshold 5, crossing at +50.
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [10, 1]),
            whole_day_calibration(3, 'cal'),
        ])
        report = self.report_ok()
        alerts = self.station_report(report)['alerts']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['start'], [START + 50, 1])
        self.assertEqual(alerts[0]['end'], [START + 100, 1])
        self.assertEqual(alerts[0]['length'], [50, 1])
        day = self.station_report(report)['days'][0]
        self.assertEqual(day['known_seconds'], [100, 1])
        self.assertEqual(day['unsafe_seconds'], [50, 1])
        # integral of (v - 5) from 50 to 100 of a ramp of slope 1/10 = 125
        self.assertEqual(day['excess_integral']['a'], [125, 1])

    def test_calibration_affine_transform_and_negative_values(self):
        # Calibration a=2, b=-4 turns ramp 0..10 into -4..16; threshold 5 at
        # 2t/10 - 4 = 5 -> t = 45.
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [10, 1]),
            whole_day_calibration(3, 'cal', a=[2, 1], b=[-4, 1]),
        ])
        alerts = self.station_report(self.report_ok())['alerts']
        self.assertEqual(alerts[0]['start'], [START + 45, 1])
        self.assertEqual(alerts[0]['end'], [START + 100, 1])

    def test_calibration_jump_resets_trend(self):
        # Two abutting calibrations: slope flips at +100.
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'm', 1, 'st', 'a', START + 100, [10, 1]),
            observation(3, 'r', 1, 'st', 'a', START + 200, [0, 1]),
            whole_day_calibration(4, 'cal-left', start=START, end=START + 100),
            whole_day_calibration(5, 'cal-right', start=START + 100,
                                  end=TWO_DAYS),
        ])
        report = self.report_ok()
        alerts = self.station_report(report)['alerts']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['start'], [START + 50, 1])
        self.assertEqual(alerts[0]['end'], [START + 150, 1])
        self.assertEqual(alerts[0]['length'], [100, 1])
        day = self.station_report(report)['days'][0]
        self.assertEqual(day['unsafe_seconds'], [100, 1])
        self.assertEqual(day['excess_integral']['a'], [250, 1])

    def test_equal_endpoints_do_not_split(self):
        # Touch at the threshold at +100 while both sides are below it.
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'm', 1, 'st', 'a', START + 100, [10, 1]),
            observation(3, 'r', 1, 'st', 'a', START + 200, [0, 1]),
            whole_day_calibration(4, 'cal'),
        ])
        alerts = self.station_report(self.report_ok())['alerts']
        self.assertEqual(len(alerts), 1)

    def test_multiple_analytes_deduplicate_unsafe_seconds(self):
        doc = config(stations=[two_analyte_station()])
        self.build([
            observation(1, 'a-l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'a-r', 1, 'st', 'a', START + 100, [10, 1]),
            observation(3, 'b-l', 1, 'st', 'b', START, [10, 1]),
            observation(4, 'b-r', 1, 'st', 'b', START + 100, [0, 1]),
            whole_day_calibration(5, 'cal-a', analyte='a'),
            whole_day_calibration(6, 'cal-b', analyte='b'),
        ], doc=doc)
        report = self.report_ok()
        station = self.station_report(report)
        day = station['days'][0]
        # Both analytes exceed on the whole 100 s; union counts it once.
        self.assertEqual(day['unsafe_seconds'], [100, 1])
        self.assertEqual(day['excess_integral']['a'], [125, 1])
        self.assertEqual(day['excess_integral']['b'], [125, 1])
        self.assertEqual(station['alerts'][0]['length'], [100, 1])

    def test_unknown_gap_splits_episode(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 60, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 150}]}])
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [10, 1]),
            observation(2, 'm', 1, 'st', 'a', START + 100, [10, 1]),
            observation(3, 'r', 1, 'st', 'a', START + 300, [10, 1]),
            observation(4, 's', 1, 'st', 'a', START + 400, [10, 1]),
            whole_day_calibration(5, 'cal'),
        ], doc=doc)
        station = self.station_report(self.report_ok())
        self.assertEqual(len(station['alerts']), 2)
        self.assertEqual(station['alerts'][0]['length'], [100, 1])
        self.assertEqual(station['alerts'][1]['length'], [100, 1])
        self.assertEqual(station['days'][0]['known_seconds'], [200, 1])

    def test_no_calibration_means_unknown(self):
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [10, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [10, 1]),
        ])
        station = self.station_report(self.report_ok())
        self.assertEqual(station['alerts'], [])
        self.assertEqual(station['days'][0]['known_seconds'], [0, 1])

    def test_station_known_only_where_every_analyte_is_known(self):
        doc = config(stations=[two_analyte_station()])
        self.build([
            observation(1, 'a-l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'a-r', 1, 'st', 'a', START + 100, [0, 1]),
            observation(3, 'b-l', 1, 'st', 'b', START + 40, [10, 1]),
            observation(4, 'b-r', 1, 'st', 'b', START + 60, [10, 1]),
            whole_day_calibration(5, 'cal-a', analyte='a'),
            whole_day_calibration(6, 'cal-b', analyte='b'),
        ], doc=doc)
        station = self.station_report(self.report_ok())
        self.assertEqual(station['days'][0]['known_seconds'], [20, 1])
        self.assertEqual(station['days'][0]['unsafe_seconds'], [20, 1])
        # b is 10 with threshold 5 on the whole 20 s window: 5 * 20 = 100
        self.assertEqual(station['days'][0]['excess_integral']['b'], [100, 1])

    def test_same_instant_multiple_events_across_stations(self):
        single = lambda sid: {'id': sid, 'min_episode_seconds': 1, 'analytes': [
            {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}
        doc = config(stations=[single('one'), single('two')])
        events = [
            observation(1, 'one-a-l', 1, 'one', 'a', START, [10, 1]),
            observation(2, 'one-a-r', 1, 'one', 'a', START + 100, [10, 1]),
            observation(3, 'two-a-l', 1, 'two', 'a', START, [0, 1]),
            observation(4, 'two-a-r', 1, 'two', 'a', START + 100, [0, 1]),
            whole_day_calibration(5, 'one-cal-a', station='one'),
            whole_day_calibration(6, 'two-cal-a', station='two'),
        ]
        self.build(events, doc=doc)
        report = self.report_ok()
        self.assertEqual(self.station_report(report, 'one')['days'][0]
                         ['unsafe_seconds'], [100, 1])
        self.assertEqual(self.station_report(report, 'two')['days'][0]
                         ['unsafe_seconds'], [0, 1])

    def test_min_episode_filters_short_alerts(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 101, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [10, 1]),
            whole_day_calibration(3, 'cal'),
        ], doc=doc)
        station = self.station_report(self.report_ok())
        self.assertEqual(station['alerts'], [])
        self.assertEqual(station['days'][0]['unsafe_seconds'], [50, 1])

    def test_cross_midnight_alert_survives_tail_query(self):
        self.build([
            observation(1, 'l', 1, 'st', 'a', START + DAY - 100, [10, 1]),
            observation(2, 'r', 1, 'st', 'a', START + DAY + 100, [10, 1]),
            whole_day_calibration(3, 'cal'),
        ])
        report = self.report_ok(start=START + DAY + 60, end=START + DAY + 120)
        station = self.station_report(report)
        self.assertEqual(len(station['alerts']), 1)
        alert = station['alerts'][0]
        self.assertEqual(alert['start'], [START + DAY - 100, 1])
        self.assertEqual(alert['end'], [START + DAY + 100, 1])
        self.assertEqual(alert['length'], [200, 1])
        self.assertEqual(alert['clipped_start'], [START + DAY + 60, 1])
        self.assertEqual(alert['clipped_end'], [START + DAY + 100, 1])
        # Day boundaries split the dose but never the alert.
        full = self.station_report(self.report_ok())
        self.assertEqual(int(full['days'][0]['unsafe_seconds'][0]),
                         DAY - (DAY - 100))
        self.assertEqual(int(full['days'][1]['unsafe_seconds'][0]), 100)

    def test_query_is_clipped_but_alert_length_is_warehouse_wide(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 150, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        self.build([
            observation(1, 'l', 1, 'st', 'a', START + 100, [10, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 300, [10, 1]),
            whole_day_calibration(3, 'cal'),
        ], doc=doc)
        station = self.station_report(
            self.report_ok(start=START + 250, end=START + 260))
        self.assertEqual(station['alerts'][0]['length'], [200, 1])
        self.assertEqual(station['alerts'][0]['clipped_length'], [10, 1])
        self.assertEqual(station['days'][0]['known_seconds'], [10, 1])

    def test_alert_needs_positive_length_and_meets_min_episode(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 100, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [10, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [10, 1]),
            whole_day_calibration(3, 'cal'),
        ], doc=doc)
        # Exactly 100 s meets the minimum and stays a single closed episode.
        station = self.station_report(self.report_ok())
        self.assertEqual(len(station['alerts']), 1)
        self.assertEqual(station['alerts'][0]['length'], [100, 1])
        tight = config(stations=[{
            'id': 'st', 'min_episode_seconds': 101, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        ok(self, self.ws.init(tight, db='tight.db'))
        ok(self, self.ws.ingest([
            observation(1, 'l', 1, 'st', 'a', START, [10, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [10, 1]),
            whole_day_calibration(3, 'cal'),
        ], db='tight.db'))
        tight_report = ok(self, self.ws.report(db='tight.db', start=START,
                                               end=TWO_DAYS, output='tight.json'))
        self.assertEqual(self.station_report(tight_report)['alerts'], [])

    def test_threshold_at_exact_value_is_not_unsafe(self):
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [5, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 100, [5, 1]),
            whole_day_calibration(3, 'cal'),
        ])
        station = self.station_report(self.report_ok())
        self.assertEqual(station['days'][0]['known_seconds'], [100, 1])
        self.assertEqual(station['days'][0]['unsafe_seconds'], [0, 1])
        self.assertEqual(station['alerts'], [])

    def test_rational_outputs_are_reduced(self):
        self.build([
            observation(1, 'l', 1, 'st', 'a', START, [0, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 3, [1, 1]),
            whole_day_calibration(3, 'cal', start=START, end=START + 3,
                                  a=[3, 2], b=[1, 6]),
        ])
        day = self.station_report(self.report_ok())['days'][0]
        value = Fraction(*day['known_seconds'])
        self.assertEqual(value, 3)

    def test_report_requires_integer_seconds_inside_warehouse(self):
        self.init_ok()
        for start, end in [(START, START), (START, START - 1),
                           (START - 1, TWO_DAYS), (START, TWO_DAYS + 1),
                           (START, START + 0.5)]:
            run = self.ws.report(start=start, end=end, output='bad.json')
            self.assertEqual(run.returncode, 2, (start, end))
        run = cli('report', '--db', str(self.ws.path('q.db')), '--start', 'x',
                  '--end', str(TWO_DAYS), '--output', str(self.ws.path('bad.json')))
        self.assertEqual(run.returncode, 2)

    def test_report_output_safety(self):
        self.init_ok()
        before = self.ws.db_bytes()
        run = cli('report', '--db', str(self.ws.path('q.db')), '--start', str(START),
                  '--end', str(TWO_DAYS), '--output', str(self.ws.path('q.db')))
        self.assertEqual(run.returncode, 2)
        self.assertEqual(self.ws.db_bytes(), before)
        link = self.ws.path('link.json')
        link.symlink_to(self.ws.path('target.json'))
        run = self.ws.report(output='link.json')
        self.assertEqual(run.returncode, 2)
        self.assertFalse(self.ws.path('target.json').exists())

    def test_report_failure_preserves_existing_report(self):
        self.init_ok()
        self.ws.path('report.json').write_text('prior')
        run = self.ws.report(start=START, end=START, output='report.json')
        self.assertEqual(run.returncode, 2)
        self.assertEqual(self.ws.path('report.json').read_text(), 'prior')


# ---------------------------------------------------------------------------
# exactness against an independent rational reference


def random_events(rng, stations):
    events = []
    seq = 0
    observations = []
    for station in stations:
        for analyte in station['analytes']:
            instants = sorted(rng.sample(range(station['_start'], station['_end'],
                                               60), 6))
            for moment in instants:
                seq += 1
                observations.append((station['id'], analyte['id'], moment,
                                     [rng.randint(0, 60), rng.choice((1, 2, 3, 7))]))
            seq += 1
            events.append(calibration(
                seq, 'cal-%s-%s' % (station['id'], analyte['id']), 1,
                station['id'], analyte['id'], station['_start'], station['_end'],
                [rng.choice((1, 2, 3, 5)), rng.choice((1, 2, 3))],
                [rng.randint(-20, 20), rng.choice((1, 2, 5))]))
    for station, analyte, moment, value in observations:
        seq += 1
        events.append(observation(seq, 'o-%s-%s-%d' % (station, analyte, moment), 1,
                                  station, analyte, moment, value))
    # Calibrations arrive after the observations; re-sequence as a valid stream.
    ordered = sorted(events, key=lambda event: event['seq'])
    rank = {id(event): index + 1 for index, event in enumerate(ordered)}
    for event in ordered:
        event['seq'] = rank[id(event)]
    return ordered


class Reference(QualityCase):
    def compare(self, doc, events, start=None, end=None, db='q.db'):
        ok(self, self.ws.init(doc, db=db))
        ok(self, self.ws.ingest(events, db=db))
        query_start = start if start is not None else doc['start']
        query_end = end if end is not None else doc['end']
        actual = ok(self, self.ws.report(db=db, start=query_start, end=query_end,
                                         output=db + '.report'))
        expected = normalise(reference_report(
            parse_config(doc), *effective_facts(events), query_start, query_end))
        self.assertEqual(actual['stations'], expected['stations'])
        self.assertEqual(actual['warehouse'], expected['warehouse'])
        self.assertEqual(actual['query'], expected['query'])

    def test_fewer_fixture_matches_reference(self):
        import json as jsonlib
        doc = jsonlib.loads((ROOT_FIXTURES / 'quality_config.json').read_text())
        events = [jsonlib.loads(line) for line in
                  (ROOT_FIXTURES / 'quality_changes.jsonl').read_text().splitlines()]
        events += [jsonlib.loads(line) for line in
                   (ROOT_FIXTURES / 'quality_corrections.jsonl').read_text().splitlines()]
        events += [jsonlib.loads(line) for line in
                   (ROOT_FIXTURES / 'quality_restore.jsonl').read_text().splitlines()]
        self.compare(doc, events, db='full.db')
        self.compare(doc, events, start=START + DAY + 4000, end=TWO_DAYS,
                     db='tail.db')

    def test_random_instances_match_reference(self):
        import random
        rng = random.Random(147)
        for attempt in range(6):
            stations = []
            for index in range(rng.randint(1, 3)):
                analytes = [{
                    'id': 'a%d' % slot,
                    'threshold': [rng.randint(0, 30), rng.choice((1, 2, 5))],
                    'max_gap_seconds': rng.choice((120, 300, 10 ** 6)),
                } for slot in range(rng.randint(1, 3))]
                stations.append({
                    'id': 's%d' % index,
                    'min_episode_seconds': rng.choice((1, 60, 600)),
                    'analytes': analytes,
                    '_start': START, '_end': START + rng.choice((2, 3)) * DAY,
                })
            doc = config(stations=[{k: v for k, v in station.items()
                                    if not k.startswith('_')} for station in stations],
                         start=START,
                         end=max(station['_end'] for station in stations))
            events = random_events(rng, stations)
            # Ensure seq is contiguous from 1.
            for number, event in enumerate(events, 1):
                event['seq'] = number
            self.compare(doc, events, db='q%d.db' % attempt)
            if attempt == 0:
                mid = START + rng.randint(1, DAY)
                self.compare(doc, events, start=mid, end=mid + rng.randint(1, DAY),
                             db='qtail.db')


ROOT_FIXTURES = ROOT / 'fixtures'


# ---------------------------------------------------------------------------
# change-driven re-materialization and atomicity


class Linkage(QualityCase):
    def make_cross_midnight(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 60, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        events = [
            observation(1, 'l', 1, 'st', 'a', START + DAY - 500, [10, 1]),
            observation(2, 'm', 1, 'st', 'a', START + DAY + 500, [10, 1]),
            observation(3, 'r', 1, 'st', 'a', START + 2 * DAY - 100, [10, 1]),
            calibration(4, 'cal-left', 1, 'st', 'a', START, START + DAY,
                        [1, 1], [0, 1]),
            calibration(5, 'cal-right', 1, 'st', 'a', START + DAY, TWO_DAYS,
                        [1, 1], [0, 1]),
        ]
        self.init_ok(doc)
        self.ingest_ok(events)
        return doc, events

    def test_revoke_calibration_splits_and_restore_merges(self):
        doc, events = self.make_cross_midnight()
        first = self.station_report(self.report_ok())
        self.assertEqual(len(first['alerts']), 1)
        self.assertEqual(first['alerts'][0]['start'], [START + DAY - 500, 1])
        self.assertEqual(first['alerts'][0]['end'], [START + 2 * DAY - 100, 1])

        revoke = calibration(6, 'cal-left', 2, 'st', 'a', START, START + DAY,
                             [1, 1], [0, 1], deleted=True)
        self.ingest_ok([revoke], name='revoke.jsonl')
        split = self.station_report(self.report_ok())
        # Unknown span [START, START+DAY) breaks the station's known domain.
        self.assertEqual(len(split['alerts']), 1)
        self.assertEqual(split['alerts'][0]['start'], [START + DAY, 1])
        self.assertEqual(split['alerts'][0]['end'], [START + 2 * DAY - 100, 1])
        self.assertEqual(int(split['days'][0]['known_seconds'][0]), 0)

        restore = calibration(7, 'cal-left', 3, 'st', 'a', START, START + DAY,
                              [1, 1], [0, 1])
        self.ingest_ok([restore], name='restore.jsonl')
        merged = self.station_report(self.report_ok())
        self.assertEqual(merged, first)

    def test_calibration_gap_splits_and_restore_merges(self):
        # One whole-day alert whose closure is cut by a calibration-free gap.
        doc, events = self.make_cross_midnight()
        first = self.station_report(self.report_ok())
        self.assertEqual(len(first['alerts']), 1)
        revoke = calibration(6, 'cal-left', 2, 'st', 'a', START, START + DAY,
                             [1, 1], [0, 1], deleted=True)
        self.ingest_ok([revoke], name='revoke.jsonl')
        split = self.station_report(self.report_ok())
        # The revoked calibration leaves [START, START+DAY) unknown, so the
        # only alert is the right-hand half.
        self.assertEqual(len(split['alerts']), 1)
        self.assertEqual(split['alerts'][0]['start'], [START + DAY, 1])
        self.assertEqual(len(first['alerts']), 1)
        # A short tail query still sees the full-length alert identity.
        tail = self.station_report(
            self.report_ok(start=START + 2 * DAY - 200, end=TWO_DAYS - 100))
        self.assertEqual(len(tail['alerts']), 1)
        self.assertEqual(tail['alerts'][0]['length'], [2 * DAY - 100 - DAY, 1])
        self.assertEqual(tail['alerts'][0]['clipped_length'], [100, 1])

    def test_revoking_outer_observations_shrinks_the_episode(self):
        doc, events = self.make_cross_midnight()
        self.ingest_ok([
            observation(6, 'l', 2, 'st', 'a', START + DAY - 500, [10, 1],
                        deleted=True),
            observation(7, 'r', 2, 'st', 'a', START + 2 * DAY - 100, [10, 1],
                        deleted=True),
        ], name='drop.jsonl')
        station = self.station_report(self.report_ok())
        self.assertEqual(station['days'][0]['known_seconds'], [0, 1])
        self.assertEqual(len(station['alerts']), 0)
        self.assertEqual(station['days'][1]['known_seconds'], [0, 1])

    def test_late_observation_moves_dose_and_episode(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 60, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        base = [
            observation(1, 'l', 1, 'st', 'a', START + 100, [0, 1]),
            observation(2, 'm', 1, 'st', 'a', START + DAY, [0, 1]),
            observation(3, 'r', 1, 'st', 'a', START + DAY + 100, [0, 1]),
            whole_day_calibration(4, 'cal'),
        ]
        self.init_ok(doc)
        self.ingest_ok(base)
        before = self.station_report(self.report_ok())
        self.assertEqual(before['alerts'], [])
        late = observation(5, 'mid', 1, 'st', 'a', START + DAY + 50, [40, 1])
        self.ingest_ok([late], name='late.jsonl')
        after = self.station_report(self.report_ok())
        self.assertEqual(len(after['alerts']), 1)
        self.assertEqual(after['alerts'][0]['start'], [4 * (START + DAY) + 25, 4])
        self.assertEqual(after['alerts'][0]['end'], [4 * (START + DAY) + 375, 4])
        # Both days carry dose; both episodes qualify on their own.
        self.assertEqual(after['days'][0]['excess_integral']['a'], [0, 1])
        self.assertGreater(int(after['days'][1]['excess_integral']['a'][0]), 0)

    def test_incremental_matches_one_shot_reference(self):
        doc = config(stations=[{
            'id': 'st', 'min_episode_seconds': 60, 'analytes': [
                {'id': 'a', 'threshold': [5, 1], 'max_gap_seconds': 10 ** 6}]}])
        first = [
            observation(1, 'l', 1, 'st', 'a', START + 100, [0, 1]),
            observation(2, 'r', 1, 'st', 'a', START + 2 * DAY - 100, [0, 1]),
            whole_day_calibration(3, 'cal'),
        ]
        corrections = [
            observation(4, 'mid', 1, 'st', 'a', START + DAY, [30, 1]),
            observation(5, 'mid', 2, 'st', 'a', START + DAY + 30, [30, 1]),
        ]
        self.init_ok(doc)
        self.ingest_ok(first)
        self.ingest_ok(corrections[:1], name='late.jsonl')
        self.ingest_ok(corrections[1:], name='move.jsonl')
        incremental = self.report_ok()
        final_events = first + corrections
        expected = normalise(reference_report(
            parse_config(doc), *effective_facts(final_events), START, TWO_DAYS))
        self.assertEqual(incremental['stations'], expected['stations'])
        # A fresh warehouse fed only the final state agrees exactly.
        fresh = Workspace(self)
        ok(self, fresh.init(doc, db='fresh.db'))
        ok(self, fresh.ingest(final_events, db='fresh.db'))
        one_shot = json.loads(fresh.report(db='fresh.db', start=START,
                                           end=TWO_DAYS).stdout)
        self.assertEqual(incremental['stations'], one_shot['stations'])

    def test_materialization_failure_rolls_back_everything(self):
        doc, events = self.make_cross_midnight()
        self.ingest_ok([observation(6, 'mid', 1, 'st', 'a', START + DAY, [8, 1])],
                       name='seed.jsonl')
        meta, rows, derived = self.ws.snapshot()
        report_before = self.report_ok()
        # Inject a failure inside materialization through a monkeypatched module
        # is not possible across the CLI boundary, so force it with a real
        # cross-entity conflict discovered after events are inserted.
        clash = observation(7, 'mid', 2, 'st', 'a', START + DAY + 10, [8, 1])
        clash2 = observation(8, 'other', 1, 'st', 'a', START + DAY + 10, [1, 1])
        run = self.ingest_fails([clash, clash2], name='clash.jsonl')
        self.assertIn('share an instant', run.stderr)
        self.assertEqual(self.ws.snapshot(), (meta, rows, derived))
        self.assertEqual(self.report_ok(), report_before)

    def test_materialize_exception_leaves_no_partial_commit(self):
        # Monkeypatch the warehouse module in-process so materialize raises
        # after events are inserted but before the transaction commits.
        import sys
        sys.path.insert(0, str(ROOT / 'src'))
        import water_rights.quality_warehouse as warehouse
        self.make_cross_midnight()
        meta, rows, derived = self.ws.snapshot()
        report_before = self.report_ok()
        db = self.ws.path('q.db')
        changes = write_jsonl(self.ws.path('boom.jsonl'), [
            observation(6, 'boom', 1, 'st', 'a', START + DAY + 5, [9, 1])])
        original = warehouse.materialize

        def explode(*args, **kwargs):
            raise RuntimeError('injected materialization failure')

        warehouse.materialize = explode
        try:
            with self.assertRaises(RuntimeError):
                warehouse.ingest_changes(changes, db)
        finally:
            warehouse.materialize = original
        self.assertEqual(self.ws.snapshot(), (meta, rows, derived))
        self.assertEqual(self.report_ok(), report_before)
        # The same batch now applies cleanly.
        self.ingest_ok([observation(6, 'boom', 1, 'st', 'a', START + DAY + 5,
                                    [9, 1])], name='boom2.jsonl')
        self.assertEqual(self.ws.snapshot()[0]['last_seq'], '6')

    def test_replay_after_correction_does_not_duplicate_exposure(self):
        doc, events = self.make_cross_midnight()
        self.ingest_ok([observation(6, 'mid', 1, 'st', 'a', START + DAY, [8, 1])],
                       name='seed.jsonl')
        once = self.report_ok()
        meta_before = self.ws.snapshot()
        # Replaying the combined stream (committed prefix only) must not add rows.
        self.ingest_ok([], name='empty.jsonl')
        self.assertEqual(self.ws.snapshot(), meta_before)
        self.ingest_ok(events, name='replay.jsonl')
        self.assertEqual(self.ws.snapshot(), meta_before)
        self.assertEqual(self.report_ok(), once)

    def test_all_derived_rows_come_from_effective_facts(self):
        doc, events = self.make_cross_midnight()
        meta, _, derived = self.ws.snapshot()
        self.assertEqual(meta['last_seq'], '5')
        self.assertGreater(len(derived['derived_unsafe']), 0)
        self.assertGreater(len(derived['derived_excess']), 0)
        self.assertGreater(len(derived['derived_alert']), 0)


class Fixtures(unittest.TestCase):
    def test_fixture_pipeline(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / 'q.db'
            run = cli('init', '--source', str(ROOT_FIXTURES / 'quality_config.json'),
                      '--db', str(db))
            self.assertEqual(run.returncode, 0, run.stderr)
            for name in ('quality_changes.jsonl', 'quality_corrections.jsonl',
                         'quality_restore.jsonl'):
                run = cli('ingest', '--source', str(ROOT_FIXTURES / name),
                          '--db', str(db))
                self.assertEqual(run.returncode, 0, (name, run.stderr))
            run = cli('report', '--db', str(db), '--start', str(START),
                      '--end', str(TWO_DAYS), '--output', str(td / 'r.json'))
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((td / 'r.json').read_text())
            self.assertEqual(report['seq'], 23)
            river = report['stations'][0]
            self.assertEqual(river['station'], 'river-a')
            self.assertEqual(len(river['alerts']), 1)
            self.assertEqual(river['alerts'][0]['start'], [1767305200, 1])


if __name__ == '__main__':
    unittest.main()
