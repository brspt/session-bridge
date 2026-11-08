"""Canonical session model and the anti-echo sync engine."""
import contextlib
import hashlib
import json
import logging
import msvcrt
import os
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# ponytail: tool blobs capped when written to another harness so resume doesn't choke; bundles keep full content.
TOOL_CAP = 8000
ACTIVE_SECS = 60  # A file touched this recently is probably in use: retry next tick.
BUNDLE_FORMAT = 'session-bridge/1'
BUNDLE = 'bundle'


def state_dir():
    path = Path(os.environ.get('SESSION_BRIDGE_DIR') or Path.home() / '.session-bridge')
    path.mkdir(parents=True, exist_ok=True)
    return path


def codex_root():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


@dataclass
class Msg:
    role: str  # user | assistant | tool
    text: str = ''
    ts: float = 0.0
    tool: dict = None  # name, input, output, id, status


@dataclass
class Session:
    harness: str
    id: str
    title: str = ''
    cwd: str = ''
    created: float = 0.0
    updated: float = 0.0
    model: str = ''
    messages: list = field(default_factory=list)
    origin: dict = None


@dataclass
class Meta:
    id: str
    title: str
    updated: float
    cwd: str = ''
    fingerprint: str = ''


def cap(text, limit=TOOL_CAP):
    """Idempotent: capping capped text changes nothing, so digests stay stable."""
    text = str(text or '')
    return text if len(text) <= limit else text[:limit - 24] + '\n[dipotong session-bridge]'


def flat(msg):
    """Plain-text form of a message. Harnesses that can't take foreign tool blocks receive this."""
    if msg.role != 'tool':
        return msg.text or ''
    tool = msg.tool or {}
    arg = tool.get('input')
    arg = arg if isinstance(arg, str) or arg is None else json.dumps(arg, ensure_ascii=False, sort_keys=True)
    return f"[tool {tool.get('name') or '?'}] {cap(arg)}\n{cap(tool.get('output'))}".strip()


def clean(msgs):
    return [m for m in msgs if flat(m).strip()]


def flatten(msgs):
    return [Msg('user' if m.role == 'user' else 'assistant', flat(m), m.ts) for m in clean(msgs)]


def digests(msgs):
    """Stable per message across harnesses: tool blocks and their flat copies hash alike."""
    count, out = Counter(), []
    for msg in msgs:
        key = ('user' if msg.role == 'user' else 'assistant') + '|' + ' '.join(flat(msg).split())
        count[key] += 1
        out.append(hashlib.sha1(f'{key}|{count[key]}'.encode('utf-8', 'replace')).hexdigest()[:20])
    return out


def group_id(harness, session_id):
    return hashlib.sha1(f'{harness}:{session_id}'.encode()).hexdigest()[:20]


