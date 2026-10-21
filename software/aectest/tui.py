"""tui.py - the technician screen: one rich `Live` page on the
same functions as the CLI. `tech run --tui` and `inspect start --tui` build it; the flag is off by default.

Screen(units, sku, operator=None, lot=None, console=None)
    say(text, err=False), ask(line) -> bool, read(prompt) -> str: the signatures that open_units, run_cable and the lot loop
    take. on_event(ev): one call per session event (records.Session.on_event, set by tech.run_cable). The page, top to
    bottom: the header (unit serials, SKU, operator, lot, cable count); the checklist with one mark per line; steps 2..9 of
    3.3 with their state (pending / running / done / stopped) and one measurement line each; one thermal line per end
    (yellow on WARN, red on THROTTLE); the verdict (PASS green, FAIL red, INCONCLUSIVE yellow) with the reason codes, the
    next step and the session path; the last lines said; the prompt as the bottom line.
    A prompt stops the Live page, prints one static copy with the prompt as its last line, reads the answer, then moves
    the cursor back up and restarts the Live page over the copy: the page does not scroll (2.7). The copy is cropped to the
    terminal height, bottom lines kept, so the cursor move never clamps at the screen top. An exception inside
    on_event is logged as `screen: <e>` and never reaches the flow: a screen fault cannot change a verdict.
Screen.plain()
    say_screen / ask_tty / scan_tty with a no-op on_event: what the flag off gives. Screen(...) falls back to the same
    when rich is missing or stdout is not a terminal, and says so in one line. `console=` forces rich (the selftest passes
    Console(file=StringIO(), force_terminal=True)).
"""
import sys
from collections import deque
from itertools import chain

from . import criteria, report, tech
from .lot import scan_tty

ENDS = ('A', 'B')
STATE_STYLE = {'pending': 'dim', 'running': 'bold cyan', 'done': 'green', 'stopped': 'bold red'}
VERDICT_STYLE = {'PASS': 'bold green', 'FAIL': 'bold red', 'INCONCLUSIVE': 'bold yellow'}
THERMAL_STYLE = {'WARN': 'bold yellow', 'THROTTLE': 'bold red'}
LOG_LINES = 5
STEP_OF = {'insert': 2, 'identity': 3, 'health': 4, 'pages': 4, 'release': 5, 'dp_activate': 5, 'prbs': 6, 'vdm': 7, 'flags': 7,
           'lowpwr': 8, 'verdict': 9}


