"""Conservative session-format recognition shared by import, UI and speaker handling."""
import re

LABELS = {'talk':'议题', 'panel':'Panel', 'fireside':'Fireside', 'roundtable':'圆桌',
          'discussion':'讨论 / 对话', 'qa':'Q&A', 'workshop':'Workshop', 'spotlight':'Startup Spotlight'}
# These formats imply dialogue; a workshop or company showcase may be a solo talk.
MULTI_SPEAKER_TYPES = ('panel', 'fireside', 'roundtable', 'discussion', 'qa')

# Keep patterns compatible with both Python re and browser RegExp (case-insensitive).
RULES = [
    ('fireside', r'\bfireside(?:\s+chat)?\b|炉边(?:对话|谈话|访谈)'),
    ('panel', r'(^|[:：–—])\s*panel\b(?![-\s]+(?:level|packaging|manufacturing)\b)|\bpanel\s+(?:discussion|of\s+experts|on)\b|\bpanel\s*$|专家讨论|小组讨论'),
    ('roundtable', r'\bround[\s-]*table\b|圆桌'),
    ('qa', r'\bq\s*&\s*a\b|\bquestions\s+and\s+answers\b|问答'),
    ('discussion', r'\b(?:open\s+discussion|in\s+conversation|conversation\s+with|dialogue|interview)\b|对谈|对话|访谈'),
    ('workshop', r'^\s*workshop\s*[:：–—-]|工作坊'),
    ('spotlight', r'^\s*startup\s+spotlight\s*[:：–—-]'),
]


def from_title(title):
    # Strip common agenda/video prefixes without making technical "panel-level" talks panels.
    title = re.sub(r'^\s*(?:\[\d+\]|\d{4,6})\s*', '', title or '')
    return next((kind for kind, pattern in RULES if re.search(pattern, title, re.I)), 'talk')


def classify(entry):
    override = entry.get('session_type_override')
    if isinstance(override, str) and override in LABELS:
        return override
    agenda = (entry.get('agenda_meta') or {}).get('agenda_title') or ''
    return next((kind for kind in (from_title(agenda), from_title(entry.get('title'))) if kind != 'talk'), 'talk')


def is_multi_speaker(entry):
    # Over-3-hour videos count as raw livestream recordings, but a type set by hand (e.g. a whole
    # series marked as dialogue, where 4-hour interviews are normal) still wins.
    override = entry.get('session_type_override')
    if isinstance(override, str) and override in LABELS:
        return override in MULTI_SPEAKER_TYPES
    return not entry.get('is_raw_session') and classify(entry) in MULTI_SPEAKER_TYPES


def catalog():
    return {'labels':LABELS, 'rules':RULES, 'multi_speaker_types':MULTI_SPEAKER_TYPES}
