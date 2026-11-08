"""Session Bridge: import, export and auto-sync sessions between the AI harnesses on this machine.

    SessionBridge.exe                 GUI
    SessionBridge.exe --detect        harnesses found (JSON)
    SessionBridge.exe --preview       what the saved routes would copy
    SessionBridge.exe --sync-once     one pass over the saved routes
    SessionBridge.exe --start         start auto-sync in the background and return
    SessionBridge.exe --stop          ask the background auto-sync to stop
    SessionBridge.exe --watch         the auto-sync loop itself, in this process
    SessionBridge.exe --self-test     sandboxed self-check

A windowed EXE has no console, so CLI output also goes to ~/.session-bridge/last-output.txt.
"""
import contextlib
import ctypes
import io
import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import winreg
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import adapters as A
import core
from core import tr

LANGS = {'Indonesia': 'id', 'English': 'en'}
BUNDLE_LABEL = 'File bundle (.json)'  # Shown through tr(); compare with bundle_label().
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'SessionBridge'
HINTS = {'export': 'Kirim sesi ke harness lain, atau simpan sebagai file bundle.',
         'import': 'Masukkan sesi ke sebuah harness, dari harness lain atau dari file bundle.'}
DEEPSEEK = ('DeepSeek lewat Claude Code atau OpenCode tersimpan di store harness itu, jadi ikut terbawa. '
            'Harness lain dengan folder sendiri muncul otomatis di sini sebagai baca-saja.\n'
            'File bundle dan salinan sesi memuat isi chat apa adanya, termasuk rahasia yang pernah tertulis di chat.')
BG, SIDE, CARD, LINE = '#f5f6f8', '#eceff3', '#ffffff', '#e1e4ea'
TEXT, MUTED, ACCENT, ACCENT_SOFT = '#1d2330', '#667085', '#2f6fed', '#e8efff'
GREEN, GREEN_SOFT = '#157a3e', '#dcf5e4'


def bundle_label():
    return tr(BUNDLE_LABEL)


def self_cmd(*args):
    if getattr(sys, 'frozen', False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def login_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, RUN_VALUE)[0])
    except OSError:
        return False


def set_login(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(self_cmd('--watch')))
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, RUN_VALUE)


def watch_running():
    with core.single('watch.lock') as free:
        return not free


