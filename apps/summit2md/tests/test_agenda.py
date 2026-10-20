import json
from unittest import mock

from bs4 import BeautifulSoup

from apps.summit2md import agenda, pipeline, server


def event(id, title, timestamp, track='FTS: AI / HPC'):
    return {'id': id, 'title': title, 'start_date': timestamp, 'track': {'name': track}}


def script_for(events):
    # JSON.parse 的参数是 JS 字符串；这里用 JSON 字符串转义再换单引号。
    raw = json.dumps(events, ensure_ascii=False)
    literal = raw.replace('\\', '\\\\').replace("'", "\\'")
    return 'JSON.parse(\'{"time_zone_id":"America/Los_Angeles"}\');JSON.parse(\'' + literal + "');"


def test_bundled_json_is_parsed_without_executing_javascript():
    items = [event(1, "Nu Quantum’s Fabric", 1791990000)]
    events, zone = agenda.bundled_schedule(script_for(items) + ";alert('must not run')")
    assert events == items
    assert zone == 'America/Los_Angeles'


def test_matches_existing_playlist_only_and_orders_by_date_time():
    events = [event(1, 'FTS Awards Ceremony', 1791991800),
              event(2, 'FTS Chair Keynote', 1791990000),
              event(3, 'Other Conference Talk', 1791900000, 'Networking')]
    entries = [{'id': 'award-video', 'title': 'FTS Awards Ceremony'},
               {'id': 'chair-video', 'title': '10158   FTS Chair Keynote'}]
    result = agenda.match_events(events, entries, 'America/Los_Angeles')
    assert len(result['matched']) == 2
    assert result['matched']['chair-video']['order'] < result['matched']['award-video']['order']
    assert result['matched']['chair-video']['day'] == '2026-10-14'
    assert result['matched']['chair-video']['time'] == '08:00'
    assert result['unmatched'] == []


def test_punctuation_truncation_and_ambiguous_title_handling():
    title = 'Copper Sintering BGA Interconnect for Mitigating Electromigration in High-Current AI Power Delivery'
    events = [event(1, title, 1), event(2, 'Quantum at OCP', 2), event(3, 'Quantum at OCP', 3)]
    entries = [{'id': 'copper', 'title': title.replace('High-Current', 'High Current')[:-7]},
               {'id': 'ambiguous', 'title': 'Quantum at OCP'},
               {'id': 'unknown', 'title': 'A completely different session'}]
    result = agenda.match_events(events, entries, 'UTC')
    assert list(result['matched']) == ['copper']
    assert len(result['unmatched']) == 2


def test_adapter_loads_only_same_origin_app_bundle():
    soup = BeautifulSoup('<script src="https://evil.test/app-abc.js"></script><script src="/app-123abc.js"></script>', 'lxml')
    fetch = mock.Mock(return_value=script_for([event(1, 'FTS Chair Keynote', 1791990000)]).encode())
    result = agenda.fetch_fnvirtual_agenda(soup, 'https://2026ocpglobal.fnvirtual.app/a/schedule/',
              [{'id': 'chair', 'title': 'FTS Chair Keynote'}], fetch)
    fetch.assert_called_once_with('https://2026ocpglobal.fnvirtual.app/app-123abc.js')
    assert 'chair' in result['matched']
    assert agenda.fetch_fnvirtual_agenda(soup, 'https://elsewhere.test/a/schedule/', [], fetch) is None


def test_existing_static_video_link_parser_still_works():
    raw = b'<div class="event"><div class="event-time">9:15 AM</div><div class="event-title">Talk</div><a href="https://youtu.be/abcdefghijk">Watch</a></div>'
    with mock.patch.object(pipeline.sources, 'http_get', return_value=raw):
        result = pipeline.fetch_agenda_order('https://conference.test/schedule')
    assert result['matched']['abcdefghijk']['time'] == '9:15 AM'


def test_api_passes_playlist_titles_and_reports_unmatched_dynamic_agenda():
    with mock.patch.object(pipeline, 'fetch_agenda_order', return_value={'matched': {}, 'source': 'fnvirtual'}) as fetch:
        r = server.app.test_client().post('/api/agenda_order', json={
            'agenda_url': 'https://example.fnvirtual.app/a/schedule/',
            'entries': [{'id': 'video', 'title': 'Talk', 'unneeded': 'ignore'}]})
    assert r.status_code == 400
    assert '可靠匹配' in r.get_json()['error']
    assert fetch.call_args.kwargs['entries'] == [{'id': 'video', 'title': 'Talk'}]
