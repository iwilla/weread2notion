import unittest
from datetime import datetime
from unittest.mock import Mock

from weread2notion.reading_stats import SCHEMA, SHANGHAI, daily_records, sync_daily_reading


class DailyReadingTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime(2026, 10, 8, tzinfo=SHANGHAI)
        self.response = {'readTimes': {str(int(self.day.timestamp())): 2059}}

    def test_seconds_and_shanghai_day_are_preserved(self):
        props = daily_records(self.response, '2026-10')['daily:2026-10-08']
        self.assertEqual(props['ReadingSeconds']['number'], 2059)
        self.assertEqual(props['ReadingMinutes']['number'], 34.32)
        self.assertEqual(props['Date']['date']['start'], '2026-10-08')

    def test_missing_days_are_not_invented(self):
        self.assertEqual(daily_records({'readTimes': {}}, '2026-10'), {})

    def test_invalid_data_and_wrong_month_fail_before_writes(self):
        for response in ({}, {'readTimes': []}, {'readTimes': {'bad': 5}},
                         {'readTimes': {str(int(self.day.timestamp())): -1}},
                         {'readTimes': {str(int(self.day.timestamp())): 'nan'}}):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                daily_records(response, '2026-10')
        with self.assertRaises(RuntimeError):
            daily_records(self.response, '2026-09')

    def run_sync(self, pages=(), dry_run=False, fail_create=False):
        gateway, store = Mock(), Mock()
        gateway.request.side_effect = [{'readTimes': {}}, self.response]
        store.dry_run = dry_run
        schema = {'properties': {name: {'type': kind} for name, kind in SCHEMA.items()}}
        def request(path, method='GET', body=None):
            if path == 'data_sources/source':
                return schema
            if path.endswith('/query'):
                return {'results': list(pages), 'has_more': False}
            if fail_create and path == 'pages':
                raise RuntimeError('timeout')
            return {'id': 'new', 'properties': body['properties']}
        store.request.side_effect = request
        totals = sync_daily_reading(gateway, store, 'source', self.day)
        return totals, gateway, store

    def test_dry_run_does_not_write(self):
        totals, gateway, store = self.run_sync(dry_run=True)
        self.assertEqual(totals['daily_added'], 1)
        self.assertEqual(gateway.request.call_count, 2)
        self.assertTrue(all(c.args[0].startswith('data_sources/') for c in store.request.call_args_list))

    def test_equal_row_does_not_write(self):
        props = daily_records(self.response, '2026-10')['daily:2026-10-08']
        totals, _, store = self.run_sync([{'id': 'old', 'properties': props}])
        self.assertEqual(totals['daily_unchanged'], 1)
        self.assertFalse(any(c.args[0].startswith('pages') for c in store.request.call_args_list))

    def test_changed_duration_updates_existing_row(self):
        props = daily_records(self.response, '2026-10')['daily:2026-10-08']
        props['ReadingSeconds']['number'] = 100
        totals, _, store = self.run_sync([{'id': 'old', 'properties': props}])
        self.assertEqual(totals['daily_updated'], 1)
        self.assertTrue(any(c.args[0] == 'pages/old' for c in store.request.call_args_list))

    def test_duplicate_identity_stops_processing(self):
        props = daily_records(self.response, '2026-10')['daily:2026-10-08']
        with self.assertRaises(RuntimeError):
            self.run_sync([{'id': 'a', 'properties': props}, {'id': 'b', 'properties': props}])

    def test_create_timeout_recovers_by_identity_without_second_create(self):
        gateway, store = Mock(), Mock()
        gateway.request.side_effect = [{'readTimes': {}}, self.response]
        store.dry_run = False
        props = daily_records(self.response, '2026-10')['daily:2026-10-08']
        schema = {'properties': {name: {'type': kind} for name, kind in SCHEMA.items()}}
        store.request.side_effect = [schema, {'results': [], 'has_more': False},
                                     RuntimeError('timeout'),
                                     {'results': [{'id': 'created', 'properties': props}], 'has_more': False}]
        totals = sync_daily_reading(gateway, store, 'source', self.day)
        self.assertEqual(totals['daily_added'], 1)
        self.assertEqual(sum(c.args[0] == 'pages' for c in store.request.call_args_list), 1)

    def test_missing_stats_schema_does_not_fetch_api_or_write(self):
        gateway, store = Mock(), Mock()
        store.request.return_value = {'properties': {}}
        with self.assertRaises(RuntimeError):
            sync_daily_reading(gateway, store, 'source', self.day)
        gateway.request.assert_not_called()
        store.request.assert_called_once_with('data_sources/source')


if __name__ == '__main__':
    unittest.main()
