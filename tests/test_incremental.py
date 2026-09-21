import copy
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from weread2notion import cli
from weread2notion.incremental import (
    MARKER, NotionStore, managed_block, read_state, source_records, text,
    reconcile, fingerprint, ensure_area, find_area, property_changes,
)


def note(value='one'):
    return {'object': 'block', 'type': 'callout', 'callout': {
        'rich_text': [text(value)], 'icon': {'type': 'emoji', 'emoji': '〰️'}}}


class FakeNotion:
    """Stateful API fake: persistent IDs, comments, and server-side write timeouts."""
    def __init__(self):
        self.items = {}
        self.children = {'area': [], 'book': []}
        self.writes = []
        self.sequence = 0
        self.timeout_after_append = False
        self.timeout_after_update = False
        self.fail_append_number = None
        self.appends = 0

    def add(self, parent, block):
        self.sequence += 1
        value = copy.deepcopy(block)
        value['id'] = str(self.sequence)
        self.items[value['id']] = value
        self.children.setdefault(parent, []).append(value['id'])
        return copy.deepcopy(value)

    def request(self, path, method='GET', body=None, query=None):
        parts = path.split('/')
        if method == 'GET':
            if path.endswith('/children'):
                ids = self.children.get(parts[1], [])
                start = int((query or {}).get('start_cursor', 0))
                batch = ids[start:start + 2]  # force pagination
                next_start = start + len(batch)
                return {'results': [copy.deepcopy(self.items[i]) for i in batch],
                        'has_more': next_start < len(ids),
                        'next_cursor': str(next_start) if next_start < len(ids) else None}
            return copy.deepcopy(self.items[parts[1]])
        self.writes.append((path, method, copy.deepcopy(body)))
        if path == 'pages':
            parent = body['parent']['page_id']
            page = self.add(parent, {'type': 'child_page', 'child_page': {'title': 'managed'}})
            self.children[page['id']] = []
            for b in body.get('children', []):
                self.add(page['id'], b)
            return page
        if path.endswith('/children'):
            self.appends += 1
            if self.appends == self.fail_append_number:
                raise RuntimeError('connection unavailable before commit')
            values = [self.add(parts[1], b) for b in body['children']]
            if self.timeout_after_append:
                self.timeout_after_append = False
                raise RuntimeError('response lost after commit')
            return {'results': values}
        self.items[parts[1]].update(copy.deepcopy(body))
        if self.timeout_after_update:
            self.timeout_after_update = False
            raise RuntimeError('update response lost')
        return copy.deepcopy(self.items[parts[1]])


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeNotion()
        self.store = NotionStore(self.api)

    def test_no_change_has_zero_writes(self):
        reconcile(self.store, 'area', [('bookmark:1', note())])
        self.api.writes.clear()
        stats = reconcile(self.store, 'area', [('bookmark:1', note())])
        self.assertEqual(stats['unchanged'], 1)
        self.assertEqual(self.api.writes, [])

    def test_one_changed_note_preserves_ids_comments_and_unknown_blocks(self):
        old = self.api.add('area', managed_block('bookmark:1', note()))
        self.api.items[old['id']]['comments'] = ['11111']
        personal = self.api.add('area', note('personal'))
        reconcile(self.store, 'area', [('bookmark:1', note('new'))])
        self.assertEqual(self.api.items[old['id']]['comments'], ['11111'])
        self.assertEqual(self.api.items[personal['id']], personal)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.writes[0][0], 'blocks/' + old['id'])

    def test_user_edit_is_kept_when_source_unchanged(self):
        old = self.api.add('area', managed_block('bookmark:1', note()))
        self.api.items[old['id']]['callout']['rich_text'][0]['text']['content'] = 'my edit'
        stats = reconcile(self.store, 'area', [('bookmark:1', note())])
        self.assertEqual(stats['local_edits'], 1)
        self.assertEqual(self.api.writes, [])

    def test_conflict_copy_is_deduplicated(self):
        old = self.api.add('area', managed_block('bookmark:1', note()))
        self.api.items[old['id']]['callout']['rich_text'][0]['text']['content'] = 'mine'
        stats = reconcile(self.store, 'area', [('bookmark:1', note('theirs'))])
        self.assertEqual(stats['conflicts'], 1)
        self.assertEqual(len(self.api.children['area']), 2)
        self.api.writes.clear()
        reconcile(self.store, 'area', [('bookmark:1', note('theirs'))])
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.items[old['id']]['callout']['rich_text'][0]['text']['content'], 'mine')

    def test_deleted_source_is_marked_not_removed(self):
        old = self.api.add('area', managed_block('bookmark:1', note()))
        reconcile(self.store, 'area', [], ['bookmark:1'])
        current = self.api.items[old['id']]
        self.assertTrue(read_state(current)['deleted'])
        self.assertEqual(fingerprint(current), fingerprint(old))
        self.api.writes.clear()
        reconcile(self.store, 'area', [], ['bookmark:1'])
        self.assertEqual(self.api.writes, [])

    def test_absent_source_does_not_mean_deleted(self):
        self.api.add('area', managed_block('bookmark:1', note()))
        reconcile(self.store, 'area', [])
        self.assertEqual(self.api.writes, [])

    def test_append_timeout_recovers_without_duplicate(self):
        self.api.timeout_after_append = True
        reconcile(self.store, 'area', [('bookmark:1', note())])
        self.assertEqual(self.api.appends, 1)
        self.assertEqual(len(self.api.children['area']), 1)

    def test_update_timeout_next_run_does_not_repeat_write(self):
        self.api.add('area', managed_block('bookmark:1', note()))
        self.api.timeout_after_update = True
        with self.assertRaises(RuntimeError):
            reconcile(self.store, 'area', [('bookmark:1', note('new'))])
        self.api.writes.clear()
        reconcile(self.store, 'area', [('bookmark:1', note('new'))])
        self.assertEqual(self.api.writes, [])

    def test_partial_run_resumes_only_missing_records(self):
        self.api.fail_append_number = 2
        records = [('bookmark:1', note()), ('bookmark:2', note('two'))]
        with self.assertRaises(RuntimeError):
            reconcile(self.store, 'area', records)
        self.api.writes.clear()
        reconcile(self.store, 'area', records)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(len(self.api.children['area']), 2)

    def test_dry_run_existing_and_new_area_never_writes(self):
        self.api.add('area', managed_block('bookmark:1', note()))
        store = NotionStore(self.api, dry_run=True)
        self.assertEqual(reconcile(store, 'area', [('bookmark:1', note('new'))])['updated'], 1)
        self.assertEqual(reconcile(store, None, [('bookmark:2', note())])['added'], 1)
        self.assertEqual(self.api.writes, [])

    def test_persistent_area_is_reused_even_after_rename(self):
        area = ensure_area(self.store, 'book', '123')
        self.api.items[area[0]]['child_page']['title'] = 'renamed by user'
        self.api.writes.clear()
        found = find_area(self.store, 'book', '123')
        self.assertEqual(found[0], area[0])
        self.assertEqual(self.api.writes, [])

    def test_legacy_blocks_are_not_adopted(self):
        legacy = self.api.add('book', note('old personal annotated text'))
        area = ensure_area(self.store, 'book', '123')
        reconcile(self.store, area[0], [('bookmark:1', note())])
        self.assertEqual(self.api.items[legacy['id']], legacy)

    def test_marker_duplicates_fail_closed(self):
        self.api.add('area', managed_block('bookmark:1', note()))
        self.api.add('area', managed_block('bookmark:1', note()))
        with self.assertRaises(RuntimeError):
            reconcile(self.store, 'area', [('bookmark:1', note('new'))])
        self.assertEqual(self.api.writes, [])

    def test_source_deep_links_and_fallback(self):
        records = source_records('123', [
            {'bookmarkId': 'a', 'markText': 'one', 'chapterUid': 1,
             'deepLink': 'weread://bestbookmark?bookId=123&rangeStart=1&rangeEnd=2'},
            {'bookmarkId': 'b', 'markText': 'two', 'chapterUid': 1},
            {'bookmarkId': 'c', 'markText': 'three'},
        ], [], [{'chapterUid': 1, 'title': 'chapter', 'deepLink': 'weread://reading?bId=123&chapterUid=1'}],
            'https://weread.qq.com/web/reader/book')
        notes = [b for k, b in records if k.startswith('bookmark:')]
        self.assertIn('在微信读书中查看', notes[0]['callout']['rich_text'][-1]['text']['content'])
        self.assertIn('查看所在章节', notes[1]['callout']['rich_text'][-1]['text']['content'])
        self.assertIn('打开书籍', notes[2]['callout']['rich_text'][-1]['text']['content'])

    def test_missing_source_identity_fails(self):
        with self.assertRaises(ValueError):
            source_records('123', [{'markText': 'no identity'}], [], [], 'url')

    def test_property_diff_ignores_server_metadata(self):
        existing = {'Author': {'id': 'a', 'type': 'rich_text', 'rich_text': [
            {'type': 'text', 'text': {'content': 'Alice', 'link': None}, 'plain_text': 'Alice'}]},
                    'Status': {'type': 'select', 'select': {'name': '在读', 'id': 's', 'color': 'blue'}}}
        desired = {'Author': {'rich_text': [text('Alice')]}, 'Status': {'select': {'name': '在读'}}}
        self.assertEqual(property_changes(existing, desired), {})


