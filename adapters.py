"""One adapter per harness store. Writes are additive only; credential files and tables are never opened."""
import contextlib
import io
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import ACTIVE_SECS, TOOL_CAP, Meta, Msg, Session, backup_db, cap, clean, flatten, state_dir, tr

HOME = Path.home()
APPDATA = Path(os.environ.get('APPDATA') or HOME / 'AppData/Roaming')
LOCAL = Path(os.environ.get('LOCALAPPDATA') or HOME / 'AppData/Local')
NO_WINDOW = subprocess.CREATE_NO_WINDOW
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
SECRET = re.compile(r'auth|credential|token|secret|\.env|cookie|password|apikey|api_key', re.I)


def ro(path):
    return sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=15)


def epoch(value):
    """ISO string or s/ms/us/ns number to epoch seconds."""
    if value in (None, ''):
        return 0.0
    if isinstance(value, (int, float)):
        value = float(value)
        while value > 4e10:
            value /= 1000
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def iso(ts=None):
    moment = datetime.fromtimestamp(ts or time.time(), timezone.utc)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def text_of(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(filter(None, (text_of(x) for x in content)))
    if isinstance(content, dict):
        for key in ('text', 'content', 'output', 'Text'):
            if key in content:
                return text_of(content[key])
    return ''


def title_from(msgs):
    for msg in msgs:
        if msg.role == 'user' and msg.text.strip():
            return ' '.join(msg.text.split())[:70]
    return ''


def stream_lines(path):
    """Text lines from a .jsonl or multi-frame .jsonl.zst; tolerates a half-written last line upstream."""
    with open(path, 'rb') as raw:
        if path.suffix == '.zst':
            import zstandard
            source = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)
        else:
            source = raw
        yield from io.TextIOWrapper(source, encoding='utf-8', errors='replace')


def json_lines(path):
    for line in stream_lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def recently_touched(path):
    try:
        return time.time() - Path(path).stat().st_mtime < ACTIVE_SECS
    except OSError:
        return False


class Adapter:
    name = label = ''
    can_write = False
    note = ''
    after = ''  # Where a copy shows up, said after a manual copy into this harness.

    def available(self):
        return False

    def location(self):
        return ''

    def list(self):
        return []

    def load(self, session_id):
        raise NotImplementedError

    def create(self, session):
        raise RuntimeError(tr('{label} baca-saja.', label=self.label))

    def append(self, session_id, msgs):
        raise RuntimeError(tr('{label} baca-saja.', label=self.label))

    def busy(self, session_id):
        return False

    def fingerprint(self, meta):
        return meta.fingerprint

    def has(self, session_id):
        return any(m.id == session_id for m in self.list())


def desktop_index(cli_id, cwd, title):
    """The Claude desktop app's Code tab lists only sessions that have a metadata file in its own store; the CLI needs none.
    Adds one beside the newest existing file (that folder is the signed-in account). Never touches existing files."""
    store = Path(os.environ.get('CLAUDE_DESKTOP_DATA') or LOCAL / 'Claude-Data')
    files = list(store.glob('claude_*/claude-code-sessions/*/*/local_*.json'))
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    like = json.loads(newest.read_text(encoding='utf-8'))
    now = int(time.time() * 1000)
    meta = {'sessionId': f'local_{uuid.uuid4()}', 'cliSessionId': cli_id, 'cwd': cwd, 'originCwd': cwd,
            'createdAt': now, 'lastActivityAt': now, 'lastFocusedAt': now, 'model': like.get('model') or 'claude-opus-5-5',
            'effort': 'high', 'isArchived': False, 'title': title or 'Session Bridge', 'titleSource': 'user',
            'permissionMode': 'acceptEdits', 'enabledMcpTools': like.get('enabledMcpTools') or {},
            'remoteMcpServersConfig': like.get('remoteMcpServersConfig') or [], 'alwaysAllowedReasons': [],
            'sessionPermissionUpdates': []}
    path = newest.parent / f"{meta['sessionId']}.json"
    temp = path.with_suffix('.tmp')  # The app may read the folder mid-write.
    temp.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)
    return path


