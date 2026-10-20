"""Read public timeline-style conference agendas without executing page scripts."""
import hashlib
import re
from urllib.parse import urlsplit


def supports(url):
    p = urlsplit(url)
    return p.hostname == 'rdi.berkeley.edu' and p.path.startswith('/events/')


def parse(soup, source, video_id):
    containers = soup.select('[id^="agenda-track-"]')
    if not containers or not soup.select('.timeline .event'):
        return None
    tabs = soup.select('.agenda-tab')
    heading = soup.find('title') or soup.find('h1')
    title = heading.get_text(' ',strip=True) if heading else 'Summit'
    title = re.split(r'\s+[|–—]\s+',title)[0].strip()
    meta = soup.select_one('meta[property="og:title"]')
    if meta and meta.get('content'):
        title = meta['content'].strip()
    tracks = []
    for number, container in enumerate(containers):
        stage = tabs[number].get_text(' ',strip=True) if number < len(tabs) else container['id']
        timeline = container.select_one('.timeline')
        if timeline is None:
            continue
        current = 'Opening Remarks'
        current_track = None
        for node in timeline.select('.session-header, .event'):
            classes = node.get('class') or []
            if 'session-header' in classes:
                label = node.select_one('.session-title')
                current = label.get_text(' ',strip=True) if label else node.get_text(' ',strip=True)
                current = re.sub(r'^Session\s+\d+\s*[:：]\s*','',current,flags=re.I)
                current_track = None
                continue
            if 'event' not in classes:
                continue
            event_title = node.select_one('.event-title')
            event_title = event_title.get_text(' ',strip=True) if event_title else current
            workshop = 'workshop' in classes
            session = event_title.replace('[Video]','').strip() if workshop else current
            if workshop:
                current_track = None
            for anchor in node.select('a[href]'):
                vid = video_id(anchor['href'])
                if not vid:
                    continue
                if current_track is None:
                    tid = 'agenda-' + hashlib.sha256((stage+'|'+session).encode()).hexdigest()[:12]
                    current_track = {'id':tid, 'title':stage+' · '+session, 'summary_group':session,
                                     'official_anchor':container['id'],
                                     'stage':stage,'agenda_session':session, 'entries':[], 'count':0}
                    tracks.append(current_track)
                if any(e['id'] == vid for e in current_track['entries']):
                    continue
                speaker = anchor.find_parent(class_='speaker')
                names = (speaker or node).select('.speaker-name, .speaker-title')
                speakers = ' · '.join(n.get_text(' ',strip=True) for n in names)
                talk = speaker.select_one('.event-title-2') if speaker else None
                talk_title = talk.get_text(' ',strip=True).replace('[Video]','').strip() if talk else event_title.replace('[Video]','').strip()
                person = speaker.select_one('.speaker-name') if speaker else None
                if person:
                    talk_title = person.get_text(' ',strip=True)+' - '+talk_title
                time = node.select_one('.event-time')
                current_track['entries'].append({'id':vid,'title':talk_title,'url':'https://www.youtube.com/watch?v='+vid,
                    'speakers':speakers,'stage':stage,'time':time.get_text(' ',strip=True) if time else '',
                    'duration':0,'index':len(current_track['entries'])+1,'is_raw_session':False,'slides':[]})
                current_track['count'] = len(current_track['entries'])
            if workshop:
                current_track = None
    if not tracks:
        return None
    return {'summit_title':title,'source_url':source,'recorded_tracks':tracks,'entries':[],'content_type':'summit'}