class OrchestrationTests(unittest.TestCase):
    def run_sync(self, pages, properties_only=False, dry_run=False, fail_notes=False, current=False):
        with ExitStack() as stack:
            stubs = {
                'validate_secret_inputs': {'notion_token': 'token', 'weread_api_key': 'key'},
                'extract_notion_id': 'id', 'resolve_data_source_id': 'source',
                'load_data_source_schema': None, 'load_page_index': pages,
                'get_notebooklist': [{'sort': 10, 'bookId': '123', 'title': 'Book'}],
                'get_bookinfo': ('isbn', 8), 'has_any_property': False,
                'book_properties': {'Sort': {'number': 10}, 'Progress': {'number': 50}},
                'find_area': ('area', {'id': 'root', 'type': 'paragraph', 'paragraph': {'rich_text': [text('root')]}},
                              {'v': 2, 'key': 'root:123', 'cursor': None, 'audited': 0}),
                'get_note_records': ([('bookmark:1', note())], []),
            }
            if current:
                import time
                stubs['find_area'][2].update(cursor=cli.notebook_cursor(stubs['get_notebooklist'][0]), audited=int(time.time()))
            mocks = {k: stack.enter_context(patch.object(cli, k, return_value=v)) for k, v in stubs.items()}
            store = stack.enter_context(patch.object(cli, 'NotionStore')).return_value
            stack.enter_context(patch.object(cli, 'Client'))
            stack.enter_context(patch.object(cli, 'WeReadGatewayClient'))
            stack.enter_context(patch.object(cli, 'ensure_area', side_effect=lambda *a: stubs['find_area']))
            reconciler = stack.enter_context(patch.object(cli, 'reconcile', return_value={'conflicts': 0}))
            if fail_notes:
                reconciler.side_effect = RuntimeError('partial failure')
            if fail_notes:
                with self.assertRaises(RuntimeError):
                    cli.sync(properties_only, dry_run)
            else:
                cli.sync(properties_only, dry_run)
            return store, reconciler, mocks

    def test_properties_only_does_not_touch_notes_or_cursor(self):
        store, reconciler, mocks = self.run_sync({'123': [{'id': 'page', 'properties': {}}]}, True)
        reconciler.assert_not_called()
        mocks['find_area'].assert_not_called()
        self.assertEqual(store.request.call_args.args[2], {'properties': {'Progress': {'number': 50}}})

    def test_full_dry_run_does_not_create_or_update(self):
        store, reconciler, mocks = self.run_sync({}, dry_run=True)
        store.request.assert_not_called()
        store.update.assert_not_called()

    def test_failure_does_not_commit_cursor(self):
        store, reconciler, mocks = self.run_sync({'123': [{'id': 'page', 'properties': {}}]}, fail_notes=True)
        store.update.assert_not_called()
        for call in store.request.call_args_list:
            self.assertNotIn('Sort', call.args[2].get('properties', {}))

    def test_equal_properties_do_not_write(self):
        store, reconciler, mocks = self.run_sync({'123': [{'id': 'page', 'properties': {
            'Progress': {'type': 'number', 'number': 50}}}]}, properties_only=True)
        store.request.assert_not_called()
    def test_normal_unchanged_run_skips_source_notes_and_all_writes(self):
        store, reconciler, mocks = self.run_sync({'123': [{'id': 'page', 'properties': {
            'Progress': {'type': 'number', 'number': 50}}}]}, current=True)
        store.request.assert_not_called()
        store.update.assert_not_called()
        reconciler.assert_not_called()
        mocks['get_note_records'].assert_not_called()