class Claude(Adapter):
    name, label, can_write = 'claude', 'Claude Code', True
    note = 'Termasuk sesi profil DeepSeek/lainnya yang berjalan lewat Claude Code.'
    after = ('Aplikasi Claude desktop: tutup lalu buka lagi aplikasinya, sesi muncul di tab Code. '
             'Terminal: jalankan claude --resume dari folder kerja sesi itu.')

    @property
    def root(self):
        return Path(os.environ.get('CLAUDE_CONFIG_DIR') or HOME / '.claude') / 'projects'

    def available(self):
        return self.root.is_dir()

    def location(self):
        return str(self.root)

    def _file(self, session_id):
        return next(self.root.glob(f'*/{session_id}.jsonl'), None)

    def list(self):
        out = []
        for path in self.root.glob('*/*.jsonl'):  # Subagent files live one level deeper.
            if not UUID.fullmatch(path.stem):
                continue
            with contextlib.suppress(OSError):
                out.extend(self._summary(path))
        return out

    def _summary(self, path):
        stat = path.stat()
        with open(path, 'rb') as handle:
            chunk = handle.read(262144)
            if stat.st_size > len(chunk):
                handle.seek(max(stat.st_size - 65536, len(chunk)))
                chunk += b'\n' + handle.read()
        custom, first, cwd = '', '', ''
        for line in chunk.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get('type') if isinstance(row, dict) else None
            cwd = cwd or (row.get('cwd') or '' if kind else '')
            if kind == 'custom-title':
                custom = row.get('customTitle') or custom
            elif kind == 'agent-name':
                custom = custom or row.get('agentName') or ''
            elif kind == 'user' and not first and not (row.get('isSidechain') or row.get('isMeta')):
                content = row.get('message', {}).get('content')
                blocks = [b for b in content if isinstance(b, dict) and b.get('type') == 'text'] \
                    if isinstance(content, list) else content
                first = ' '.join(text_of(blocks).split())[:70]
        if not custom and not first:
            return []
        return [Meta(path.stem, custom or first, stat.st_mtime, cwd, f'{stat.st_size}:{stat.st_mtime_ns}')]

    def load(self, session_id):
        path = self._file(session_id)
        if not path:
            raise KeyError(session_id)
        session = Session(self.name, session_id, updated=path.stat().st_mtime)
        tools, custom = {}, ''
        for row in json_lines(path):
            kind = row.get('type')
            if kind == 'custom-title':
                custom = row.get('customTitle') or custom
            elif kind == 'agent-name':
                custom = custom or row.get('agentName') or ''
            if kind not in ('user', 'assistant') or row.get('isSidechain') or row.get('isMeta'):
                continue
            session.cwd = session.cwd or row.get('cwd') or ''
            ts = epoch(row.get('timestamp'))
            session.created = session.created or ts
            message = row.get('message') or {}
            session.model = message.get('model') or session.model
            content = message.get('content')
            if isinstance(content, str):
                session.messages.append(Msg(kind, content, ts))
                continue
            # ponytail: images and thinking blocks aren't carried; add when a target can render them.
            for block in content or []:
                btype = block.get('type')
                if btype == 'text':
                    session.messages.append(Msg(kind, block.get('text', ''), ts))
                elif btype == 'tool_use':
                    tool = Msg('tool', '', ts, {'name': block.get('name'), 'input': block.get('input'), 'output': '',
                                                'id': block.get('id'), 'status': 'pending'})
                    tools[block.get('id')] = tool
                    session.messages.append(tool)
                elif btype == 'tool_result' and block.get('tool_use_id') in tools:
                    tools[block['tool_use_id']].tool.update(output=text_of(block.get('content')),
                                                            status='failed' if block.get('is_error') else 'completed')
        session.title = custom or title_from(session.messages)
        return session

    def _rows(self, session_id, cwd, msgs, parent):
        rows = []
        for msg in flatten(msgs):
            row_id = str(uuid.uuid4())
            base = {'parentUuid': parent, 'isSidechain': False, 'type': msg.role, 'uuid': row_id,
                    'timestamp': iso(msg.ts), 'userType': 'external', 'entrypoint': 'cli', 'cwd': cwd,
                    'sessionId': session_id, 'version': '2.1.296', 'gitBranch': ''}
            if msg.role == 'user':
                base['message'] = {'role': 'user', 'content': msg.text}
            else:
                base['message'] = {'model': 'session-bridge', 'id': 'msg_' + uuid.uuid4().hex, 'type': 'message',
                                   'role': 'assistant', 'content': [{'type': 'text', 'text': msg.text}],
                                   'stop_reason': 'end_turn', 'stop_sequence': None,
                                   'usage': {'input_tokens': 0, 'output_tokens': 0}}
            rows.append(base)
            parent = row_id
        return rows

    def create(self, session):
        cwd = session.cwd if session.cwd and os.path.isdir(session.cwd) else str(HOME)  # Resume fails in a missing folder.
        folder = self.root / re.sub(r'[^a-zA-Z0-9]', '-', cwd)
        folder.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid.uuid4())
        rows = self._rows(session_id, cwd, session.messages, None)
        title = session.title or title_from(session.messages)
        if title:
            rows.append({'type': 'custom-title', 'customTitle': title, 'sessionId': session_id})
        with open(folder / f'{session_id}.jsonl', 'w', encoding='utf-8', newline='\n') as handle:
            handle.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
        with contextlib.suppress(OSError, ValueError):  # The CLI copy stands even if the desktop list can't be told.
            desktop_index(session_id, cwd, title)
        return session_id

    def append(self, session_id, msgs):
        path = self._file(session_id)
        parent, cwd = None, ''
        for row in json_lines(path):
            if row.get('type') in ('user', 'assistant') and not row.get('isSidechain'):
                parent, cwd = row.get('uuid'), row.get('cwd') or cwd
        with open(path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.writelines(json.dumps(r, ensure_ascii=False) + '\n'
                              for r in self._rows(session_id, cwd or str(HOME), msgs, parent))

    def busy(self, session_id):
        path = self._file(session_id)
        return bool(path) and recently_touched(path)


class Codex(Adapter):
    name, label, can_write = 'codex', 'Codex', True
    SEGMENT = re.compile(r'^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-([0-9a-f-]{36})(?:_|\.)')

    @property
    def root(self):
        return Path(os.environ.get('CODEX_HOME') or HOME / '.codex')

    def available(self):
        return (self.root / 'sessions').is_dir() or (self.root / 'state_5.sqlite').exists()

    def location(self):
        return str(self.root)

    def _segments(self):
        found = {}
        for folder in ('sessions', 'archived_sessions'):
            for path in (self.root / folder).rglob('rollout-*.jsonl*'):
                match = self.SEGMENT.match(path.name)
                if match and path.suffix in ('.jsonl', '.zst'):
                    key = path.name.removesuffix('.zst')
                    items = found.setdefault(match[1], {})
                    # During compression both names may exist. Read the uncompressed file once.
                    if key not in items or path.suffix == '.jsonl':
                        items[key] = path
        return {tid: [items[k] for k in sorted(items)] for tid, items in found.items()}

    def _names(self):
        names = {}
        with contextlib.suppress(OSError):
            for row in json_lines(self.root / 'session_index.jsonl'):
                names[row.get('id')] = row.get('thread_name')
        return names

    def list(self):
        rows, names = {}, self._names()
        database = self.root / 'state_5.sqlite'
        if database.exists():
            with contextlib.suppress(sqlite3.Error), contextlib.closing(ro(database)) as conn:
                conn.row_factory = sqlite3.Row
                rows = {r['id']: dict(r) for r in conn.execute('SELECT * FROM threads')}
        out = []
        for tid, paths in self._segments().items():
            row = rows.get(tid, {})
            if str(row.get('source') or '').startswith('{'):
                continue  # Sub-agent threads are part of their parent's work.
            try:
                stats = [p.stat() for p in paths]
            except OSError:
                continue
            title = row.get('name') or names.get(tid) or row.get('title') or row.get('first_user_message') or tid
            newest = max(s.st_mtime for s in stats)
            print_id = f'{sum(s.st_size for s in stats)}:{max(s.st_mtime_ns for s in stats)}'
            out.append(Meta(tid, ' '.join(str(title).split())[:70], newest, row.get('cwd') or '', print_id))
        return out

    def load(self, session_id):
        paths = self._segments().get(session_id)
        if not paths:
            raise KeyError(session_id)
        session = Session(self.name, session_id, updated=max(p.stat().st_mtime for p in paths))
        ordered, tools, turn = [], {}, 0
        for path in paths:
            turn += 1
            for row in json_lines(path):
                kind, body, ts = row.get('type'), row.get('payload') or {}, epoch(row.get('timestamp'))
                if kind == 'session_meta':
                    session.cwd = session.cwd or body.get('cwd') or ''
                    session.created = session.created or ts
                elif kind == 'turn_context':
                    session.model = body.get('model') or session.model
                elif kind == 'event_msg':
                    event = body.get('type')
                    if event == 'task_started':
                        turn += 1
                    elif event == 'item_completed' and body.get('thread_id', session_id) == session_id:
                        item = body.get('item') or {}
                        text = ''.join(b.get('text', '') for b in item.get('content') or [] if isinstance(b, dict))
                        if item.get('type') == 'UserMessage':
                            ordered.append((turn, 'item', Msg('user', text, ts)))
                        elif item.get('type') == 'AgentMessage':
                            ordered.append((turn, 'item', Msg('assistant', text, ts)))
                    elif event == 'user_message':
                        ordered.append((turn, 'legacy', Msg('user', body.get('message', ''), ts)))
                    elif event == 'agent_message':
                        ordered.append((turn, 'legacy', Msg('assistant', body.get('message', ''), ts)))
                elif kind == 'response_item':
                    event = body.get('type')
                    if event in ('function_call', 'custom_tool_call'):
                        tool = Msg('tool', '', ts, {'name': body.get('name'), 'id': body.get('call_id'), 'output': '',
                                                    'input': body.get('arguments', body.get('input')),
                                                    'status': body.get('status')})
                        tools[body.get('call_id')] = tool
                        ordered.append((turn, 'tool', tool))
                    elif event in ('function_call_output', 'custom_tool_call_output') and body.get('call_id') in tools:
                        tools[body['call_id']].tool['output'] = text_of(body.get('output'))
        # A turn may record a message both ways; item_completed wins. Old turns only have the legacy events.
        modern = {t for t, k, _ in ordered if k == 'item'}
        session.messages = [m for t, k, m in ordered if k != 'legacy' or t not in modern]
        session.title = self._names().get(session_id) or title_from(session.messages)
        return session

    @staticmethod
    def _turns(thread_id, msgs, paginated):
        """Verified against the real app-server: legacy events replay at once; paginated threads need item_completed."""
        rows, turn, started = [], None, 0

        def close():
            rows.append(('event_msg', {'type': 'task_complete', 'turn_id': turn, 'last_agent_message': None,
                                       'started_at': started}))
        for msg in flatten(msgs):
            ts = msg.ts or time.time()
            if turn is None or msg.role == 'user':
                if turn:
                    close()
                turn, started = 'bridge-' + uuid.uuid4().hex, int(ts)
                rows.append(('event_msg', {'type': 'task_started', 'turn_id': turn, 'started_at': started,
                                           'model_context_window': None, 'collaboration_mode_kind': 'default'}))
            user = msg.role == 'user'
            if paginated:
                content = ([{'type': 'text', 'text': msg.text, 'text_elements': []}] if user
                           else [{'type': 'Text', 'text': msg.text}])
                item = {'type': 'UserMessage' if user else 'AgentMessage', 'id': str(uuid.uuid4()), 'content': content}
                rows.append(('event_msg', {'type': 'item_completed', 'thread_id': thread_id, 'turn_id': turn,
                                           'item': item, 'completed_at_ms': int(ts * 1000)}))
            elif user:
                rows.append(('event_msg', {'type': 'user_message', 'message': msg.text, 'images': [],
                                           'local_images': [], 'text_elements': []}))
            else:
                rows.append(('event_msg', {'type': 'agent_message', 'message': msg.text}))
            rows.append(('response_item', {'type': 'message', 'role': msg.role, 'content': [
                {'type': 'input_text' if user else 'output_text', 'text': msg.text}]}))
        if turn:
            close()
        return rows

    @staticmethod
    def _encode(rows, start, ts):
        return ''.join(json.dumps({'timestamp': ts, 'ordinal': start + i, 'type': kind, 'payload': body},
                                  ensure_ascii=False) + '\n' for i, (kind, body) in enumerate(rows))

    def create(self, session):
        thread_id, now = str(uuid.uuid4()), datetime.now()
        folder = self.root / 'sessions' / f'{now:%Y/%m/%d}'
        folder.mkdir(parents=True, exist_ok=True)
        ts = iso(session.created or None)
        meta = {'session_id': thread_id, 'id': thread_id, 'timestamp': ts, 'cwd': session.cwd or str(HOME),
                'originator': 'Session Bridge', 'cli_version': '0.162.0-alpha.17.2', 'source': 'vscode',
                'model_provider': 'openai', 'context_window': {'window_id': str(uuid.uuid4())}}
        rows = [('session_meta', meta)] + self._turns(thread_id, session.messages, paginated=False)
        path = folder / f'rollout-{now:%Y-%m-%dT%H-%M-%S}-{thread_id}.jsonl'
        path.write_text(self._encode(rows, 0, ts), encoding='utf-8', newline='\n')
        title = session.title or title_from(session.messages)
        if title:
            with open(self.root / 'session_index.jsonl', 'a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps({'id': thread_id, 'thread_name': title, 'updated_at': iso()},
                                        ensure_ascii=False) + '\n')
        return thread_id

    def append(self, session_id, msgs):
        paths = self._segments()[session_id]
        first = next(json_lines(paths[0]), {})
        paginated = (first.get('payload') or {}).get('history_mode') == 'paginated'
        last = paths[-1]
        start = sum(1 for _ in stream_lines(last))
        data = self._encode(self._turns(session_id, msgs, paginated), start, iso()).encode('utf-8')
        if last.suffix == '.zst':
            import zstandard
            data = zstandard.ZstdCompressor().compress(data)  # Codex reads across concatenated frames.
        with open(last, 'ab') as handle:
            handle.write(data)

    def busy(self, session_id):
        return any(recently_touched(p) for p in self._segments().get(session_id, ()))