class Screen:
    def __init__(self, units, sku, operator=None, lot=None, console=None):
        self.header = {'Units': f'A {units["A"]}  B {units["B"]}', 'SKU': sku, 'Operator': operator or '-', 'Lot': lot or '-'}
        self.cables = 0
        self.checklist = {}
        self.steps = {n: 'pending' for n in tech.STEPS}
        self.prompt = ''
        self.log = deque(maxlen=LOG_LINES)
        self._reset()
        self.console = console
        if console is None:
            try:
                from rich.console import Console
            except ImportError:
                why = 'rich is not installed'
            else:
                why = None if sys.stdout.isatty() else 'stdout is not a terminal'
                if why is None:
                    self.console = Console(highlight=False)
            if why:
                tech.say_screen(f'--tui off: {why}. The plain lines follow.')
        self.live = None
        if self.console is not None:
            from rich.live import Live
            self.live = Live(get_renderable=self.render, console=self.console, auto_refresh=False, transient=True,
                             redirect_stdout=False, redirect_stderr=False)

    def _reset(self):
        """The per-cable panels start over (a new cable on the lot)."""
        self.measured = {n: {} for n in tech.STEPS}  # step -> {end or direction: text}
        self.samples = {e: [] for e in ENDS}  # the soak health samples, for the slope (tech._slope)
        self.thermal = {e: None for e in ENDS}
        self.verdict = self.session = None

    @classmethod
    def plain(cls):
        """The flag off: the CLI functions behind the Screen names; the fields exist, nothing renders."""
        scr = cls.__new__(cls)
        scr.console = scr.live = None
        scr.checklist, scr.steps, scr.prompt, scr.log = {}, {}, '', deque(maxlen=LOG_LINES)
        scr._reset()
        return scr

    def __enter__(self):
        if self.live:
            self.live.start(refresh=True)
        return self

    def __exit__(self, *exc):
        if self.live:
            self.live.stop()
            self.console.print(self.render())  # the last page stays on the terminal

    # ----------------------------------------------------------------- the three callbacks and the hook
    def say(self, text, err=False):
        if not self.live:
            return tech.say_screen(text, err)
        if text.startswith('session: '):
            self.session = text[len('session: '):]
        if text.strip() and not text.startswith('# '):  # the report page is not a line; the verdict panel holds its facts
            self.log.append(text.splitlines()[0])
        self.live.refresh()

    def ask(self, line):
        if not self.live:
            return tech.ask_tty(line)
        ok = tech.ask_tty(line, self._input)
        if line in self.checklist:
            self.checklist[line] = ok
        return ok

    def read(self, prompt):
        return scan_tty(prompt, self._input) if self.live else scan_tty(prompt)

    def on_event(self, ev):
        if not self.live:
            return
        try:
            self._on_event(ev)
        except Exception as e:
            self.log.append(f'screen: {e}')

    def _on_event(self, ev):
        kind, end, d = ev['type'], ev.get('end'), ev.get('data') or {}
        n = STEP_OF.get(kind)
        if kind == 'step':
            for k, st in self.steps.items():
                if st == 'running':
                    self.steps[k] = 'done'
            self.steps[d['n']] = 'running'
            if d['n'] == 2:  # a new cable on the lot: the per-cable panels start over
                self._reset()
        elif kind == 'precheck':
            self.checklist[tech.precheck_line(d)] = bool(d.get('ok'))
        elif kind in ('stop', 'refused'):
            running = [k for k, st in self.steps.items() if st == 'running']
            if running:
                self.steps[running[0]] = 'stopped'
                self.measured[running[0]]['stop'] = d.get('text') or ' '.join(d.get('codes') or [])
        elif kind == 'insert':
            ms = d.get('mgmt_init_ms')
            self.measured[2][end] = 'absent' if not d.get('present') else f'MgmtInit {ms} ms' if ms is not None else 'present, no edge'
        elif kind == 'identity':
            self.measured[3][end] = f'{tech.identity_line(d)} class {d["power_class"]}'
        elif kind == 'health':
            level = d.get('thermal')
            self.thermal[end] = (d['temp_c'], level)
            if d.get('t_s', 0) == 0:
                self.measured[4][end] = f'{d["state_name"]} {d["temp_c"]:.1f} C {d["vcc_v"]:.3f} V'
            else:
                self.samples[end].append(d)
                slope = tech._slope(self.samples[end])
                self.measured[5][end] = f'{d["temp_c"]:.1f} C' + (f' slope {slope:+.2f} C/min' if slope is not None else '')
        elif kind == 'prbs':
            for direction, verdict in (d.get('directions') or {}).items():  # 'A_to_B' -> 'A>B'
                self.measured[6][direction.replace('_to_', '>')] = verdict
        elif kind == 'vdm':
            self.measured[7][end] = 'VDM recorded' if d.get('page_2fh') else 'VDM not advertised'
        elif kind == 'lowpwr':
            after = d.get('lowpwr_after')
            self.measured[8][end] = f'low power in {after:.1f} s' if after is not None else 'low power'
        elif kind == 'verdict':
            self.verdict = d
            self.cables += 1
            self.steps[9] = 'done'
            k = d.get('key_numbers') or {}
            for direction in ('AB', 'BA'):
                if k.get(f'worst_errors_{direction}') is not None:
                    key = f'{direction[0]}>{direction[1]}'
                    self.measured[6][key] = f'{self.measured[6].get(key, "")} worst {k[f"worst_errors_{direction}"]} errors'.strip()
        if n and self.steps[n] == 'pending':
            self.steps[n] = 'running'
        self.live.refresh()

    # ----------------------------------------------------------------- rendering
    def _input(self, prompt):
        """The prompt as the bottom line, the answer under it, then the Live page back over the static copy. The copy is
        the last `console.height - 1` lines of the page: the cursor move back up never clamps at the screen top."""
        from rich.control import Control, ControlType
        from rich.segment import Segment, Segments
        self.prompt = prompt
        self.live.stop()
        lines = self.console.render_lines(self.render(), pad=False)[-(self.console.height - 1):]
        height = len(lines)
        self.console.print(Segments(chain.from_iterable(line + [Segment.line()] for line in lines)), end='')
        try:
            return self.console.input()
        finally:
            self.prompt = ''
            self.console.control(Control.move(0, -1), Control((ControlType.ERASE_IN_LINE, 2)), Control.move(0, -height))
            self.live.start(refresh=True)

    def render(self):
        from rich.console import Group
        from rich.markup import escape
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        head = Table.grid(padding=(0, 2))
        head.add_row(*(f'{k}: {escape(str(v))}' for k, v in self.header.items()), f'Cables: {self.cables}')
        check = Table.grid(padding=(0, 1))
        for line, ok in self.checklist.items():
            mark, style = ('[x]', 'green') if ok else ('[n]', 'bold red') if ok is False else ('[ ]', 'dim')
            check.add_row(Text(mark, style=style), Text(line, style=None if ok is None else style))
        steps = Table.grid(padding=(0, 1))
        for n, name in tech.STEPS.items():
            st = self.steps[n]
            parts = [v if k == 'stop' else f'{k}: {v}' for k, v in self.measured[n].items()]
            steps.add_row(Text(f'{n} {name:<16}', style=STATE_STYLE[st]), Text(f'{st:<8}', style=STATE_STYLE[st]), Text('  '.join(parts)))
        therm = Table.grid(padding=(0, 1))
        for end, t in self.thermal.items():
            text = f'end {end}: {t[0]:.1f} C {t[1] or ""}'.rstrip() if t else f'end {end}: -'
            therm.add_row(Text(text, style=THERMAL_STYLE.get(t[1]) if t else 'dim'))
        if self.verdict:
            v = self.verdict
            verdict = Group(Text(tech.verdict_line(v['status'], [criteria.code_of(r) for r in v.get('reasons') or []]),
                                 style=VERDICT_STYLE.get(v['status'], 'bold')),
                            Text(report.next_step(v)), Text(f'session: {self.session or "-"}', style='dim'))
        else:
            verdict = Text('no verdict yet', style='dim')
        log = Group(*(Text(t, style=tech.line_style(t)) for t in self.log)) if self.log else Text('', style='dim')
        return Group(Panel(head, title='AEC-CT2-MINI technician screen'), Panel(check, title='Checklist'),
                     Panel(steps, title='Steps'), Panel(therm, title='Thermal'), Panel(verdict, title='Verdict'),
                     Panel(log, title='Last lines'), Text(self.prompt, style='bold'))
