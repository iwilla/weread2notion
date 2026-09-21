import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack
from weread2notion import cli


class SyncTests(unittest.TestCase):
    def setUp(self):
        cli.data_source_property_types = {
            'BookName': 'title', 'BookId': 'rich_text', 'Sort': 'number',
            'ReadingTime': 'rich_text', 'Progress': 'number', 'Status': 'select',
            'Date': 'date', 'Author': 'rich_text', 'Rating': 'number',
        }
        cli.data_source_property_configs = {}
        cli.title_property_name = 'BookName'
        cli.skipped_property_names = set()

    def test_english_schema_and_units(self):
        cli.data_source_property_configs = {'Rating': {'number': {'format': 'percent'}}}
        with patch.object(cli, 'get_read_info', return_value={
            'readingTime': 9000, 'readingProgress': .5, 'finishedDate': 0,
        }) as read:
            props = cli.book_properties('Book', '123', 10, 'Author', None, 8, None)
        read.assert_called_once()
        self.assertEqual(props['ReadingTime']['rich_text'][0]['text']['content'], '2时30分')
        self.assertEqual(props['Progress']['number'], 50)
        self.assertEqual(props['Rating']['number'], .8)
        self.assertNotIn('Date', props)

    def test_chinese_precedence_and_percent(self):
        cli.data_source_property_types['阅读时长'] = 'rich_text'
        cli.data_source_property_configs['Progress'] = {'number': {'format': 'percent'}}
        props = cli.build_notion_properties({'阅读时长': '0分', '阅读进度': .3})
        self.assertIn('阅读时长', props)
        self.assertNotIn('ReadingTime', props)
        self.assertEqual(props['Progress']['number'], .3)

    def test_missing_reading_time_is_not_erased(self):
        with patch.object(cli, 'get_read_info', return_value={'readingTime': None}):
            props = cli.book_properties('Book', '123', 10, 'A', None, None, None)
        self.assertNotIn('ReadingTime', props)

    def test_pagination(self):
        with patch.object(cli, 'query_data_source', side_effect=[
            {'results': [{'id': 'a'}], 'has_more': True, 'next_cursor': 'next'},
            {'results': [{'id': 'b'}], 'has_more': False},
        ]) as query:
            self.assertEqual(len(cli.find_book_pages('123')), 2)
            self.assertEqual(query.call_args.kwargs['start_cursor'], 'next')

    def run_sync(self, properties_only, pages):
        with ExitStack() as stack:
            mocks = {}
            values = {
                'validate_secret_inputs': {'notion_token': 'token', 'weread_api_key': 'key'},
                'extract_notion_id': 'id', 'resolve_data_source_id': 'source',
                'load_data_source_schema': None,
                'get_notebooklist': [{'sort': 10, 'bookId': '123', 'title': 'Book'}],
                'find_book_pages': pages, 'get_bookinfo': ('isbn', 8),
                'book_properties': {'Sort': {'number': 10}, 'Progress': {'number': 50}},
                'get_chapter_info': {}, 'get_bookmark_list': [],
                'get_review_list': ([], []), 'get_children': ([], {}),
                'add_children': [], 'insert_to_notion': 'new', 'check': None,
            }
            for name, value in values.items():
                mocks[name] = stack.enter_context(patch.object(cli, name, return_value=value))
            notion = stack.enter_context(patch.object(cli, 'Client')).return_value
            stack.enter_context(patch.object(cli, 'WeReadGatewayClient'))
            cli.sync(properties_only=properties_only)
            return notion, mocks

    def test_properties_only_preserves_notes_and_cursor(self):
        notion, mocks = self.run_sync(True, [{'id': 'existing'}])
        self.assertEqual(notion.pages.update.call_args.kwargs,
                         {'page_id': 'existing', 'properties': {'Progress': {'number': 50}}})
        mocks['check'].assert_not_called()
        mocks['insert_to_notion'].assert_not_called()
        mocks['get_bookmark_list'].assert_not_called()

    def test_unchanged_notes_still_refresh_reading(self):
        notion, mocks = self.run_sync(False, [{'id': 'existing', 'properties': {
            'Sort': {'type': 'number', 'number': 10}}}])
        notion.pages.update.assert_called_once()
        mocks['check'].assert_not_called()

    def test_properties_only_skips_missing_pages(self):
        notion, mocks = self.run_sync(True, [])
        notion.pages.update.assert_not_called()
        mocks['insert_to_notion'].assert_not_called()

    def test_missing_book_is_synced_regardless_of_other_books(self):
        notion, mocks = self.run_sync(False, [])
        mocks['insert_to_notion'].assert_called_once()

    def test_empty_blocks_do_not_send_empty_request(self):
        cli.client = Mock()
        self.assertEqual(cli.add_children('page', []), [])
        cli.client.blocks.children.append.assert_not_called()


if __name__ == '__main__':
    unittest.main()
