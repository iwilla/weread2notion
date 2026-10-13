import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack
from weread2notion import cli


class SyncTests(unittest.TestCase):
    def test_gateway_sends_supported_skill_version(self):
        gateway = cli.WeReadGatewayClient('test-key')
        response = Mock()
        response.json.return_value = {'errcode': 0, 'books': []}
        with patch.object(gateway.session, 'post', return_value=response) as post:
            self.assertEqual(gateway.request('/user/notebooks', count=100),
                             {'errcode': 0, 'books': []})
        post.assert_called_once_with(
            cli.WEREAD_GATEWAY_URL,
            json={'api_name': '/user/notebooks', 'skill_version': '1.0.5', 'count': 100},
            timeout=30,
        )

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

    def test_incomplete_progress_preserves_reading_properties(self):
        gateway = Mock()
        for response in ({}, {'book': None}, {'book': {}},
                         {'book': {'progress': 'invalid', 'finishTime': 123}}):
            with self.subTest(response=response):
                gateway.request.return_value = response
                with patch.object(cli, 'weread', gateway):
                    props = cli.book_properties('Book', '123', 10, 'A', None, None, None)
                for name in ('ReadingTime', 'Progress', 'Status', 'Date'):
                    self.assertNotIn(name, props)
                self.assertIn('BookName', props)

    def test_zero_progress_is_valid(self):
        gateway = Mock()
        gateway.request.return_value = {'book': {'progress': 0, 'recordReadingTime': 0}}
        with patch.object(cli, 'weread', gateway):
            info = cli.get_read_info('123')
        self.assertEqual(info['readingProgress'], 0)
        self.assertEqual(info['readingTime'], 0)

    def test_reading_request_errors_still_fail(self):
        gateway = Mock()
        for error in (RuntimeError('request failed'), cli.UpgradeRequired('upgrade')):
            with self.subTest(error=error):
                gateway.request.side_effect = error
                with patch.object(cli, 'weread', gateway), self.assertRaises(type(error)):
                    cli.book_properties('Book', '123', 10, 'A', None, None, None)

    def test_pagination(self):
        with patch.object(cli, 'query_data_source', side_effect=[
            {'results': [{'id': 'a'}], 'has_more': True, 'next_cursor': 'next'},
            {'results': [{'id': 'b'}], 'has_more': False},
        ]) as query:
            self.assertEqual(len(cli.find_book_pages('123')), 2)
            self.assertEqual(query.call_args.kwargs['start_cursor'], 'next')

    def test_empty_blocks_do_not_send_empty_request(self):
        cli.client = Mock()
        self.assertEqual(cli.add_children('page', []), [])
        cli.client.blocks.children.append.assert_not_called()


if __name__ == '__main__':
    unittest.main()