def spawn_watch():
    (core.state_dir() / 'STOP').unlink(missing_ok=True)
    # PyInstaller would otherwise share the GUI's unpack folder, which vanishes when the GUI closes.
    subprocess.Popen(self_cmd('--watch'), env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def watch():
    stop = core.state_dir() / 'STOP'
    with core.single('watch.lock', wait=5) as free:
        if not free:
            return 1
        stop.unlink(missing_ok=True)
        logging.info('watch started')
        due = 0
        while not stop.exists():
            if time.time() >= due:
                cfg = core.load_cfg()
                due = time.time() + max(1, int(cfg.get('interval_min', 5))) * 60
                try:
                    scan = any(str(route[0]).startswith('auto:') for route in cfg.get('routes', []))
                    results = core.sync_all(cfg, A.registry(scan=scan))  # No log callback: titles stay out of the log.
                    if results:
                        logging.info('pass: %d sessions changed', len(results))
                except Exception as error:
                    logging.warning('pass failed: %s', type(error).__name__)
            time.sleep(5)
        stop.unlink(missing_ok=True)
        logging.info('watch stopped')
    return 0


def tally(results):
    """Adoption rows copy nothing, so they don't count as sessions."""
    return tr('{s} sesi, {m} pesan.', s=sum(r[5] > 0 for r in results), m=sum(r[5] for r in results))


def emit(text):
    (core.state_dir() / 'last-output.txt').write_text(text + '\n', encoding='utf-8')
    if sys.stdout:
        print(text)


def cli(flag):
    if flag == '--watch':
        return watch()
    if flag == '--start':
        if not watch_running():
            spawn_watch()
        return 0
    if flag == '--stop':
        (core.state_dir() / 'STOP').touch()
        return 0
    if flag == '--detect':
        emit(json.dumps(A.detect_rows(A.registry()), ensure_ascii=False, indent=1))
        return 0
    if flag == '--self-test':
        import test_bridge
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                test_bridge.run()
        except Exception:
            emit(out.getvalue() + traceback.format_exc())
            return 1
        emit(out.getvalue() + 'ALL OK')
        return 0
    if flag in ('--preview', '--sync-once'):
        lines = []
        results = core.sync_all(core.load_cfg(), A.registry(), dry=flag == '--preview', log=lines.append)
        if results is None:
            emit(tr('Sinkron lain masih berjalan.'))
            return 1
        emit('\n'.join(lines + [tally(results)]))
        return 0
    emit(tr('Opsi tidak dikenal: {flag}', flag=flag))
    return 2


def theme(root):
    """Flat light look on clam, the built-in theme whose colours are all configurable. Returns the DPI scale."""
    scale = root.winfo_fpixels('1i') / 96
    for name in ('TkDefaultFont', 'TkTextFont', 'TkHeadingFont', 'TkMenuFont'):
        tkfont.nametofont(name).configure(family='Segoe UI', size=10)
    for key, value in (('background', CARD), ('selectBackground', ACCENT_SOFT), ('selectForeground', TEXT), ('font', 'TkDefaultFont')):
        root.option_add(f'*TCombobox*Listbox.{key}', value)
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', background=CARD, foreground=TEXT, bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, troughcolor=BG,
                focuscolor=ACCENT, selectbackground=ACCENT_SOFT, selectforeground=TEXT, insertcolor=TEXT, font='TkDefaultFont')
    semi = ('Segoe UI Semibold', 10)
    for style, opts in {
        'Page.TFrame': dict(background=BG), 'Side.TFrame': dict(background=SIDE),
        'Muted.TLabel': dict(foreground=MUTED), 'Page.TLabel': dict(background=BG, foreground=MUTED),
        'Title.TLabel': dict(background=BG, font=('Segoe UI Semibold', 17)), 'H.TLabel': dict(font=('Segoe UI Semibold', 11)),
        'Arrow.TLabel': dict(foreground=MUTED, font=('Segoe UI Semibold', 13)),
        'Step.TLabel': dict(background=ACCENT_SOFT, foreground=ACCENT, font=semi, padding=(8, 0)),
        'Brand.TLabel': dict(background=SIDE, font=('Segoe UI Semibold', 14)),
        'Side.TLabel': dict(background=SIDE, foreground=MUTED, font=('Segoe UI', 9)),
        'On.TLabel': dict(background=GREEN_SOFT, foreground=GREEN, font=semi, padding=(12, 8)),
        'Off.TLabel': dict(background=LINE, foreground=MUTED, font=semi, padding=(12, 8)),
    }.items():
        s.configure(style, **opts)

    def button(style, base, hover, press, fg, off, off_fg):
        edges = ('background', 'bordercolor', 'lightcolor', 'darkcolor')
        s.configure(style, foreground=fg, padding=(16, 7), **dict.fromkeys(edges, base))
        s.map(style, foreground=[('disabled', off_fg)], **dict.fromkeys(edges, [('disabled', off), ('pressed', press), ('active', hover)]))
    button('TButton', '#eceff3', '#e1e5eb', '#d5dae2', TEXT, '#f2f3f6', '#a3a9b4')
    button('Accent.TButton', ACCENT, '#2459cc', '#1d4cb0', '#ffffff', '#a9c3f7', '#ffffff')

    for name in ('Nav', 'Seg'):  # Radio buttons without the dot, drawn as tabs.
        s.layout(f'{name}.TRadiobutton', [('Radiobutton.padding', {'sticky': 'nswe', 'children': [
            ('Radiobutton.focus', {'sticky': 'nswe', 'children': [('Radiobutton.label', {'sticky': 'nswe'})]})]})])
    s.configure('Nav.TRadiobutton', background=SIDE, padding=(14, 10))
    s.map('Nav.TRadiobutton', background=[('selected', CARD), ('active', '#e3e6eb')], foreground=[('selected', ACCENT)], font=[('selected', semi)])
    s.configure('Seg.TRadiobutton', background='#eceff3', padding=(22, 7))
    s.map('Seg.TRadiobutton', background=[('selected', ACCENT), ('active', '#e1e5eb')], foreground=[('selected', '#ffffff')], font=[('selected', semi)])

    s.configure('TCheckbutton', indicatorbackground=CARD, indicatorforeground='#ffffff', upperbordercolor='#b9bfca', lowerbordercolor='#b9bfca',
                indicatorsize=round(13 * scale), indicatormargin=(0, 0, round(6 * scale), 0))
    s.map('TCheckbutton', background=[('active', CARD)], indicatorbackground=[('selected', ACCENT)],
          upperbordercolor=[('selected', ACCENT)], lowerbordercolor=[('selected', ACCENT)])
    for name in ('TCombobox', 'TEntry', 'TSpinbox'):
        s.configure(name, fieldbackground=CARD, bordercolor='#cdd2da', arrowcolor=MUTED, arrowsize=round(12 * scale), padding=5)
        s.map(name, bordercolor=[('focus', ACCENT)], lightcolor=[('focus', ACCENT)], background=[('active', CARD)],
              fieldbackground=[('readonly', CARD)], selectbackground=[('readonly', CARD)], selectforeground=[('readonly', TEXT)])

    s.configure('Treeview', fieldbackground=CARD, bordercolor=CARD, rowheight=tkfont.nametofont('TkDefaultFont').metrics('linespace') + round(10 * scale))
    s.map('Treeview', background=[('selected', ACCENT_SOFT)], foreground=[('selected', TEXT)])
    s.configure('Treeview.Heading', foreground=MUTED, bordercolor=LINE, relief='flat', font=('Segoe UI Semibold', 9), padding=(4, 6))
    s.map('Treeview.Heading', background=[('active', BG)])
    s.layout('Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children': [
        ('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
    s.configure('Vertical.TScrollbar', troughcolor=CARD, bordercolor=CARD, arrowsize=round(9 * scale), gripcount=0,
                **dict.fromkeys(('background', 'lightcolor', 'darkcolor'), '#cdd2da'))
    s.map('Vertical.TScrollbar', **dict.fromkeys(('background', 'lightcolor', 'darkcolor'), [('active', '#aeb5c1')]))
    return scale


def icon(root):
    """Two arrows on a rounded blue square, drawn here so the EXE needs no image file."""
    arrow = ['bbbbbbbbbwbbbbbb', 'bbbbbbbbbwwbbbbb', 'bbbwwwwwwwwwbbbb', 'bbbwwwwwwwwwbbbb', 'bbbbbbbbbwwbbbbb', 'bbbbbbbbbwbbbbbb']
    cap = ['  ' + 'b' * 12 + '  ', ' ' + 'b' * 14 + ' ']
    image = tk.PhotoImage(width=16, height=16)
    for y, row in enumerate(cap + arrow + [r[::-1] for r in arrow] + cap[::-1]):
        for x, pixel in enumerate(row):
            if pixel != ' ':
                image.put(ACCENT if pixel == 'b' else '#ffffff', (x, y))
    image = image.zoom(2)
    root.iconphoto(True, image)
    return image


class App:
    def __init__(self, root, page='transfer', first=True):
        self.root = root
        self.alive = True
        self.adapters, self.by_label, self.pages = {}, {}, {}
        self.jobs = queue.Queue()
        self.bundle, self.metas = [], []
        self.cfg = core.load_cfg()
        core.LANG = self.cfg.get('lang', 'id')
        self.want, self.settle = None, 0  # Keeps "memulai…" on screen until the watcher answers or a deadline passes.
        log = core.state_dir() / 'bridge.log'
        self.log_pos = log.stat().st_size if log.exists() else 0
        self.scale = theme(root)
        if first:
            self.icon = icon(root)
            root.title('Session Bridge')
            width, height = round(1100 * self.scale), round(740 * self.scale)
            root.geometry(f'{width}x{height}+{max(0, (root.winfo_screenwidth() - width) // 2)}+{max(0, (root.winfo_screenheight() - height) // 3)}')
            root.minsize(round(880 * self.scale), round(620 * self.scale))
        root.report_callback_exception = lambda _kind, error, _trace: self.fail(error)
        self.build_side()
        self.content = ttk.Frame(root, style='Page.TFrame')
        self.content.pack(side='left', fill='both', expand=True)
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.build_transfer()
        self.build_auto()
        self.build_harness()
        self.goto(page)
        self.pump()
        self.poll()
        self.detect()

    def set_lang(self, lang):
        """Rebuilds the window in the new language. The old instance's timers see `alive` and stop."""
        self.save()
        self.cfg['lang'] = lang
        core.save_cfg(self.cfg)
        self.alive = False
        for child in self.root.winfo_children():
            child.destroy()
        return App(self.root, self.page.get(), first=False)

    # --- Plumbing ---

    def pump(self):
        if not self.alive:
            return
        with contextlib.suppress(queue.Empty):
            while True:
                self.jobs.get_nowait()()
        self.root.after(80, self.pump)

    def run(self, work, done=None):
        """Harness CLIs and large stores are slow, so work happens off the UI thread."""
        def body():
            try:
                result = work()
            except Exception as error:
                logging.warning('job failed: %s', type(error).__name__)
                self.jobs.put(lambda e=error: self.fail(e))
                return
            if done:
                self.jobs.put(lambda r=result: done(r))
        threading.Thread(target=body, daemon=True).start()

    def fail(self, error):
        self.go.state(['!disabled'])
        self.detect_btn.state(['!disabled'])
        self.refresh_go()
        messagebox.showerror('Session Bridge', tr(str(error)) or type(error).__name__)

    def write(self, box, text):
        box.configure(state='normal')
        box.insert('end', text + '\n')
        box.see('end')
        box.configure(state='disabled')

    def logger(self, box):
        return lambda line: self.jobs.put(lambda text=line: self.write(box, text))

    # --- Layout pieces ---

    def build_side(self):
        self.side = ttk.Frame(self.root, style='Side.TFrame', padding=(10, 22, 10, 14))
        self.side.pack(side='left', fill='y')
        ttk.Label(self.side, text='Session Bridge', style='Brand.TLabel').pack(anchor='w', padx=8)
        ttk.Label(self.side, text=tr('Pindah dan sinkron sesi\nantar harness AI.'), style='Side.TLabel').pack(anchor='w', padx=8, pady=(2, 22))
        self.page = tk.StringVar()
        self.status = tk.StringVar()
        self.pill = ttk.Label(self.side, textvariable=self.status, style='Off.TLabel', cursor='hand2')
        self.pill.pack(side='bottom', fill='x')
        self.pill.bind('<Button-1>', lambda _event: self.goto('auto'))
        line = ttk.Frame(self.side, style='Side.TFrame')
        line.pack(side='bottom', fill='x', pady=(0, 10))
        ttk.Label(line, text=tr('Bahasa'), style='Side.TLabel').pack(side='left', padx=(8, 8))
        self.lang = ttk.Combobox(line, state='readonly', width=10, values=list(LANGS))
        self.lang.set(next(k for k, v in LANGS.items() if v == core.LANG))
        self.lang.pack(side='left', fill='x', expand=True)
        self.lang.bind('<<ComboboxSelected>>', lambda _event: LANGS[self.lang.get()] != core.LANG and self.set_lang(LANGS[self.lang.get()]))

    def add_page(self, name, nav, title, intro):
        ttk.Radiobutton(self.side, text=nav, value=name, variable=self.page, command=self.goto, style='Nav.TRadiobutton').pack(fill='x', pady=1)
        page = ttk.Frame(self.content, style='Page.TFrame', padding=(28, 22, 28, 20))
        page.grid(row=0, column=0, sticky='nsew')
        ttk.Label(page, text=title, style='Title.TLabel').pack(anchor='w')
        self.wrap(ttk.Label(page, text=intro, style='Page.TLabel')).pack(fill='x', pady=(2, 16))
        self.pages[name] = page
        return page

    def goto(self, name=None):
        if name:
            self.page.set(name)
        self.pages[self.page.get()].tkraise()

    def card(self, parent, title, step=None, **pack):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightcolor=LINE, highlightthickness=1)
        outer.pack(**{'fill': 'x', 'pady': (0, 12), **pack})
        body = ttk.Frame(outer, padding=(18, 14))
        body.pack(fill='both', expand=True)
        head = ttk.Frame(body)
        head.pack(fill='x', pady=(0, 10))
        if step:
            ttk.Label(head, text=step, style='Step.TLabel').pack(side='left', padx=(0, 10))
        ttk.Label(head, text=title, style='H.TLabel').pack(side='left')
        return body, head

    def wrap(self, label):
        label.bind('<Configure>', lambda event: label.configure(wraplength=event.width))
        return label

    def pair(self, parent, width):
        """`Dari [..] → Ke [..]` on one line."""
        line = ttk.Frame(parent)
        line.pack(fill='x')
        ttk.Label(line, text=tr('Dari')).pack(side='left')
        src = ttk.Combobox(line, state='readonly', width=width)
        src.pack(side='left', padx=(8, 0))
        ttk.Label(line, text='→', style='Arrow.TLabel').pack(side='left', padx=14)
        ttk.Label(line, text=tr('Ke')).pack(side='left')
        dst = ttk.Combobox(line, state='readonly', width=width)
        dst.pack(side='left', padx=(8, 16))
        return line, src, dst

    def log_box(self, parent, height):
        frame = ttk.Frame(parent)
        box = tk.Text(frame, height=height, wrap='word', state='disabled', bg='#f7f8fa', fg='#3a4150', relief='flat', bd=0,
                      highlightthickness=0, padx=12, pady=8, font=('Consolas', 9))
        bar = ttk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')  # Packed first so a wide widget can't push it out of view.
        box.pack(side='left', fill='both', expand=True)
        return frame, box

    def table(self, parent, columns, select, height):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', selectmode=select, height=height)
        for name, width in columns:
            tree.heading(name, text=name, anchor='w')
            tree.column(name, width=round(width * self.scale), stretch=name == columns[0][0])
        bar = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        return frame, tree

    def detect(self):
        self.detect_btn.state(['disabled'])
        self.harness_status.set(tr('Mendeteksi harness…'))

        def work():
            found = A.registry()
            self.jobs.put(lambda: self.on_found(found))  # Combos fill first; counting sessions can take a while.
            return A.detect_rows(found)

        def done(rows):
            self.detect_btn.state(['!disabled'])
            self.harness_status.set(tr('{n} harness, {w} bisa ditulis', n=len(rows), w=sum(bool(r['write']) for r in rows)))
            self.harnesses.delete(*self.harnesses.get_children())
            for row in rows:
                access = tr('✓ baca + tulis') if row['write'] else tr('baca-saja')
                self.harnesses.insert('', 'end', values=(row['label'], row['sessions'], access, row['path'], row['note']),
                                      tags=() if row['write'] else ('ro',))
        self.run(work, done)

    def on_found(self, found):
        self.adapters = found
        self.by_label = {a.label: a for a in found.values()}
        self.on_mode()
        labels = list(self.by_label)
        targets = [a.label for a in found.values() if a.can_write]
        self.route_src['values'], self.route_dst['values'] = labels, targets
        if not self.route_src.get() and labels:
            self.route_src.set(labels[0])
            self.route_dst.set(next((t for t in targets if t != labels[0]), ''))
        self.show_routes()

    # --- Import / Export ---

    def build_transfer(self):
        page = self.add_page('transfer', '⇄   Import / Export', 'Import / Export',
                             tr('Salin sesi sekali jalan ke harness lain, atau simpan dan buka lewat file bundle.'))
        body, _ = self.card(page, tr('Pilih arah'), '1')
        self.mode = tk.StringVar(value='export')
        line = ttk.Frame(body)
        line.pack(fill='x', pady=(0, 12))
        for text, value in (('Export', 'export'), ('Import', 'import')):
            ttk.Radiobutton(line, text=text, value=value, variable=self.mode, command=self.on_mode, style='Seg.TRadiobutton').pack(side='left')
        self.hint = tk.StringVar()
        ttk.Label(line, textvariable=self.hint, style='Muted.TLabel').pack(side='left', padx=16)
        _, self.src, self.dst = self.pair(body, 30)
        self.src.bind('<<ComboboxSelected>>', lambda _event: self.on_source())
        self.dst.bind('<<ComboboxSelected>>', lambda _event: self.refresh_go())

        body, _ = self.card(page, tr('Aktivitas'), side='bottom', pady=0)
        frame, self.transfer_log = self.log_box(body, 4)
        frame.pack(fill='both', expand=True)

        body, head = self.card(page, tr('Pilih sesi'), '2', fill='both', expand=True)
        self.count = tk.StringVar()
        ttk.Label(head, textvariable=self.count, style='Muted.TLabel').pack(side='left', padx=10)
        ttk.Button(head, text=tr('Muat ulang'), command=self.load_list).pack(side='right')
        ttk.Button(head, text=tr('Pilih semua'), command=self.pick_all).pack(side='right', padx=8)
        self.query = tk.StringVar()
        self.query.trace_add('write', lambda *_: self.render())
        ttk.Entry(head, textvariable=self.query, width=26).pack(side='right', padx=(6, 0))
        ttk.Label(head, text=tr('Cari'), style='Muted.TLabel').pack(side='right')
        foot = ttk.Frame(body)
        foot.pack(side='bottom', fill='x', pady=(12, 0))
        self.go_note = tk.StringVar()
        ttk.Label(foot, textvariable=self.go_note, style='Muted.TLabel').pack(side='left')
        self.go = ttk.Button(foot, text=tr('Pilih sesi dulu'), style='Accent.TButton', command=self.execute)
        self.go.pack(side='right')
        frame, self.tree = self.table(body, ((tr('Judul'), 470), (tr('Terakhir diubah'), 140), (tr('Folder kerja'), 260)), 'extended', 8)
        frame.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda _event: self.refresh_go())
        self.tree.bind('<Control-a>', lambda _event: self.pick_all())

    def on_mode(self):
        mode = self.mode.get()
        self.hint.set(tr(HINTS[mode]))
        labels = list(self.by_label)
        targets = [a.label for a in self.adapters.values() if a.can_write]
        self.src['values'] = labels + [bundle_label()] if mode == 'import' else labels
        self.dst['values'] = targets + [bundle_label()] if mode == 'export' else targets
        if self.src.get() not in self.src['values']:
            self.src.set(labels[0] if labels else '')
            self.on_source()
        if self.dst.get() not in self.dst['values']:
            self.dst.set(next((t for t in targets if t != self.src.get()), ''))
        self.refresh_go()

    def on_source(self):
        if self.src.get() != bundle_label():
            self.load_list()
            return
        path = filedialog.askopenfilename(title=tr('Pilih file bundle'), filetypes=[(tr('Bundle Session Bridge'), '*.json')])
        if not path:
            return
        self.bundle = []
        self.show([])
        self.run(lambda: core.load_bundle(path), self.on_bundle)

    def on_bundle(self, sessions):
        self.bundle = sessions
        self.show([core.Meta(str(i), s.title or s.id, s.updated, s.cwd) for i, s in enumerate(sessions)])

    def load_list(self):
        label = self.src.get()
        if label == bundle_label():
            self.on_bundle(self.bundle)
            return
        adapter = self.by_label.get(label)
        if not adapter:
            return
        self.show([])
        self.count.set(tr('Memuat…'))
        self.run(adapter.list, lambda metas: self.src.get() == label and self.show(metas))

    def show(self, metas):
        unique = {m.id: m for m in metas}  # A store can hold one id twice (active and archived); Treeview ids must be unique.
        self.metas = sorted(unique.values(), key=lambda m: m.updated, reverse=True)
        self.tree.delete(*self.tree.get_children())
        self.render()

    def render(self):
        """Search filter: every word must appear in the title or folder. Selection survives while still visible."""
        keep = set(self.tree.selection())
        words = self.query.get().lower().split()
        rows = [m for m in self.metas if all(w in f'{m.title} {m.cwd}'.lower() for w in words)]
        self.tree.delete(*self.tree.get_children())
        for meta in rows:
            when = datetime.fromtimestamp(meta.updated).strftime('%Y-%m-%d %H:%M') if meta.updated else ''
            self.tree.insert('', 'end', iid=meta.id, values=(meta.title, when, meta.cwd))
        self.tree.selection_set([m.id for m in rows if m.id in keep])
        total = len(self.metas)
        self.count.set(tr('{x} dari {n} sesi', x=len(rows), n=total) if words else tr('{n} sesi', n=total) if total else tr('Belum ada sesi'))
        self.refresh_go()

    def pick_all(self):
        self.tree.selection_set(self.tree.get_children())
        return 'break'

    def refresh_go(self):
        picked, dst = len(self.tree.selection()), self.dst.get()
        bundle = dst == bundle_label()
        self.go_note.set(tr('File berisi isi chat apa adanya. Simpan di tempat aman.') if bundle else
                         tr('Sesi yang sudah ada di tujuan tidak diubah atau dihapus.'))
        if self.go.instate(['!disabled']):
            self.go.configure(text=(tr('Simpan {n} sesi ke file  →', n=picked) if bundle else tr('Salin {n} sesi ke {dst}  →', n=picked, dst=dst or '…'))
                           if picked else tr('Pilih sesi dulu'))

    def execute(self):
        picks = [str(i) for i in self.tree.selection()]
        src_label, dst_label = self.src.get(), self.dst.get()
        if not picks:
            messagebox.showinfo('Session Bridge', tr('Pilih dulu sesi yang akan disalin.'))
            return
        if src_label == dst_label:
            messagebox.showinfo('Session Bridge', tr('Asal dan tujuan harus berbeda.'))
            return
        log = self.logger(self.transfer_log)
        if dst_label == bundle_label():
            path = filedialog.asksaveasfilename(title=tr('Simpan bundle'), defaultextension='.json', filetypes=[(tr('Bundle Session Bridge'), '*.json')],
                                                initialfile=f'sesi-{datetime.now():%Y%m%d-%H%M}.json')
            if not path:
                return
            src = self.by_label[src_label]
            work = lambda: core.export_bundle(src, picks, path, log)
            finish = lambda n: tr('Selesai: {n} sesi tersimpan di {path}. Isinya chat apa adanya; simpan di tempat aman.', n=n, path=path)
        else:
            dst = self.by_label[dst_label]
            question = tr('Salin {n} sesi dari {src} ke {dst}?\nData yang sudah ada tidak diubah atau dihapus.', n=len(picks), src=src_label, dst=dst.label)
            if not messagebox.askyesno('Session Bridge', question):
                return
            if src_label == bundle_label():
                pairs = [(core.BUNDLE, lambda s=self.bundle[int(i)]: s) for i in picks]
            else:
                src = self.by_label[src_label]
                pairs = [(src.name, lambda i=i: src.load(i)) for i in picks]
            work = lambda: core.transfer(pairs, dst, log)
            finish = lambda n: tr('Selesai: {n} pesan disalin.', n=n)
        self.go.configure(text=tr('Sedang berjalan…'))
        self.go.state(['disabled'])

        def done(result):
            self.go.state(['!disabled'])
            self.refresh_go()
            self.write(self.transfer_log, finish(result))
        self.run(work, done)

    # --- Auto sync ---

    def build_auto(self):
        page = self.add_page('auto', tr('↻   Sinkron Otomatis'), tr('Sinkron Otomatis'),
                             tr('Salin pesan baru antar harness secara berkala. Hanya menambah, tidak pernah mengubah atau menghapus.'))
        body, _ = self.card(page, tr('Rute'), '1')
        line, self.route_src, self.route_dst = self.pair(body, 24)
        self.both = tk.BooleanVar(value=False)
        ttk.Checkbutton(line, text=tr('Dua arah'), variable=self.both).pack(side='left')
        ttk.Button(line, text=tr('+  Tambah rute'), command=self.add_route).pack(side='right')
        self.routes = tk.Listbox(body, height=4, selectmode='extended', activestyle='none', relief='flat', bd=0, highlightthickness=1,
                                 highlightbackground=LINE, highlightcolor=ACCENT, bg=CARD, fg=TEXT, selectbackground=ACCENT_SOFT,
                                 selectforeground=TEXT, font='TkDefaultFont')
        self.routes.pack(fill='x', pady=(12, 8))
        line = ttk.Frame(body)
        line.pack(fill='x')
        self.route_hint = tk.StringVar()
        ttk.Label(line, textvariable=self.route_hint, style='Muted.TLabel').pack(side='left')
        ttk.Button(line, text=tr('Hapus terpilih'), command=self.del_route).pack(side='right')

        body, _ = self.card(page, tr('Jadwal'), '2')
        line = ttk.Frame(body)
        line.pack(fill='x')
        self.interval = tk.StringVar(value=str(self.cfg.get('interval_min', 5)))
        self.since = tk.StringVar(value=str(self.cfg.get('since_days', 7)))
        for before, var, low, high, after in ((tr('Periksa setiap'), self.interval, 1, 1440, tr('menit')),
                                              (tr('Hanya sesi yang berubah dalam'), self.since, 0, 3650, tr('hari terakhir (0 = semua)'))):
            ttk.Label(line, text=before).pack(side='left')
            spin = ttk.Spinbox(line, from_=low, to=high, width=5, textvariable=var, command=self.save)
            spin.pack(side='left', padx=6)
            spin.bind('<FocusOut>', lambda _event: self.save())
            ttk.Label(line, text=after).pack(side='left', padx=(0, 32))
        self.login = tk.BooleanVar(value=login_enabled())
        ttk.Checkbutton(body, text=tr('Mulai sinkron otomatis saat login Windows'), variable=self.login,
                        command=lambda: set_login(self.login.get())).pack(anchor='w', pady=(12, 0))

        body, _ = self.card(page, tr('Jalankan'), '3')
        line = ttk.Frame(body)
        line.pack(fill='x')
        ttk.Button(line, text=tr('Pratinjau'), command=lambda: self.sync(True)).pack(side='left')
        ttk.Button(line, text=tr('Sinkron sekarang'), command=lambda: self.sync(False)).pack(side='left', padx=8)
        self.watch_btn = ttk.Button(line, text=tr('▶  Mulai otomatis'), style='Accent.TButton', command=self.toggle_watch)
        self.watch_btn.pack(side='right')
        self.wrap(ttk.Label(body, style='Muted.TLabel', text=tr('Pratinjau menghitung yang akan disalin tanpa menulis apa pun; coba dulu sebelum '
                            'sinkron pertama. Sinkron otomatis tetap jalan setelah jendela ini ditutup.'))).pack(fill='x', pady=(10, 0))

        body, _ = self.card(page, tr('Aktivitas'), fill='both', expand=True, pady=0)
        frame, self.auto_log = self.log_box(body, 5)
        frame.pack(fill='both', expand=True)

    def route_name(self, name):
        adapter = self.adapters.get(name)
        return adapter.label if adapter else name

    def show_routes(self):
        self.routes.delete(0, 'end')
        for src, dst in self.cfg['routes']:
            self.routes.insert('end', f'  {self.route_name(src)}   →   {self.route_name(dst)}')
        count = len(self.cfg['routes'])
        self.route_hint.set(tr('{n} rute. Pilih rute lalu Hapus terpilih untuk melepasnya.', n=count) if count else
                            tr('Belum ada rute. Pilih Dari dan Ke di atas, lalu Tambah rute.'))

    def add_route(self):
        src, dst = self.by_label.get(self.route_src.get()), self.by_label.get(self.route_dst.get())
        if not src or not dst or src is dst:
            messagebox.showinfo('Session Bridge', tr('Pilih dua harness yang berbeda.'))
            return
        wanted = [[src.name, dst.name]] + ([[dst.name, src.name]] if self.both.get() and src.can_write else [])
        self.cfg['routes'] += [r for r in wanted if r not in self.cfg['routes']]
        self.save()
        self.show_routes()

    def del_route(self):
        for index in reversed(self.routes.curselection()):
            del self.cfg['routes'][index]
        self.save()
        self.show_routes()

    def save(self):
        for key, var, floor in (('interval_min', self.interval, 1), ('since_days', self.since, 0)):
            with contextlib.suppress(ValueError):
                self.cfg[key] = max(floor, int(var.get()))
            var.set(str(self.cfg[key]))
        core.save_cfg(self.cfg)

    def sync(self, dry):
        self.save()
        if not self.cfg['routes']:
            messagebox.showinfo('Session Bridge', tr('Tambahkan rute dulu.'))
            return
        cfg, found, log = dict(self.cfg), self.adapters, self.logger(self.auto_log)
        self.write(self.auto_log, tr('Pratinjau…') if dry else tr('Sinkron…'))

        def done(results):
            if results is None:
                text = tr('Sinkron lain masih berjalan. Coba lagi sebentar.')
            elif not any(r[5] for r in results):
                text = tr('Tidak ada yang perlu disalin.')
            else:
                text = tr('Akan disalin: {t}', t=tally(results)) if dry else tr('Selesai: {t}', t=tally(results))
            self.write(self.auto_log, text)
        self.run(lambda: core.sync_all(cfg, found, dry=dry, log=log), done)

    def toggle_watch(self):
        (self.stop_watch if self.want else self.start_watch)()

    def start_watch(self):
        self.save()
        if not self.cfg['routes']:
            messagebox.showinfo('Session Bridge', tr('Tambahkan rute dulu.'))
            return
        if not watch_running():
            spawn_watch()
            self.status.set(tr('Memulai sinkron otomatis…'))
            self.watch_btn.state(['disabled'])
            self.want, self.settle = True, time.time() + 30

    def stop_watch(self):
        if watch_running():
            (core.state_dir() / 'STOP').touch()
            self.status.set(tr('Menghentikan sinkron otomatis…'))
            self.watch_btn.state(['disabled'])
            self.want, self.settle = False, time.time() + 300  # A pass in progress finishes first.

    def poll(self):
        if not self.alive:
            return
        running = watch_running()
        if running == self.want or time.time() > self.settle:
            self.want = running
            self.status.set(tr('●  Sinkron otomatis aktif') if running else tr('○  Sinkron otomatis mati'))
            self.pill.configure(style='On.TLabel' if running else 'Off.TLabel')
            self.watch_btn.configure(text=tr('■  Hentikan otomatis') if running else tr('▶  Mulai otomatis'), style='TButton' if running else 'Accent.TButton')
            self.watch_btn.state(['!disabled'])
        path = core.state_dir() / 'bridge.log'
        with contextlib.suppress(OSError):
            size = path.stat().st_size
            if size < self.log_pos:
                self.log_pos = 0
            if size > self.log_pos:
                with open(path, 'rb') as handle:
                    handle.seek(self.log_pos)
                    data = handle.read()
                self.log_pos = size
                self.write(self.auto_log, data.decode('utf-8', 'replace').rstrip())
        self.root.after(2000, self.poll)

    # --- Harnesses ---

    def build_harness(self):
        page = self.add_page('harness', tr('▤   Harness'), tr('Harness Terdeteksi'),
                             tr('Harness AI yang ditemukan di perangkat ini, berapa sesinya, dan apakah bisa ditulisi.'))
        body, _ = self.card(page, tr('DeepSeek dan data sensitif'), side='bottom', pady=0)
        self.wrap(ttk.Label(body, text=tr(DEEPSEEK), style='Muted.TLabel')).pack(fill='x')
        body, head = self.card(page, tr('Daftar harness'), fill='both', expand=True)
        self.harness_status = tk.StringVar()
        ttk.Label(head, textvariable=self.harness_status, style='Muted.TLabel').pack(side='left', padx=10)
        self.detect_btn = ttk.Button(head, text=tr('Deteksi ulang'), command=self.detect)
        self.detect_btn.pack(side='right')
        self.harness_note = tk.StringVar(value=tr('Pilih satu baris untuk membaca lokasi dan catatan lengkapnya.'))
        self.wrap(ttk.Label(body, textvariable=self.harness_note, style='Muted.TLabel')).pack(side='bottom', fill='x', pady=(10, 0))
        columns = (('Harness', 170), (tr('Sesi'), 60), (tr('Akses'), 110), (tr('Lokasi'), 280), (tr('Catatan'), 320))
        frame, self.harnesses = self.table(body, columns, 'browse', 10)
        frame.pack(fill='both', expand=True)
        self.harnesses.tag_configure('ro', foreground=MUTED)
        self.harnesses.bind('<<TreeviewSelect>>', lambda _event: self.on_harness())

    def on_harness(self):
        values = self.harnesses.item(self.harnesses.focus(), 'values')
        if values:
            self.harness_note.set(f'{values[0]}  ·  {values[3]}\n{values[4]}')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
        logging.handlers.RotatingFileHandler(core.state_dir() / 'bridge.log', maxBytes=1_000_000, backupCount=1, encoding='utf-8', delay=True)])
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding='utf-8')
    core.LANG = core.load_cfg().get('lang', 'id')
    if len(sys.argv) > 1:
        return cli(sys.argv[1])
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
