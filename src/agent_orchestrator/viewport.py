"""Pure, screen-sized navigation over stable dashboard rows."""
import textwrap


def wrap(text, width):
    return [s.rstrip() for s in textwrap.wrap(text, width=max(1, width),
            replace_whitespace=False, drop_whitespace=False, break_on_hyphens=False)] or ['']


class Viewport:
    def __init__(self):
        self.selected = None
        self.manual = False
        self.offset = 0
        self.anchor = None
        self.sections = []
        self.rows = []
        self.page = 1

    def render(self, sections, size, paint=lambda line: line):
        self.sections = sections
        width, height = max(1, size[0]-1), max(1, size[1])
        if not sections:
            self.rows = []
            return '\n'.join(['Orchestrator dashboard'[:width],
                              'No objectives; waiting for submissions.'[:width]][:height])
        keys = [s['key'] for s in sections]
        active = next((s for s in sections if s['active']), None)
        if self.selected not in keys or (not self.manual and active and active['key'] != self.selected):
            chosen = active or next((s for s in sections if s['state'] not in
                                    ('COMPLETED', 'BLOCKED', 'STOPPED')), sections[-1])
            self.selected = chosen['key']
            self.offset, self.anchor = 0, None
        section = next(s for s in sections if s['key'] == self.selected)
        header = [s for line in ['Orchestrator dashboard', *section['header']] for s in wrap(line, width)]
        header_limit = max(0, min(len(header), height//2, height-2))
        # Header overflow remains reachable in the body, including on tiny terminals.
        body = [(f'{self.selected}:header:{i}', line) for i, line in enumerate(header[header_limit:])]
        body.extend(section['body'])
        self.rows = [(key, part, line) for key, text in body
                     for part, line in enumerate(wrap(text, width))]
        self.page = max(0, height-header_limit-1)
        if self.anchor is not None:
            found = next((i for i, row in enumerate(self.rows) if row[:2] == self.anchor), None)
            if found is None:
                found = next((i for i, row in enumerate(self.rows) if row[0] == self.anchor[0]), None)
            if found is not None:
                self.offset = found
        self.offset = max(0, min(self.offset, max(0, len(self.rows)-max(1, self.page))))
        self.anchor = self.rows[self.offset][:2] if self.rows else None
        visible = self.rows[self.offset:self.offset+self.page]
        start = self.offset+1 if self.rows else 0
        footer = (f'Objective {keys.index(self.selected)+1}/{len(keys)} | '
                  f'Rows {start}-{min(len(self.rows), self.offset+self.page)}/{len(self.rows)} | '
                  'Wheel/Arrows PgUp/PgDn Home/End Tab a:active Ctrl+C:stop')
        lines = [*header[:header_limit], *(row[2] for row in visible), footer[:width]]
        return '\n'.join(paint(line) for line in lines[:height])

    def handle(self, action, amount=1):
        if not self.sections or action == 'resize':
            return
        self.manual = True
        keys = [s['key'] for s in self.sections]
        if action in ('next', 'previous'):
            index = keys.index(self.selected) if self.selected in keys else 0
            self.selected = keys[(index+(1 if action == 'next' else -1)) % len(keys)]
            self.offset = 0
        elif action == 'active':
            section = next((s for s in self.sections if s['active']), None)
            if section:
                if self.selected != section['key']:
                    self.selected, self.offset = section['key'], 0
                    self.anchor = (section['active']+':0', 0)
                    return
                self.offset = next((i for i, row in enumerate(self.rows)
                                    if row[0] == section['active']+':0'), 0)
        elif action == 'home':
            self.offset = 0
        elif action == 'end':
            self.offset = max(0, len(self.rows)-max(1, self.page))
        else:
            delta = {'up': -amount, 'down': amount, 'scroll': amount,
                     'page_up': -max(1, self.page-1)*amount,
                     'page_down': max(1, self.page-1)*amount}.get(action, 0)
            self.offset += delta
        self.offset = max(0, min(self.offset, max(0, len(self.rows)-max(1, self.page))))
        self.anchor = None