class Delta(Adapter):
    name, label, can_write = 'delta', 'Delta', True
    note = 'Lewat CLI resmi; thread baru selalu privat dan terikat ke akun yang pertama dipakai.'
    SOURCE = 'session-bridge'
    MAX_RECORD = 15 * 1024 * 1024  # Delta rejects JSONL records above 16 MiB.
    ESCAPES = {n: '\\u%04x' % n for n in range(0x7f, 0xa0)}

    @property
    def exe(self):
        return LOCAL / 'Programs/Delta/bin/delta.exe'

    def available(self):
        return self.exe.exists()

    def location(self):
        return str(self.exe)

    def run(self, *args, records=None):
        for attempt in range(4):
            done = subprocess.run([str(self.exe), '--json', *args], input=records, encoding='utf-8',
                                  capture_output=True, timeout=180, creationflags=NO_WINDOW)
            try:
                rows = [json.loads(done.stdout)] if done.stdout.strip() else []
            except ValueError:  # `thread append` answers with one receipt per line.
                rows = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
            error = next((r['error'] for r in rows if isinstance(r, dict) and 'error' in r), None)
            if done.returncode == 0 and not error:
                return rows if records is not None else (rows[0] if rows else {})
            if error and error.get('retryable') and attempt < 3:
                time.sleep(min(error.get('retry_after_ms', 1000) / 1000, 30))
                continue
            # Never echo the input: it holds private conversation text.
            raise RuntimeError(f"Delta: {(error or {}).get('code') or done.stderr.strip()[-200:]}")

    def list(self):
        return [Meta(r['id'], r.get('name') or r.get('generated_title') or r['id'],
                     epoch(r.get('created_at_utc'))) for r in self.run('thread', 'list')]

    def fingerprint(self, meta):
        # ponytail: `thread list` has no updated time, so this costs one CLI call per unskipped thread.
        return str(len(self.run('thread', 'show', meta.id, '--format', 'full')[0]['messages']))

    def load(self, session_id):
        doc = self.run('thread', 'show', session_id, '--format', 'full')[0]
        total = len(doc.get('messages', []))
        session = Session(self.name, session_id, doc.get('name') or doc.get('generated_title') or '')
        for start in range(1, total + 1, 20):
            page = self.run('thread', 'show', session_id, '--format', 'messages', '--messages',
                            f'{start}-{min(start + 19, total)}')[0]
            for row in page.get('messages', []):
                ts = epoch(row.get('created_at_utc'))
                session.created = session.created or ts
                session.updated = max(session.updated, ts)
                role = 'user' if row.get('role') == 'user' else 'assistant'
                # ponytail: Delta sometimes renders tool output into the buffer too; a copy may repeat it. Harmless.
                if (row.get('buffer') or '').strip():
                    session.messages.append(Msg(role, row['buffer'], ts))
                for call in row.get('tool_calls') or []:
                    session.messages.append(Msg('tool', '', ts, {k: call.get(k) for k in
                                                                 ('name', 'input', 'output', 'id', 'status')}))
        session.title = session.title or title_from(session.messages)
        return session

    def _ledger(self):
        try:
            return json.loads((state_dir() / 'delta.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'account': None, 'threads': {}}

    def _save(self, ledger):
        (state_dir() / 'delta.json').write_text(json.dumps(ledger), encoding='utf-8')

    def _ready(self, ledger):
        contract = self.run('thread', 'contract')
        if not contract['min'] <= 8 <= contract['max']:
            raise RuntimeError('Kontrak perekaman Delta tidak didukung.')
        auth = self.run('auth', 'status')
        account = (auth.get('account') or {}).get('account_id')
        if auth.get('status') != 'signed_in' or not account:
            raise RuntimeError('Masuk ke Delta dulu.')
        if ledger['account'] not in (None, account):
            raise RuntimeError('Akun Delta berubah; ditunda untuk menjaga privasi percakapan.')
        ledger['account'] = account

    @staticmethod
    def _updates(msgs):
        for msg in clean(msgs):
            key = uuid.uuid4().hex
            if msg.role == 'tool':
                tool = msg.tool or {}
                status = tool.get('status') if tool.get('status') in ('pending', 'in_progress', 'failed') else 'completed'
                raw = tool.get('input')
                encoded = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                raw = cap(encoded) if len(encoded) > TOOL_CAP else raw
                yield {'sessionUpdate': 'tool_call_update', 'toolCallId': tool.get('id') or key,
                       'name': tool.get('name') or 'tool', 'title': tool.get('name') or 'tool', 'status': status,
                       'rawInput': raw, 'rawOutput': cap(tool.get('output')),
                       'content': [{'type': 'content', 'content': {'type': 'text', 'text': cap(tool.get('output'))}}]}
                continue
            kind = 'user_message' if msg.role == 'user' else 'agent_message'
            step = Delta.MAX_RECORD // 6
            for part in range(0, max(len(msg.text), 1), step):
                yield {'sessionUpdate': kind, 'messageId': f'{key}:{part}',
                       'content': [{'type': 'text', 'text': msg.text[part:part + step]}]}

    def _send(self, thread, ledger, updates):
        entry = ledger['threads'][thread]
        rows = [{'schemaVersion': 1, 'sequence': entry['seq'] + i + 1, 'update': u} for i, u in enumerate(updates)]
        for start in range(0, len(rows), 100):
            batch = rows[start:start + 100]
            text = '\n'.join(json.dumps(r, ensure_ascii=False, separators=(',', ':')).translate(self.ESCAPES)
                             for r in batch) + '\n'
            receipts = self.run('thread', 'append', thread, '--source', self.SOURCE, '--external-id', entry['ext'],
                                '--producer-name', 'Session Bridge', '--acknowledge', 'local', records=text)
            accepted = {r.get('sequence') for r in receipts if r.get('disposition') in ('recorded', 'duplicate')}
            if accepted != {r['sequence'] for r in batch}:
                raise RuntimeError('Tanda terima Delta tidak lengkap.')
            entry['seq'] = batch[-1]['sequence']
            self._save(ledger)

    def create(self, session):
        ledger = self._ledger()
        self._ready(ledger)
        # Stable id: a retry after a failed send resumes this thread instead of leaving another one each tick.
        external = uuid.uuid5(uuid.NAMESPACE_URL, f'{session.harness}/{session.id}').hex
        thread = next((t for t, e in ledger['threads'].items() if e['ext'] == external), None)
        if not thread:
            orgs = self.run('org', 'list').get('organizations', [])
            org = next((o['id'] for o in orgs if o.get('current')), None)
            if not org:
                raise RuntimeError('Organisasi Delta tidak ditemukan.')
            thread = self.run('thread', 'create', '--source', self.SOURCE, '--external-id', external,
                              '--organization', org, '--visibility', 'private')['thread_id']
            ledger['threads'][thread] = {'ext': external, 'seq': 0}
            self._save(ledger)
        title = {'sessionUpdate': 'session_info_update', 'title': session.title or title_from(session.messages) or 'Sesi'}
        updates = [title, *self._updates(session.messages)]
        self._send(thread, ledger, updates[ledger['threads'][thread]['seq']:])
        return thread

    def append(self, session_id, msgs):
        ledger = self._ledger()
        self._ready(ledger)
        ledger['threads'].setdefault(session_id, {'ext': session_id, 'seq': 0})
        self._send(session_id, ledger, list(self._updates(msgs)))

    def has(self, session_id):
        return session_id in {r['id'] for r in self.run('thread', 'list')}


class OpenCode(Adapter):
    name, label, can_write = 'opencode', 'OpenCode', True
    note = 'Tulis lewat `opencode import` (upsert resmi). Termasuk sesi model DeepSeek lewat OpenCode.'

    @property
    def db(self):
        base = os.environ.get('XDG_DATA_HOME')
        return (Path(base) if base else HOME / '.local/share') / 'opencode/opencode.db'

    @property
    def exe(self):
        return shutil.which('opencode.cmd') or shutil.which('opencode')

    def available(self):
        return self.db.exists() or bool(self.exe)

    def location(self):
        return str(self.db)

    def list(self):
        if not self.db.exists():
            return []
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('SELECT id, title, time_updated, directory FROM session WHERE parent_id IS NULL').fetchall()
        return [Meta(r[0], r[1] or r[0], epoch(r[2]), r[3] or '', str(r[2])) for r in rows]

    def load(self, session_id):
        with contextlib.closing(ro(self.db)) as conn:
            row = conn.execute('SELECT title, directory, time_created, time_updated FROM session WHERE id=?',
                               (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            session = Session(self.name, session_id, row[0] or '', row[1] or '', epoch(row[2]), epoch(row[3]))
            for message_id, data in conn.execute('SELECT id, data FROM message WHERE session_id=? ORDER BY time_created, id',
                                                 (session_id,)).fetchall():
                info = json.loads(data)
                role = 'user' if info.get('role') == 'user' else 'assistant'
                ts = epoch((info.get('time') or {}).get('created'))
                session.model = (info.get('model') or {}).get('modelID') or info.get('modelID') or session.model
                for (part,) in conn.execute('SELECT data FROM part WHERE message_id=? ORDER BY id', (message_id,)).fetchall():
                    block = json.loads(part)
                    if block.get('type') == 'text' and not block.get('synthetic'):
                        session.messages.append(Msg(role, block.get('text', ''), ts))
                    elif block.get('type') == 'tool':
                        state = block.get('state') or {}
                        session.messages.append(Msg('tool', '', ts, {
                            'name': block.get('tool'), 'input': state.get('input'), 'output': state.get('output') or '',
                            'id': block.get('callID'), 'status': state.get('status')}))
        return session

    def _cli(self, command, cwd):
        done = subprocess.run(f'"{self.exe}" {command}', shell=True, cwd=cwd, capture_output=True, encoding='utf-8',
                              timeout=180, creationflags=NO_WINDOW)
        if done.returncode:
            raise RuntimeError(f'opencode: {done.stderr.strip()[-200:]}')
        return done.stdout

    @staticmethod
    def _new_id(prefix, stamp, descending=False):
        value = int(stamp * 1000) * 0x1000 + random.randrange(0x1000)
        value = (~value if descending else value) & (2 ** 48 - 1)
        tail = ''.join(random.choices(string.ascii_letters + string.digits, k=14))
        return f'{prefix}_{value:012x}{tail}'

    def _docs(self, session_id, msgs, cwd, parent=None):
        out, last = [], parent
        for index, msg in enumerate(flatten(msgs)):
            ts = msg.ts or time.time()
            ms = int(ts * 1000)
            mid = self._new_id('msg', ts + index / 1000)
            info = {'id': mid, 'sessionID': session_id, 'role': msg.role, 'time': {'created': ms}}
            if msg.role == 'user':
                info.update(agent='build', model={'providerID': 'bridge', 'modelID': 'imported'})
                last = mid
            else:
                info.update(parentID=last or mid, modelID='imported', providerID='bridge', mode='build', agent='build',
                            path={'cwd': cwd, 'root': cwd}, cost=0, time={'created': ms, 'completed': ms},
                            tokens={'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}})
            part = {'id': self._new_id('prt', ts + index / 1000), 'sessionID': session_id, 'messageID': mid,
                    'type': 'text', 'text': msg.text}
            out.append({'info': info, 'parts': [part]})
        return out

    def _import(self, doc, cwd):
        cwd = cwd if Path(cwd).is_dir() else str(HOME)  # Import stamps the session with its own cwd.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.json'
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
            self._cli(f'import "{path}"', cwd)

    def create(self, session):
        now = time.time()
        session_id = self._new_id('ses', now, descending=True)
        cwd = session.cwd if session.cwd and Path(session.cwd).is_dir() else str(HOME)
        ms = int(now * 1000)
        info = {'id': session_id, 'slug': 'session-bridge', 'projectID': 'global', 'directory': cwd,
                'title': session.title or title_from(session.messages) or 'Sesi', 'version': '1.18.33',
                'time': {'created': int((session.created or now) * 1000), 'updated': ms}}
        self._import({'info': info, 'messages': self._docs(session_id, session.messages, cwd)}, cwd)
        return session_id

    def append(self, session_id, msgs):
        raw = self._cli(f'export {session_id}', str(HOME))
        doc = json.loads(raw[raw.find('{'):])
        cwd = doc['info'].get('directory') or str(HOME)
        users = [m['info']['id'] for m in doc['messages'] if m['info'].get('role') == 'user']
        doc['messages'] += self._docs(session_id, msgs, cwd, users[-1] if users else None)
        doc['info'].setdefault('time', {})['updated'] = int(time.time() * 1000)
        self._import(doc, cwd)  # Import upserts by id, so existing rows stay as they are.


class Hermes(Adapter):
    name, label, can_write = 'hermes', 'Hermes', True
    note = 'Tulis berupa baris baru di state.db; database dicadangkan dulu.'

    @property
    def db(self):
        return Path(os.environ.get('HERMES_HOME') or HOME / '.hermes') / 'state.db'

    def available(self):
        return self.db.exists()

    def location(self):
        return str(self.db)

    def list(self):
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('''SELECT s.id, s.title, s.cwd, s.started_at, MAX(m.timestamp), COUNT(m.id)
                                   FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
                                   WHERE s.parent_session_id IS NULL GROUP BY s.id''').fetchall()
        return [Meta(r[0], r[1] or r[0], epoch(r[4] or r[3]), r[2] or '', f'{r[5]}:{r[4]}') for r in rows if r[5]]

    def load(self, session_id):
        with contextlib.closing(ro(self.db)) as conn:
            row = conn.execute('SELECT title, cwd, started_at, model FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            session = Session(self.name, session_id, row[0] or '', row[1] or '', epoch(row[2]), model=row[3] or '')
            rows = conn.execute('''SELECT role, content, tool_call_id, tool_calls, tool_name, timestamp FROM messages
                                   WHERE session_id=? AND active=1 ORDER BY timestamp, id''', (session_id,)).fetchall()
        calls = {}
        for role, content, call_id, tool_calls, tool_name, ts in rows:
            if role == 'tool':
                name, args = calls.get(call_id, (tool_name, None))
                session.messages.append(Msg('tool', '', epoch(ts), {'name': name or tool_name, 'input': args,
                                                                    'output': content or '', 'id': call_id}))
                continue
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                for call in json.loads(tool_calls or '[]'):
                    fn = call.get('function') or call
                    calls[call.get('id')] = (fn.get('name'), fn.get('arguments'))
            if role in ('user', 'assistant') and content:
                session.messages.append(Msg(role, content, epoch(ts)))
            session.updated = max(session.updated, epoch(ts))
        session.title = session.title or title_from(session.messages)
        return session

    def _insert(self, conn, session_id, msgs):
        rows = flatten(msgs)
        conn.executemany('INSERT INTO messages(session_id, role, content, timestamp) VALUES(?,?,?,?)',
                         [(session_id, m.role, m.text, m.ts or time.time() + i / 1000) for i, m in enumerate(rows)])
        conn.execute('''UPDATE sessions SET message_count=(SELECT COUNT(*) FROM messages WHERE session_id=?),
                        last_activity_at=? WHERE id=?''', (session_id, time.time(), session_id))

    def create(self, session):
        backup_db(self.db, 'hermes')
        session_id = 'bridge_' + uuid.uuid4().hex[:16]
        base = session.title or title_from(session.messages) or 'Sesi'
        with contextlib.closing(sqlite3.connect(self.db, timeout=30)) as conn, conn:
            title, attempt = base, 1
            while conn.execute('SELECT 1 FROM sessions WHERE title=?', (title,)).fetchone():  # Titles are unique there.
                attempt += 1
                title = f'{base} ({attempt})'
            conn.execute('''INSERT INTO sessions(id, source, started_at, ended_at, end_reason, title, title_source, cwd, model)
                            VALUES(?,?,?,?,?,?,?,?,?)''',
                         (session_id, 'cli', session.created or time.time(), time.time(), 'imported', title, 'derived',
                          session.cwd or None, session.model or None))
            self._insert(conn, session_id, session.messages)
        return session_id

    def append(self, session_id, msgs):
        backup_db(self.db, 'hermes')
        with contextlib.closing(sqlite3.connect(self.db, timeout=30)) as conn, conn:
            self._insert(conn, session_id, msgs)


class Copilot(Adapter):
    name, label = 'copilot', 'GitHub Copilot'
    note = 'Baca-saja: format tulis tak bisa diverifikasi tanpa CLI Copilot.'

    @property
    def root(self):
        return Path(os.environ.get('COPILOT_HOME') or HOME / '.copilot') / 'session-state'

    def available(self):
        return self.root.is_dir()

    def location(self):
        return str(self.root)

    @staticmethod
    def _yaml(path):
        out = {}
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                match = re.match(r'^(\w+):\s*(.*)$', line)
                if match:
                    out[match[1]] = match[2].strip().strip('"\'')
        return out

    def list(self):
        out = []
        for folder in self.root.iterdir():
            events = folder / 'events.jsonl'
            if events.exists():
                info, stat = self._yaml(folder / 'workspace.yaml'), events.stat()
                out.append(Meta(folder.name, info.get('name') or info.get('summary') or folder.name, stat.st_mtime,
                                info.get('cwd', ''), f'{stat.st_size}:{stat.st_mtime_ns}'))
        return out

    def load(self, session_id):
        folder = self.root / session_id
        info = self._yaml(folder / 'workspace.yaml')
        session = Session(self.name, session_id, info.get('name') or info.get('summary') or '', info.get('cwd', ''))
        tools = {}
        for row in json_lines(folder / 'events.jsonl'):
            kind, data, ts = row.get('type'), row.get('data') or {}, epoch(row.get('timestamp'))
            session.updated = max(session.updated, ts)
            session.created = session.created or ts
            if kind == 'user.message':
                session.messages.append(Msg('user', text_of(data.get('content')), ts))
            elif kind == 'assistant.message' and data.get('content'):
                session.messages.append(Msg('assistant', text_of(data.get('content')), ts))
            elif kind == 'tool.execution_start':
                tool = Msg('tool', '', ts, {'name': data.get('toolName'), 'input': data.get('arguments'), 'output': '',
                                            'id': data.get('toolCallId')})
                tools[data.get('toolCallId')] = tool
                session.messages.append(tool)
            elif kind == 'tool.execution_complete' and data.get('toolCallId') in tools:
                tools[data['toolCallId']].tool['output'] = text_of((data.get('result') or {}).get('content'))
        session.title = session.title or title_from(session.messages)
        return session


class AionUi(Adapter):
    name, label = 'aionui', 'AionUi'
    note = 'Baca-saja: baris percakapan butuh konfigurasi agen yang tak bisa diverifikasi tanpa aplikasinya.'

    @property
    def db(self):
        return APPDATA / 'AionUi/aionui/aionui-backend.db'

    def available(self):
        return self.db.exists()

    def location(self):
        return str(self.db)

    @staticmethod
    def _cwd(extra):
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            return json.loads(extra).get('workspace') or ''
        return ''

    def list(self):
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('''SELECT c.id, c.name, c.updated_at, c.extra, COUNT(m.id) FROM conversations c
                                   LEFT JOIN messages m ON m.conversation_id = c.id GROUP BY c.id''').fetchall()
        return [Meta(r[0], r[1] or r[0], epoch(r[2]), self._cwd(r[3]), f'{r[2]}:{r[4]}') for r in rows if r[4]]

    def load(self, session_id):
        with contextlib.closing(ro(self.db)) as conn:
            row = conn.execute('SELECT name, extra, model, created_at, updated_at FROM conversations WHERE id=?',
                               (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            session = Session(self.name, session_id, row[0] or '', self._cwd(row[1]), epoch(row[3]), epoch(row[4]))
            rows = conn.execute('''SELECT type, content, position, created_at FROM messages
                                   WHERE conversation_id=? AND COALESCE(hidden,0)=0 ORDER BY created_at, id''',
                                (session_id,)).fetchall()
        for kind, content, position, created in rows:
            ts = epoch(created)
            try:
                body = json.loads(content)
            except (ValueError, TypeError):
                continue
            if kind == 'text':
                session.messages.append(Msg('user' if position == 'right' else 'assistant', text_of(body), ts))
            elif kind == 'tool_call':
                session.messages.append(Msg('tool', '', ts, {'name': body.get('name'), 'id': body.get('call_id'),
                                                             'input': body.get('args') or body.get('input'),
                                                             'output': text_of(body.get('output')),
                                                             'status': body.get('status')}))
            elif kind == 'acp_tool_call':
                update = body.get('update') or {}
                session.messages.append(Msg('tool', '', ts, {'name': update.get('title') or update.get('kind'),
                                                             'id': update.get('tool_call_id'), 'input': None,
                                                             'output': text_of(update.get('content')),
                                                             'status': update.get('status')}))
        session.title = session.title or title_from(session.messages)
        return session


class Antigravity(Adapter):
    name, label = 'antigravity', 'Antigravity'
    note = 'Baca-saja lewat indeks teks; langkah aslinya protobuf tanpa skema.'

    @property
    def db(self):
        return HOME / '.gemini/antigravity/conversation_summaries.db'

    def available(self):
        return self.db.exists()

    def location(self):
        return str(self.db)

    def list(self):
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('SELECT conversation_id, title, last_modified_time, step_count, workspace_uris '
                                'FROM conversation_summaries').fetchall()
        return [Meta(r[0], r[1] or r[0], epoch(r[2]), str(r[4] or ''), f'{r[2]}:{r[3]}') for r in rows]

    def load(self, session_id):
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('''SELECT c8, c9, c13, c14, c10, c11, c4 FROM conversations_fts_content
                                   WHERE c0=? AND c1>=0 ORDER BY c1''', (session_id,)).fetchall()
        session = Session(self.name, session_id)
        steps = []
        for user_off, agent_off, user_body, agent_body, title, workspace, modified in rows:
            session.title = session.title or title or ''
            session.cwd = session.cwd or workspace or ''
            session.updated = max(session.updated, epoch(modified))
            # Offsets are [[step, byte], ...] into the body of that side.
            for role, offsets, body in (('user', user_off, user_body), ('assistant', agent_off, agent_body)):
                if not offsets or not body:
                    continue
                raw = body.encode('utf-8') if isinstance(body, str) else body
                pairs = json.loads(offsets)
                for index, (step, start) in enumerate(pairs):
                    end = pairs[index + 1][1] if index + 1 < len(pairs) else len(raw)
                    steps.append((step, role, raw[start:end].decode('utf-8', 'replace').strip()))
        session.messages = [Msg(role, text) for _, role, text in sorted(steps, key=lambda s: s[0]) if text]
        session.title = session.title or title_from(session.messages)
        return session


def walk_messages(node, out, depth=0):
    """Shape-tolerant reader for stores with no dedicated adapter."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            walk_messages(item, out, depth + 1)
    elif isinstance(node, dict):
        role = next((node[k] for k in ('role', 'sender', 'author', 'type') if isinstance(node.get(k), str)), '').lower()
        body = next((node[k] for k in ('content', 'text', 'message', 'parts') if k in node), None)
        mapped = {'user': 'user', 'human': 'user', 'assistant': 'assistant', 'ai': 'assistant', 'model': 'assistant',
                  'bot': 'assistant', 'agent': 'assistant'}.get(role)
        if mapped and text_of(body).strip():
            ts = next((node[k] for k in ('timestamp', 'created_at', 'createdAt', 'time') if node.get(k)), None)
            out.append(Msg(mapped, text_of(body), epoch(ts) if isinstance(ts, (str, int, float)) else 0.0))
            return
        for key, mapped in (('User', 'user'), ('Agent', 'assistant')):  # Zed's enum-tagged messages.
            if isinstance(node.get(key), dict) and text_of(node[key]).strip():
                out.append(Msg(mapped, text_of(node[key])))
                return
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk_messages(value, out, depth + 1)


class Zed(Adapter):
    name, label = 'zed', 'Zed'
    note = 'Baca-saja, belum diverifikasi: perangkat ini belum punya thread Zed.'

    @property
    def db(self):
        return LOCAL / 'Zed/threads/threads.db'

    def available(self):
        return self.db.exists()

    def location(self):
        return str(self.db)

    def list(self):
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('SELECT id, summary, updated_at FROM threads').fetchall()
        return [Meta(r[0], r[1] or r[0], epoch(r[2]), '', str(r[2])) for r in rows]

    def load(self, session_id):
        with contextlib.closing(ro(self.db)) as conn:
            row = conn.execute('SELECT summary, updated_at, data_type, data FROM threads WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        data = row[3]
        if row[2] == 'zstd':
            import zstandard
            data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True).read()
        session = Session(self.name, session_id, row[0] or '', updated=epoch(row[1]))
        walk_messages(json.loads(data), session.messages)
        return session


class Cherry(Adapter):
    name, label = 'cherry', 'Cherry Studio'
    note = 'Baca-saja, hanya sesi agen. Chat utamanya tersimpan di IndexedDB dan tidak didukung.'

    @property
    def db(self):
        return APPDATA / 'CherryStudio/Data/agents.db'

    def available(self):
        return self.db.exists()

    def location(self):
        return str(self.db)

    def list(self):
        with contextlib.closing(ro(self.db)) as conn:
            rows = conn.execute('''SELECT s.id, s.name, MAX(m.updated_at), COUNT(m.id) FROM sessions s
                                   JOIN session_messages m ON m.session_id = s.id GROUP BY s.id''').fetchall()
        return [Meta(str(r[0]), r[1] or str(r[0]), epoch(r[2]), '', f'{r[2]}:{r[3]}') for r in rows]

    def load(self, session_id):
        with contextlib.closing(ro(self.db)) as conn:
            row = conn.execute('SELECT name FROM sessions WHERE id=?', (session_id,)).fetchone()
            rows = conn.execute('SELECT role, content, created_at FROM session_messages WHERE session_id=? ORDER BY created_at, id',
                                (session_id,)).fetchall()
        session = Session(self.name, session_id, (row[0] if row else '') or '')
        for role, content, created in rows:
            ts = epoch(created)
            session.updated = max(session.updated, ts)
            try:
                body = json.loads(content)
            except (ValueError, TypeError):
                body = content
            blocks = body.get('blocks') if isinstance(body, dict) else None
            text = text_of([b for b in blocks if isinstance(b, dict) and 'text' in str(b.get('type'))]) if isinstance(blocks, list) else ''
            text = text or text_of(body.get('message') if isinstance(body, dict) else body)
            if text.strip():
                session.messages.append(Msg('user' if role == 'user' else 'assistant', text, ts))
        return session


class GenericFiles(Adapter):
    """A folder of JSON/JSONL conversations found by the scanner."""
    note = 'Baca-saja, terdeteksi otomatis. Format ditebak dari isi file.'

    def __init__(self, label, folder, files):
        self.name, self.label = f'auto:{label}', tr('{label} (terdeteksi)', label=label)
        self.folder, self.files = folder, files

    def available(self):
        return True

    def location(self):
        return str(self.folder)

    def list(self):
        out = []
        for path in self.files:
            with contextlib.suppress(OSError):
                stat = path.stat()
                out.append(Meta(str(path.relative_to(self.folder)), path.stem, stat.st_mtime, '',
                                f'{stat.st_size}:{stat.st_mtime_ns}'))
        return out

    def load(self, session_id):
        path = (self.folder / session_id).resolve()
        if self.folder.resolve() not in path.parents:
            raise KeyError(session_id)
        session = Session(self.name, session_id, updated=path.stat().st_mtime)
        if path.suffix == '.jsonl':
            for row in json_lines(path):
                walk_messages(row, session.messages)
        else:
            walk_messages(json.loads(path.read_text(encoding='utf-8', errors='replace')), session.messages)
        session.title = title_from(session.messages) or path.stem
        return session


class GenericSqlite(Adapter):
    """A SQLite file holding a table with role + content + a session column."""
    note = 'Baca-saja, terdeteksi otomatis. Format ditebak dari skema tabel.'

    def __init__(self, label, path, table, group, role, content):
        self.name, self.label = f'auto:{label}', tr('{label} (terdeteksi)', label=label)
        self.path, self.table, self.group, self.role, self.content = path, table, group, role, content

    def available(self):
        return True

    def location(self):
        return str(self.path)

    def list(self):
        with contextlib.closing(ro(self.path)) as conn:
            rows = conn.execute(f'SELECT "{self.group}", COUNT(*) FROM "{self.table}" GROUP BY 1').fetchall()
        updated = self.path.stat().st_mtime
        return [Meta(str(r[0]), str(r[0]), updated, '', str(r[1])) for r in rows]

    def load(self, session_id):
        with contextlib.closing(ro(self.path)) as conn:
            rows = conn.execute(f'SELECT "{self.role}", "{self.content}" FROM "{self.table}" WHERE "{self.group}"=? ORDER BY rowid',
                                (session_id,)).fetchall()
        session = Session(self.name, session_id, updated=self.path.stat().st_mtime)
        for role, content in rows:
            walk_messages({'role': str(role), 'content': content}, session.messages)
        session.title = title_from(session.messages) or session_id
        return session


FOLDERS = {'sessions', 'conversations', 'chats', 'threads', 'session-state', 'tasks', 'history'}
NOISE = {'.cache', '.npm', '.nuget', '.gradle', '.android', '.vscode', '.cargo', '.rustup', '.pyenv', '.conda', '.docker',
         '.ssh', '.git', '.local', '.config', '.claude', '.codex', '.hermes', '.copilot', '.gemini', '.session-bridge',
         '.ollama', '.gnupg', '.aws', '.azure', '.kube', '.m2', '.ipython', '.jupyter', '.matplotlib', '.dotnet',
         'code', 'code - insiders', 'cursor', 'google', 'microsoft', 'mozilla', 'packages', 'npm', 'npm-cache', 'pip',
         'temp', 'programs', 'node_modules', 'aionui', 'cherrystudio', 'zed', 'delta', 'openai', 'opencode', 'claude',
         'claude-3p', 'discord', 'slack', 'spotify', 'telegram desktop', 'whatsapp', 'nvidia', 'nvidia corporation',
         'adobe', 'jetbrains', 'unity', 'uv', 'windsurf'}
SKIP_WALK = {'node_modules', 'cache', 'caches', 'gpucache', 'code cache', 'dawngraphitecache', 'dawnwebgpucache', 'logs',
             'blob_storage', 'site packages', 'site-packages', '.git', 'indexeddb', 'local storage', 'session storage'}


def _shows_messages(path):
    """True when a file's first records hold real role+text messages."""
    found = []
    with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
        if path.suffix == '.jsonl':
            for index, line in enumerate(stream_lines(path)):
                if index > 40:
                    break
                with contextlib.suppress(ValueError):
                    walk_messages(json.loads(line), found)
        elif path.stat().st_size < 50 * 1024 * 1024:
            walk_messages(json.loads(path.read_text(encoding='utf-8')), found)
    return bool(found)


def _sqlite_candidate(path):
    with contextlib.suppress(sqlite3.Error, OSError), contextlib.closing(ro(path)) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if SECRET.search(table):
                continue
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            lowered = {c.lower(): c for c in columns}
            role = lowered.get('role')
            content = lowered.get('content') or lowered.get('text')
            group = next((c for c in columns if re.search(r'(session|conversation|chat|thread)_?id$', c, re.I)), None)
            if role and content and group and conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                return table, group, role, content
    return None


def scan_generic():
    """Shallow search outside known stores. Credential-looking names are skipped without being opened."""
    found = {}
    roots = []
    with contextlib.suppress(OSError):
        roots += [p for p in HOME.iterdir() if p.name.startswith('.')]
    for base in (APPDATA, LOCAL):
        with contextlib.suppress(OSError):
            roots += list(base.iterdir())
    for root in roots:
        with contextlib.suppress(OSError):
            if root.name.lower() in NOISE or not root.is_dir():
                continue
        if root.name.lower() in NOISE:
            continue
        label = root.name.lstrip('.')
        stack, seen = [(root, 0)], 0
        while stack and seen < 2000 and label not in found:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            seen += len(entries)
            for entry in entries:
                name = entry.name.lower()
                if SECRET.search(name):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                path = Path(entry.path)
                if is_dir:
                    if name in FOLDERS:
                        files = [p for p in _session_files(path)]
                        if files and any(_shows_messages(p) for p in files[:3]):
                            found[label] = GenericFiles(label, path, files)
                            break
                    if depth < 3 and name not in SKIP_WALK:
                        stack.append((path, depth + 1))
                elif name.endswith(('.db', '.sqlite', '.sqlite3')):
                    hit = _sqlite_candidate(path)
                    if hit:
                        found[label] = GenericSqlite(label, path, *hit)
                        break
    return list(found.values())


def _session_files(folder):
    out = []
    for base, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_WALK and not SECRET.search(d)][:50]
        for name in names:
            if name.endswith(('.json', '.jsonl')) and not SECRET.search(name):
                out.append(Path(base) / name)
                if len(out) >= 2000:
                    return out
    return out


def registry(scan=True):
    known = [Claude(), Codex(), Delta(), OpenCode(), Hermes(), Copilot(), AionUi(), Antigravity(), Zed(), Cherry()]
    found = [a for a in known if a.available()]
    if scan:
        with contextlib.suppress(Exception):  # A scan problem must never block the known harnesses.
            found += scan_generic()
    return {a.name: a for a in found}


def detect_rows(adapters):
    rows = []
    for adapter in adapters.values():
        try:
            count = len(adapter.list())
        except Exception as error:
            logging.warning('detect %s failed: %s', adapter.name, type(error).__name__)
            count = tr('gagal: {error}', error=type(error).__name__)
        rows.append({'name': adapter.name, 'label': adapter.label, 'sessions': count,
                     'write': adapter.can_write, 'path': adapter.location(), 'note': tr(adapter.note)})
    return rows