class State:
    def __init__(self):
        self.db = sqlite3.connect(state_dir() / 'state.sqlite', timeout=30)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS links(group_id TEXT, harness TEXT, session_id TEXT,
                needs_baseline INT DEFAULT 0, PRIMARY KEY(harness, session_id));
            CREATE INDEX IF NOT EXISTS links_group ON links(group_id);
            CREATE TABLE IF NOT EXISTS seen(group_id TEXT, harness TEXT, digest TEXT,
                PRIMARY KEY(group_id, harness, digest)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS fingerprints(src TEXT, session_id TEXT, dst TEXT, fp TEXT,
                PRIMARY KEY(src, session_id, dst)) WITHOUT ROWID;
        ''')

    def group_of(self, harness, session_id):
        row = self.db.execute('SELECT group_id FROM links WHERE harness=? AND session_id=?',
                              (harness, session_id)).fetchone()
        return row[0] if row else None

    def peer(self, group, harness):
        return self.db.execute('SELECT session_id, needs_baseline FROM links WHERE group_id=? AND harness=?',
                               (group, harness)).fetchone()

    def link(self, group, harness, session_id, baseline=0):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?,?)', (group, harness, session_id, baseline))

    def link_pair(self, a, b):
        """Join two sessions that already share history. Nothing is copied: baseline is set on first contact."""
        group = self.group_of(*a) or self.group_of(*b) or group_id(*a)
        other = self.group_of(*b)
        with self.db:
            if other and other != group:
                self.db.execute('UPDATE links SET group_id=? WHERE group_id=?', (group, other))
                self.db.execute('UPDATE OR IGNORE seen SET group_id=? WHERE group_id=?', (group, other))
            for harness, session_id in (a, b):
                self.db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?,1)', (group, harness, session_id))

    def settled(self, group, harness):
        with self.db:
            self.db.execute('UPDATE links SET needs_baseline=0 WHERE group_id=? AND harness=?', (group, harness))

    def seen(self, group, *harnesses):
        marks = ','.join('?' * len(harnesses))
        return {r[0] for r in self.db.execute(
            f'SELECT digest FROM seen WHERE group_id=? AND harness IN ({marks})', (group, *harnesses))}

    def mark(self, group, harness, values):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO seen VALUES(?,?,?)', [(group, harness, v) for v in values])

    def fingerprint(self, src, session_id, dst):
        row = self.db.execute('SELECT fp FROM fingerprints WHERE src=? AND session_id=? AND dst=?',
                              (src, session_id, dst)).fetchone()
        return row[0] if row else None

    def set_fingerprint(self, src, session_id, dst, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO fingerprints VALUES(?,?,?,?)', (src, session_id, dst, value))


@contextlib.contextmanager
def single(name='sync.lock', wait=0):
    """Non-blocking process lock. Yields False when another process holds it."""
    with open(state_dir() / name, 'a+') as handle:
        deadline = time.time() + wait
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() >= deadline:
                    yield False
                    return
                time.sleep(1)
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def backup_db(path, label):
    """One backup per harness per day, before the first write to its live database."""
    folder = state_dir() / 'backups'
    folder.mkdir(exist_ok=True)
    target = folder / f'{label}-{datetime.now():%Y%m%d}.sqlite'
    if target.exists():
        return
    with contextlib.closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=15)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    for old in sorted(folder.glob(f'{label}-*.sqlite'))[:-7]:
        old.unlink()


# --- Coexistence with the older one-way Codex -> Delta mirror (delta-sync) ---

def read_mirror():
    root = codex_root() / 'integrations' / 'delta-sync'
    path = root / 'sync-state.sqlite'
    if not path.exists():
        return None
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
            rows = conn.execute('SELECT codex_id, delta_id FROM threads WHERE delta_id IS NOT NULL').fetchall()
    except sqlite3.Error:
        return None
    return {'pairs': [r for r in rows if not r[0].startswith('project:')],
            'delta': {r[1] for r in rows},
            'active': not (root / 'STOP').exists()}


def effective_routes(routes, mirror):
    """delta-sync already carries Codex -> Delta, including threads this program adds to Codex."""
    keep, notes = [], []
    active = bool(mirror and mirror['active'])
    for src, dst in routes:
        if active and dst == 'delta' and src == 'codex':
            notes.append(tr('Rute codex→delta dilewati: sudah ditangani delta-sync.'))
        elif active and dst == 'delta' and (src, 'codex') in routes:
            notes.append(tr('Rute {src}→delta dilewati: {src}→codex lalu delta-sync sudah membawanya.', src=src))
        else:
            keep.append((src, dst))
    return keep, notes


def mirror_skip(state, mirror, src, session_id, dst):
    """Why this session must be left to delta-sync, or '' when it is ours to copy."""
    if not mirror:
        return ''
    group = state.group_of(src, session_id)
    delta_peer = state.peer(group, 'delta') if group else None
    if src == 'delta' and session_id in mirror['delta']:
        return tr('thread cermin Codex; Codex→tujuan sudah membawanya')
    if dst == 'delta' and delta_peer and delta_peer[0] in mirror['delta']:
        return tr('thread Delta milik delta-sync')
    if not mirror['active']:
        return ''
    if src == 'codex' and dst == 'delta':
        return tr('sudah ditangani delta-sync')
    if dst == 'delta' and not delta_peer and group and state.peer(group, 'codex'):
        return tr('delta-sync akan membawanya lewat Codex')
    if src == 'delta' and dst == 'codex' and not (group and state.peer(group, 'codex')):
        return tr('delta-sync akan mencerminkannya balik jadi thread ganda')
    return ''


def adopt(state, mirror):
    """Register links other tools already made, so their sessions never get duplicated."""
    path = codex_root() / 'external_agent_session_imports.json'
    try:
        for record in json.loads(path.read_text(encoding='utf-8')).get('records', []):
            source, thread = Path(str(record.get('source_path', ''))), record.get('imported_thread_id')
            if thread and source.suffix == '.jsonl' and '.claude' in source.parts:
                state.link_pair(('claude', source.stem), ('codex', thread))
    except (OSError, ValueError, AttributeError):
        pass
    for codex_id, delta_id in (mirror or {}).get('pairs', ()):
        state.link_pair(('codex', codex_id), ('delta', delta_id))


# --- Engine ---

def sync_session(state, session, src_name, dst, dry=False, force=False):
    """Copy what dst lacks. Returns (action, message count); never edits or removes existing data."""
    msgs = clean(session.messages)
    if not msgs:
        return 'kosong', 0
    digs = digests(msgs)
    origin = session.origin or {}
    group = state.group_of(src_name, session.id) or origin.get('group') or group_id(src_name, session.id)
    peer = state.peer(group, dst.name)
    if not peer and origin.get('harness') == dst.name and dst.has(origin.get('id')):
        peer = (origin['id'], 1)  # A bundle returning to the machine that exported it.
        if not dry:
            state.link(group, dst.name, origin['id'], 1)
    if peer and peer[1]:
        if dry:
            return 'adopsi', 0
        state.link(group, src_name, session.id)
        own = digests(clean(dst.load(peer[0]).messages))
        state.mark(group, src_name, digs)
        state.mark(group, dst.name, digs + own)
        state.mark(group, '*', own)
        state.settled(group, dst.name)
        return 'adopsi', 0
    have = state.seen(group, dst.name, '*')
    missing = [m for m, d in zip(msgs, digs) if d not in have]
    written = [d for d in digs if d not in have]
    if not missing:
        if not dry:
            state.link(group, src_name, session.id)
            state.mark(group, src_name, digs)
        return 'tetap', 0
    if peer and not force and dst.busy(peer[0]):
        return 'sibuk', 0
    if dry:
        return ('tambah' if peer else 'baru'), len(missing)
    if src_name != BUNDLE:
        state.link(group, src_name, session.id)
    state.mark(group, src_name, digs)
    if peer:
        target = peer[0]
        before = set(digests(clean(dst.load(target).messages)))
        dst.append(target, missing)
    else:
        before = set()
        copy = Session(**{**session.__dict__, 'messages': missing, 'origin': {**origin, 'group': group}})
        target = dst.create(copy)
        state.link(group, dst.name, target)
    state.mark(group, dst.name, written)
    with contextlib.suppress(Exception):  # Read-back only protects against echo; the write already succeeded.
        after = digests(clean(dst.load(target).messages))
        state.mark(group, dst.name, after)
        # ponytail: a reformatted copy is echo-blocked for every route, so a chain A->B->C loses it on C; list A->C too.
        state.mark(group, '*', set(after) - before - set(written))
    return ('tambah' if peer else 'baru'), len(missing)


def sync_route(state, src, dst, since_days, mirror, dry=False, log=lambda _: None):
    results = []
    cutoff = time.time() - since_days * 86400 if since_days else 0
    for meta in src.list():
        # A group link alone isn't enough: adopt() links almost everything, and old history must stay home.
        group = state.group_of(src.name, meta.id)
        if meta.updated < cutoff and not (group and state.peer(group, dst.name)):
            continue
        # ponytail: replies typed in Delta on mirrored threads aren't carried; delta-sync is one-way.
        if mirror_skip(state, mirror, src.name, meta.id, dst.name):
            continue
        if time.time() - meta.updated < ACTIVE_SECS:
            continue
        try:
            print_fp = src.fingerprint(meta)
            if state.fingerprint(src.name, meta.id, dst.name) == print_fp:
                continue
            action, count = sync_session(state, src.load(meta.id), src.name, dst, dry)
        except Exception as error:  # One broken session must not stop the rest of the route.
            logging.warning('%s %s -> %s failed: %s', src.name, meta.id, dst.name, type(error).__name__)
            log(tr('GAGAL {what}: {error}', what=f'{src.name}→{dst.name} {meta.title[:50]}', error=tr(str(error))))
            continue
        if not dry and action != 'sibuk':
            state.set_fingerprint(src.name, meta.id, dst.name, print_fp)
        if count or action == 'adopsi':
            results.append((src.name, dst.name, meta.id, meta.title, action, count))
            logging.info('%s %s -> %s: %s %d', src.name, meta.id, dst.name, action, count)
            log(f'{src.name}→{dst.name} [{tr(action)} {count}] {meta.title[:70]}')
    return results


def sync_all(cfg, adapters, dry=False, log=lambda _: None):
    """One pass over the configured routes. Returns None when another sync holds the lock."""
    with single() as free:
        if not free:
            return None
        mirror = read_mirror()
        routes, notes = effective_routes([tuple(r) for r in cfg.get('routes', [])], mirror)
        for note in notes:
            logging.info(note)
            log(note)
        state = State()
        adopt(state, mirror)
        results = []
        for src_name, dst_name in routes:
            src, dst = adapters.get(src_name), adapters.get(dst_name)
            if not src or not dst or not dst.can_write:
                log(tr('Rute {route} dilewati: harness tidak ada atau baca-saja.', route=f'{src_name}→{dst_name}'))
                continue
            try:
                results += sync_route(state, src, dst, cfg.get('since_days', 7), mirror, dry, log)
            except Exception as error:
                logging.warning('route %s -> %s failed: %s', src_name, dst_name, type(error).__name__)
                log(tr('GAGAL rute {what}: {error}', what=f'{src_name}→{dst_name}', error=tr(str(error))))
        return results


def transfer(sessions, dst, log=lambda _: None):
    """Manual import/export of chosen sessions. `sessions` yields (src_name, loader) pairs."""
    with single(wait=30) as free:
        if not free:
            raise RuntimeError('Sinkron lain masih berjalan. Coba lagi sebentar.')
        mirror = read_mirror()
        state = State()
        adopt(state, mirror)
        total = 0
        for src_name, load in sessions:
            try:
                session = load()
                reason = mirror_skip(state, mirror, src_name, session.id, dst.name)
                if reason:
                    log(f'[{tr("lewati")}] {session.title[:60]}: {reason}')
                    continue
                action, count = sync_session(state, session, src_name, dst, force=True)
            except Exception as error:
                logging.warning('transfer %s -> %s failed: %s', src_name, dst.name, type(error).__name__)
                log(tr('GAGAL: {error}', error=tr(str(error))))
                continue
            total += count
            log(f'[{tr(action)} {count}] {session.title[:70]}')
        return total


def export_bundle(src, ids, path, log=lambda _: None):
    state = State()
    sessions = []
    for session_id in ids:
        try:
            session = src.load(session_id)
        except Exception as error:
            logging.warning('export %s failed: %s', src.name, type(error).__name__)
            log(tr('GAGAL: {error}', error=tr(str(error))))
            continue
        session.origin = {'harness': src.name, 'id': session_id,
                          'group': state.group_of(src.name, session_id) or group_id(src.name, session_id)}
        sessions.append(asdict(session))
        log(f'[{tr("ekspor")} {len(session.messages)}] {session.title[:70]}')
    temp = Path(str(path) + '.tmp')
    temp.write_text(json.dumps({'format': BUNDLE_FORMAT, 'exported_at': time.time(), 'sessions': sessions},
                               ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temp, path)
    return len(sessions)


def load_bundle(path):
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(doc, dict) or doc.get('format') != BUNDLE_FORMAT:
        raise ValueError('Bukan file bundle Session Bridge.')
    sessions = []
    for item in doc.get('sessions', []):
        msgs = [Msg(str(m.get('role')), str(m.get('text') or ''), float(m.get('ts') or 0), m.get('tool'))
                for m in item.get('messages', []) if m.get('role') in ('user', 'assistant', 'tool')]
        origin = item.get('origin') if isinstance(item.get('origin'), dict) else None
        sessions.append(Session(str(item.get('harness')), str(item.get('id')), str(item.get('title') or ''),
                                str(item.get('cwd') or ''), float(item.get('created') or 0),
                                float(item.get('updated') or 0), str(item.get('model') or ''), msgs, origin))
    return sessions


def load_cfg():
    try:
        return json.loads((state_dir() / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'routes': [], 'interval_min': 5, 'since_days': 7}


def save_cfg(cfg):
    (state_dir() / 'config.json').write_text(json.dumps(cfg, indent=1), encoding='utf-8')


# Language: Indonesian text is the key, so code reads as before and a missing entry falls back to Indonesian.
LANG = 'id'  # Set from config.json "lang" by app.py.
EN = {
    # Engine log and errors
    'Rute codex→delta dilewati: sudah ditangani delta-sync.': 'Route codex→delta skipped: delta-sync already handles it.',
    'Rute {src}→delta dilewati: {src}→codex lalu delta-sync sudah membawanya.':
        'Route {src}→delta skipped: {src}→codex plus delta-sync already carries it.',
    'thread cermin Codex; Codex→tujuan sudah membawanya': 'Codex mirror thread; Codex→destination already carries it',
    'thread Delta milik delta-sync': 'Delta thread owned by delta-sync',
    'sudah ditangani delta-sync': 'already handled by delta-sync',
    'delta-sync akan membawanya lewat Codex': 'delta-sync will carry it via Codex',
    'delta-sync akan mencerminkannya balik jadi thread ganda': 'delta-sync would mirror it back as a duplicate thread',
    'GAGAL {what}: {error}': 'FAILED {what}: {error}',
    'GAGAL rute {what}: {error}': 'FAILED route {what}: {error}',
    'GAGAL: {error}': 'FAILED: {error}',
    'Rute {route} dilewati: harness tidak ada atau baca-saja.': 'Route {route} skipped: harness missing or read-only.',
    'Sinkron lain masih berjalan. Coba lagi sebentar.': 'Another sync is still running. Try again shortly.',
    'Sinkron lain masih berjalan.': 'Another sync is still running.',
    'Bukan file bundle Session Bridge.': 'Not a Session Bridge bundle file.',
    'tambah': 'add', 'baru': 'new', 'sibuk': 'busy', 'adopsi': 'adopt', 'tetap': 'same', 'lewati': 'skip', 'ekspor': 'export',
    # Adapters
    '{label} baca-saja.': '{label} is read-only.',
    '{label} (terdeteksi)': '{label} (detected)',
    'gagal: {error}': 'failed: {error}',
    'Kontrak perekaman Delta tidak didukung.': 'Unsupported Delta recording contract.',
    'Masuk ke Delta dulu.': 'Sign in to Delta first.',
    'Akun Delta berubah; ditunda untuk menjaga privasi percakapan.': 'Delta account changed; postponed to keep conversations private.',
    'Tanda terima Delta tidak lengkap.': 'Incomplete Delta receipt.',
    'Organisasi Delta tidak ditemukan.': 'Delta organization not found.',
    'Termasuk sesi profil DeepSeek/lainnya yang berjalan lewat Claude Code.': 'Includes DeepSeek and other profile sessions run through Claude Code.',
    'Lewat CLI resmi; thread baru selalu privat dan terikat ke akun yang pertama dipakai.':
        'Through the official CLI; new threads are always private and tied to the account used first.',
    'Tulis lewat `opencode import` (upsert resmi). Termasuk sesi model DeepSeek lewat OpenCode.':
        'Writes through `opencode import` (official upsert). Includes DeepSeek model sessions run through OpenCode.',
    'Tulis berupa baris baru di state.db; database dicadangkan dulu.': 'Writes new rows to state.db; the database is backed up first.',
    'Baca-saja: format tulis tak bisa diverifikasi tanpa CLI Copilot.': 'Read-only: the write format cannot be verified without the Copilot CLI.',
    'Baca-saja: baris percakapan butuh konfigurasi agen yang tak bisa diverifikasi tanpa aplikasinya.':
        'Read-only: conversation rows need an agent configuration that cannot be verified without the app.',
    'Baca-saja lewat indeks teks; langkah aslinya protobuf tanpa skema.': 'Read-only through the text index; the real steps are schemaless protobuf.',
    'Baca-saja, belum diverifikasi: perangkat ini belum punya thread Zed.': 'Read-only, unverified: this device has no Zed threads yet.',
    'Baca-saja, hanya sesi agen. Chat utamanya tersimpan di IndexedDB dan tidak didukung.':
        'Read-only, agent sessions only. The main chats live in IndexedDB, which is not supported.',
    'Baca-saja, terdeteksi otomatis. Format ditebak dari isi file.': 'Read-only, detected automatically. Format guessed from file contents.',
    'Baca-saja, terdeteksi otomatis. Format ditebak dari skema tabel.': 'Read-only, detected automatically. Format guessed from the table schema.',
    # Window
    'File bundle (.json)': 'Bundle file (.json)',
    'Export: sesi diambil DARI harness asal, lalu disalin ke harness lain atau disimpan jadi file bundle (.json) '
    'untuk cadangan atau pindah PC. Sesi asli tidak berubah.':
        'Export: sessions are taken FROM the source harness, then copied to another harness or saved as a bundle file (.json) '
        'for a backup or another PC. The original sessions stay as they are.',
    'Import: sesi dimasukkan KE harness tujuan, diambil dari harness lain atau dari file bundle (.json) '
    'hasil Export. Antar dua harness di PC ini, Export dan Import hasilnya sama.':
        'Import: sessions are put INTO the target harness, from another harness or from a bundle file (.json) '
        'made by Export. Between two harnesses on this PC, Export and Import do the same thing.',
    'Aplikasi Claude desktop: tutup lalu buka lagi aplikasinya, sesi muncul di tab Code. '
    'Terminal: jalankan claude --resume dari folder kerja sesi itu.':
        'Claude desktop app: quit and reopen it, the session then shows in the Code tab. '
        'Terminal: run claude --resume from that session\'s working folder.',
    'Memuat sesi dari {h}…': 'Loading sessions from {h}…',
    'DeepSeek lewat Claude Code atau OpenCode tersimpan di store harness itu, jadi ikut terbawa. '
    'Harness lain dengan folder sendiri muncul otomatis di sini sebagai baca-saja.\n'
    'File bundle dan salinan sesi memuat isi chat apa adanya, termasuk rahasia yang pernah tertulis di chat.':
        'DeepSeek through Claude Code or OpenCode is stored in that harness, so it comes along. '
        'Other harnesses with their own folder show up here automatically as read-only.\n'
        'Bundle files and session copies hold the chats as they are, including any secret ever written in a chat.',
    '{s} sesi, {m} pesan.': '{s} sessions, {m} messages.',
    'Opsi tidak dikenal: {flag}': 'Unknown option: {flag}',
    'Pindah dan sinkron sesi\nantar harness AI.': 'Move and sync sessions\nbetween AI harnesses.',
    'Bahasa': 'Language',
    'Dari': 'From', 'Ke': 'To',
    'Mendeteksi harness…': 'Detecting harnesses…',
    '{n} harness, {w} bisa ditulis': '{n} harnesses, {w} writable',
    '✓ baca + tulis': '✓ read + write', 'baca-saja': 'read-only',
    'Salin sesi sekali jalan ke harness lain, atau simpan dan buka lewat file bundle.':
        'Copy sessions once to another harness, or save and open them as a bundle file.',
    'Pilih arah': 'Choose direction', 'Aktivitas': 'Activity', 'Pilih sesi': 'Choose sessions',
    'Muat ulang': 'Reload', 'Pilih semua': 'Select all', 'Cari': 'Search', 'Pilih sesi dulu': 'Choose sessions first',
    'Judul': 'Title', 'Terakhir diubah': 'Last modified', 'Folder kerja': 'Working folder',
    'Pilih file bundle': 'Choose bundle file', 'Bundle Session Bridge': 'Session Bridge bundle',
    'Memuat…': 'Loading…', '{x} dari {n} sesi': '{x} of {n} sessions', '{n} sesi': '{n} sessions', 'Belum ada sesi': 'No sessions yet',
    'File berisi isi chat apa adanya. Simpan di tempat aman.': 'The file holds the chats as they are. Keep it somewhere safe.',
    'Sesi yang sudah ada di tujuan tidak diubah atau dihapus.': 'Sessions already at the destination are never changed or deleted.',
    'Simpan {n} sesi ke file  →': 'Save {n} sessions to file  →', 'Salin {n} sesi ke {dst}  →': 'Copy {n} sessions to {dst}  →',
    'Pilih dulu sesi yang akan disalin.': 'Choose the sessions to copy first.',
    'Asal dan tujuan harus berbeda.': 'Source and destination must differ.',
    'Simpan bundle': 'Save bundle',
    'Selesai: {n} sesi tersimpan di {path}. Isinya chat apa adanya; simpan di tempat aman.':
        'Done: {n} sessions saved to {path}. It holds the chats as they are; keep it somewhere safe.',
    'Salin {n} sesi dari {src} ke {dst}?\nData yang sudah ada tidak diubah atau dihapus.':
        'Copy {n} sessions from {src} to {dst}?\nExisting data is never changed or deleted.',
    'Selesai: {n} pesan disalin.': 'Done: {n} messages copied.',
    'Sedang berjalan…': 'Running…',
    '↻   Sinkron Otomatis': '↻   Auto-sync', 'Sinkron Otomatis': 'Auto-sync',
    'Salin pesan baru antar harness secara berkala. Hanya menambah, tidak pernah mengubah atau menghapus.':
        'Copy new messages between harnesses on a schedule. Only adds; never changes or deletes.',
    'Rute': 'Routes', 'Dua arah': 'Both ways', '+  Tambah rute': '+  Add route', 'Hapus terpilih': 'Remove selected',
    'Jadwal': 'Schedule', 'Periksa setiap': 'Check every', 'menit': 'minutes',
    'Hanya sesi yang berubah dalam': 'Only sessions changed in the last', 'hari terakhir (0 = semua)': 'days (0 = all)',
    'Mulai sinkron otomatis saat login Windows': 'Start auto-sync when signing in to Windows',
    'Jalankan': 'Run', 'Pratinjau': 'Preview', 'Sinkron sekarang': 'Sync now',
    '▶  Mulai otomatis': '▶  Start auto-sync', '■  Hentikan otomatis': '■  Stop auto-sync',
    'Pratinjau menghitung yang akan disalin tanpa menulis apa pun; coba dulu sebelum sinkron pertama. '
    'Sinkron otomatis tetap jalan setelah jendela ini ditutup.':
        'Preview counts what would be copied without writing anything; try it before the first sync. '
        'Auto-sync keeps running after this window closes.',
    '{n} rute. Pilih rute lalu Hapus terpilih untuk melepasnya.': '{n} route(s). Select one, then Remove selected to drop it.',
    'Belum ada rute. Pilih Dari dan Ke di atas, lalu Tambah rute.': 'No routes yet. Pick From and To above, then Add route.',
    'Pilih dua harness yang berbeda.': 'Choose two different harnesses.',
    'Tambahkan rute dulu.': 'Add a route first.',
    'Pratinjau…': 'Previewing…', 'Sinkron…': 'Syncing…',
    'Tidak ada yang perlu disalin.': 'Nothing to copy.',
    'Akan disalin: {t}': 'Would copy: {t}', 'Selesai: {t}': 'Done: {t}',
    'Memulai sinkron otomatis…': 'Starting auto-sync…', 'Menghentikan sinkron otomatis…': 'Stopping auto-sync…',
    '●  Sinkron otomatis aktif': '●  Auto-sync on', '○  Sinkron otomatis mati': '○  Auto-sync off',
    '▤   Harness': '▤   Harnesses', 'Harness Terdeteksi': 'Detected Harnesses',
    'Harness AI yang ditemukan di perangkat ini, berapa sesinya, dan apakah bisa ditulisi.':
        'AI harnesses found on this device, how many sessions each has, and whether they can be written.',
    'DeepSeek dan data sensitif': 'DeepSeek and sensitive data', 'Daftar harness': 'Harness list', 'Deteksi ulang': 'Detect again',
    'Pilih satu baris untuk membaca lokasi dan catatan lengkapnya.': 'Select a row to read its full location and notes.',
    'Sesi': 'Sessions', 'Akses': 'Access', 'Lokasi': 'Location', 'Catatan': 'Notes',
}


def tr(text, **values):
    text = EN.get(text, text) if LANG == 'en' else text
    return text.format(**values) if values else text