class BoundaryTests(unittest.TestCase):
    def test_progress_one_means_one_percent(self):
        self.assertEqual(cli.normalize_reading_progress(1), .01)
        self.assertEqual(cli.normalize_reading_progress(100), 1)

    def test_hierarchy_includes_parent_without_direct_notes(self):
        records = source_records('123', [{'bookmarkId': 'a', 'chapterUid': 2, 'markText': 'x'}], [], [
            {'chapterUid': 1, 'chapterIdx': 1, 'level': 1, 'title': 'Part'},
            {'chapterUid': 2, 'chapterIdx': 2, 'level': 2, 'title': 'Chapter'},
        ], 'https://weread.qq.com/web/reader/test')
        self.assertEqual([k for k, _ in records], ['chapter:1', 'chapter:2', 'bookmark:a'])
        self.assertEqual(records[0][1]['type'], 'heading_1')
        self.assertEqual(records[1][1]['type'], 'heading_2')

    def test_api_rejects_duplicate_source_ids_before_writing(self):
        api = FakeNotion()
        with self.assertRaises(RuntimeError):
            reconcile(NotionStore(api), 'area', [('bookmark:a', note()), ('bookmark:a', note())])
        self.assertEqual(api.writes, [])

    def test_notebook_pagination_stall_raises(self):
        gateway = Mock()
        gateway.request.return_value = {'books': [{'bookId': '1', 'sort': 10}], 'hasMore': 1}
        with patch.object(cli, 'weread', gateway), self.assertRaises(RuntimeError):
            cli.get_notebooklist()
        self.assertEqual(gateway.request.call_count, 2)

    def test_reviews_pagination_stall_raises(self):
        gateway = Mock()
        gateway.request.side_effect = [
            {'updated': [], 'chapters': []},
            {'reviews': [], 'hasMore': 1, 'synckey': 0},
        ]
        with patch.object(cli, 'weread', gateway), patch.object(cli, 'get_chapter_info', return_value={}):
            with self.assertRaises(RuntimeError):
                cli.get_note_records('123')

    def test_missing_reading_response_cannot_erase_properties(self):
        gateway = Mock()
        gateway.request.return_value = {}
        with patch.object(cli, 'weread', gateway), self.assertRaises(RuntimeError):
            cli.get_read_info('123')

    def test_upgrade_required_is_not_retried(self):
        gateway = cli.WeReadGatewayClient('test-key')
        gateway.session = Mock()
        gateway.session.post.return_value.json.return_value = {'upgrade_info': {'message': 'upgrade'}}
        with self.assertRaises(cli.UpgradeRequired):
            gateway.request('/book/info', bookId='123')
        self.assertEqual(gateway.session.post.call_count, 1)

    def test_deleted_record_restores_without_recreating_block(self):
        api = FakeNotion()
        old = api.add('area', managed_block('bookmark:1', note()))
        store = NotionStore(api)
        reconcile(store, 'area', [], ['bookmark:1'])
        reconcile(store, 'area', [('bookmark:1', note())])
        self.assertFalse(read_state(api.items[old['id']]).get('deleted', False))
        self.assertEqual(len(api.children['area']), 1)

    def test_concurrent_local_edit_stops_write(self):
        api = FakeNotion()
        old = api.add('area', managed_block('bookmark:1', note()))
        api.items[old['id']]['callout']['rich_text'][0]['text']['content'] = 'edited after listing'
        with self.assertRaises(RuntimeError):
            NotionStore(api).update(old, managed_block('bookmark:1', note('source')))
        self.assertEqual(api.writes, [])

    def test_sdk_uses_position_and_no_automatic_write_retry(self):
        import httpx
        from notion_client import Client
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(503, json={'object': 'error', 'code': 'service_unavailable', 'message': 'retry later'})
        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            sdk = Client(client=http_client, auth='fake', notion_version='2026-03-11', retry=False)
            with self.assertRaises(Exception):
                NotionStore(sdk).request('blocks/test/children', 'PATCH', {'children': [note()]})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers['Notion-Version'], '2026-03-11')

    def test_heading_level_change_preserves_original_block(self):
        api = FakeNotion()
        old = {'type': 'heading_1', 'heading_1': {'rich_text': [text('Chapter')]}}
        block = api.add('area', managed_block('chapter:1', old))
        new = {'type': 'heading_2', 'heading_2': {'rich_text': [text('Chapter')]}}
        result = reconcile(NotionStore(api), 'area', [('chapter:1', new)])
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(api.items[block['id']]['type'], 'heading_1')

    def test_no_rating_response_preserves_previous_value(self):
        gateway = Mock()
        gateway.request.return_value = {'bookId': '123'}
        with patch.object(cli, 'weread', gateway):
            self.assertEqual(cli.get_bookinfo('123'), (None, None))


if __name__ == '__main__':
    unittest.main()
