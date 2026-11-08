"""Self-check. Everything runs in a throwaway folder; no live store is read or written.

    uv run --python 3.12 --with zstandard python test_bridge.py [--live]

--live also drives the real codex/opencode/claude/hermes binaries against sandboxes (no model calls).
Set BRIDGE_TEST_DIR when the default temp folder can't hold SQLite files.
"""
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from glob import glob
from pathlib import Path


class Mem:
    """In-memory harness. `reformat` mimics a store that rewrites what it receives."""
    can_write = True

    def __init__(self, name, reformat=False):
        self.name, self.reformat, self.store, self.count = name, reformat, {}, 0

    def add(self, sid, *texts, age=3600):
        import core
        msgs = self.store.setdefault(sid, [])
        for text in texts:
            role = 'assistant' if len(msgs) % 2 else 'user'
            msgs.append(core.Msg(role, text, time.time() - age))

    def _meta(self, sid):
        import core
        return core.Meta(sid, f'judul {sid}', self.store[sid][-1].ts, '', str(len(self.store[sid])))

    def list(self):
        return [self._meta(s) for s in self.store]

    def fingerprint(self, meta):
        return meta.fingerprint

    def load(self, sid):
        import core
        return core.Session(self.name, sid, f'judul {sid}', messages=list(self.store[sid]))

    def create(self, session):
        self.count += 1
        sid = f'{self.name}{self.count}'
        self.store[sid] = []
        self.append(sid, session.messages)
        return sid

    def append(self, sid, msgs):
        import core
        for msg in core.flatten(msgs):
            text = f'>> {msg.text}' if self.reformat else msg.text
            self.store[sid].append(core.Msg(msg.role, text, msg.ts))

    def busy(self, sid):
        return False

    def reveal(self, sid):
        self.revealed = getattr(self, 'revealed', []) + [sid]

    def has(self, sid):
        return sid in self.store


def fresh(root, name):
    box = Path(root) / name
    box.mkdir(parents=True)
    os.environ['SESSION_BRIDGE_DIR'] = str(box / 'state')
    return box


def texts(adapter, sid):
    return [m.text for m in adapter.load(sid).messages]


def check_engine(root):
    import core
    fresh(root, 'engine')
    a, b, c = Mem('a'), Mem('b'), Mem('c')
    state = core.State()
    route = lambda s, d, **k: core.sync_route(state, s, d, 0, None, **k)

    a.add('s', 'tanya', 'jawab')
    preview = route(a, b, dry=True)
    assert preview[0][4:] == ('baru', 2) and not b.store, 'preview must not write'
    assert route(a, b)[0][4:] == ('baru', 2) and texts(b, 'b1') == ['tanya', 'jawab']
    assert route(b, a) == [] and len(a.store['s']) == 2, 'echo back to source'
    assert route(a, b) == [], 'unchanged source should cost nothing'

    a.add('s', 'lanjut di a', 'balas a')
    assert route(a, b)[0][4:] == ('tambah', 2) and len(b.store) == 1
    b.add('b1', 'lanjut di b', 'balas b')
    assert route(b, a)[0][4:] == ('tambah', 2) and texts(a, 's')[-1] == 'balas b'
    assert route(a, b) == [] and route(b, a) == [], 'no ping-pong after both sides moved'

    a.add('s', 'tanya', 'jawab')  # Same words again are new messages, not echoes.
    assert route(a, b)[0][5] == 2 and len(b.store['b1']) == 8

    assert route(b, c)[0][4:] == ('baru', 8) and route(c, a) == [], 'chain stays deduped'
    assert route(a, c) == []

    # A store that rewrites text must not bounce the rewrite back.
    fresh(root, 'reformat')
    a, r = Mem('a'), Mem('r', reformat=True)
    state = core.State()
    route = lambda s, d, **k: core.sync_route(state, s, d, 0, None, **k)
    a.add('s', 'halo', 'ya')
    route(a, r)
    assert route(r, a) == [] and len(a.store['s']) == 2, 'reformatted copy echoed'
    r.add('r1', 'baru di r', 'ok')
    assert route(r, a)[0][5] == 2 and len(a.store['s']) == 4, 'real new message lost behind reformat'
    assert route(a, r) == [] or route(a, r)[0][5] == 0

    # Tool blocks and their flattened copies are the same message.
    fresh(root, 'tools')
    a, b = Mem('a'), Mem('b')
    state = core.State()
    a.store['s'] = [core.Msg('user', 'run', time.time() - 900),
                    core.Msg('tool', '', time.time() - 900, {'name': 'ls', 'input': {'b': 1, 'a': 2}, 'output': 'x' * 20000})]
    core.sync_route(state, a, b, 0, None)
    assert 'dipotong session-bridge' in texts(b, 'b1')[1] and len(texts(b, 'b1')[1]) < 16100
    assert core.sync_route(state, b, a, 0, None) == [], 'flat tool copy must digest like the tool block'

    # Bundles: re-import dedupes, and returning home links instead of duplicating.
    fresh(root, 'bundle')
    a, b = Mem('a'), Mem('b')
    state = core.State()
    a.add('s', 'tanya', 'jawab')
    path = Path(root) / 'x.json'
    assert core.export_bundle(a, ['s'], path) == 1
    loaded = core.load_bundle(path)
    transfer = lambda items, dst: core.transfer([(core.BUNDLE, lambda s=s: s) for s in items], dst)
    assert transfer(loaded, b) == 2 and transfer(loaded, b) == 0 and len(b.store) == 1
    assert b.revealed == ['b1', 'b1'], 'every manual move shows its copy, new or already there'
    assert transfer(loaded, a) == 0 and len(a.store) == 1 and a.revealed == ['s'], 'bundle came home'
    a.add('s', 'tambah', 'lagi')
    core.export_bundle(a, ['s'], path)
    assert transfer(core.load_bundle(path), b) == 2 and len(b.store['b1']) == 4
    path.write_text('{"format": "lain"}', encoding='utf-8')
    with contextlib.suppress(ValueError):
        core.load_bundle(path)
        raise AssertionError('foreign bundle accepted')

    # Existing links are adopted without copying anything.
    fresh(root, 'adopt')
    a, b = Mem('a'), Mem('b')
    state = core.State()
    a.add('s', 'lama 1', 'lama 2')
    b.add('t', 'lama 1 versi lain', 'lama 2 versi lain')
    state.link_pair(('a', 's'), ('b', 't'))
    assert core.sync_route(state, a, b, 0, None)[0][4] == 'adopsi' and len(b.store['t']) == 2
    assert core.sync_route(state, b, a, 0, None)[0][4] == 'adopsi' and len(a.store['s']) == 2
    a.add('s', 'baru', 'baru jawab')
    assert core.sync_route(state, a, b, 0, None)[0][5] == 2 and len(b.store['t']) == 4
    assert core.sync_route(state, b, a, 0, None) == [] and len(a.store['s']) == 4

    # Day filter: adopt() links nearly everything, so a group alone must not let old history through.
    fresh(root, 'since')
    a, b, c = Mem('a'), Mem('b'), Mem('c')
    state = core.State()
    a.add('old', 'lama', 'lama jawab', age=30 * 86400)
    b.add('peer', 'lama', 'lama jawab')
    state.link_pair(('a', 'old'), ('b', 'peer'))
    assert core.sync_route(state, a, c, 7, None) == [] and not c.store, 'since_days bypassed by group link'
    assert core.sync_route(state, a, b, 7, None)[0][4] == 'adopsi', 'existing peer must stay in sync'

    # delta-sync coexistence.
    fresh(root, 'mirror')
    state = core.State()
    mirror = {'pairs': [('cx', 'dl')], 'delta': {'dl'}, 'active': True}
    core.adopt(state, mirror)
    skip = core.mirror_skip
    assert skip(state, mirror, 'codex', 'new', 'delta'), 'codex->delta belongs to delta-sync'
    assert skip(state, mirror, 'delta', 'dl', 'claude'), 'mirrored thread as source'
    assert skip(state, mirror, 'delta', 'native', 'codex'), 'would come back as a second thread'
    assert not skip(state, mirror, 'delta', 'native', 'claude')
    assert not skip(state, mirror, 'claude', 'c1', 'delta')
    state.link('g', 'claude', 'c1')
    state.link('g', 'codex', 'c2')
    assert skip(state, mirror, 'claude', 'c1', 'delta'), 'delta-sync brings it via codex'
    state.link_pair(('claude', 'c3'), ('codex', 'cx'))
    assert skip(state, mirror, 'claude', 'c3', 'delta'), 'never write into a delta-sync thread'
    keep, _ = core.effective_routes([('claude', 'codex'), ('claude', 'delta'), ('codex', 'delta'), ('delta', 'claude')], mirror)
    assert keep == [('claude', 'codex'), ('delta', 'claude')]
    assert skip(state, {**mirror, 'active': False}, 'codex', 'new', 'delta') == ''
    assert core.effective_routes([('codex', 'delta')], None)[0] == [('codex', 'delta')]

    assert core.flat(core.Msg('tool', '', 0, {'name': 'x', 'input': {'b': 1, 'a': 2}, 'output': 'o'})) == \
        core.flat(core.Msg('tool', '', 0, {'name': 'x', 'input': {'a': 2, 'b': 1}, 'output': 'o'}))
    print('ok engine')


def old_claude_rows(sid):
    base = {'sessionId': sid, 'cwd': r'D:\proj', 'version': '2', 'userType': 'external'}
    return [
        {**base, 'type': 'custom-title', 'customTitle': 'Judul Asli'},
        {**base, 'type': 'user', 'uuid': 'u1', 'parentUuid': None, 'timestamp': '2026-01-01T00:00:00Z', 'message': {'role': 'user', 'content': 'pertanyaan'}},
        {**base, 'type': 'assistant', 'uuid': 'a0', 'parentUuid': 'u1', 'timestamp': '2026-01-01T00:00:01Z', 'message': {'role': 'assistant', 'model': 'm', 'content': [{'type': 'thinking', 'thinking': 'rahasia', 'signature': 'x'}]}},
        {**base, 'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'a0', 'timestamp': '2026-01-01T00:00:02Z', 'message': {'role': 'assistant', 'model': 'm', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {'p': 1}}]}},
        {**base, 'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1', 'timestamp': '2026-01-01T00:00:03Z', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'isi file'}]}},
        {**base, 'type': 'assistant', 'uuid': 'a2', 'parentUuid': 'u2', 'timestamp': '2026-01-01T00:00:04Z', 'message': {'role': 'assistant', 'model': 'm', 'content': [{'type': 'text', 'text': 'jawaban'}]}},
        {**base, 'type': 'user', 'isSidechain': True, 'uuid': 's1', 'message': {'role': 'user', 'content': 'subagent'}},
    ]


def check_files(root):
    """Real adapters on hand-made stores: format readers plus a full route through every file-based writer."""
    import core
    import adapters as A
    import zstandard
    box = fresh(root, 'files')
    os.environ.update(CLAUDE_CONFIG_DIR=str(box / 'claude'), CODEX_HOME=str(box / 'codex'),
                      HERMES_HOME=str(box / 'hermes'), COPILOT_HOME=str(box / 'copilot'))
    claude, codex, hermes, copilot = A.Claude(), A.Codex(), A.Hermes(), A.Copilot()

    sid = '11111111-2222-3333-4444-555555555555'
    project = box / 'claude/projects/D--proj'
    (project / sid / 'subagents').mkdir(parents=True)
    (project / sid / 'subagents' / 'agent-1.jsonl').write_text('{}\n')
    (project / f'{sid}.jsonl').write_text('\n'.join(map(json.dumps, old_claude_rows(sid))) + '\n', encoding='utf-8')
    assert [m.id for m in claude.list()] == [sid] and claude.list()[0].title == 'Judul Asli'
    loaded = claude.load(sid)
    assert [m.role for m in loaded.messages] == ['user', 'tool', 'assistant'], 'thinking and sidechains stay out'
    assert loaded.messages[1].tool['output'] == 'isi file' and loaded.cwd == r'D:\proj'

    # Codex: compressed native paginated thread, plus legacy turns in an uncompressed one.
    day = box / 'codex/sessions/2026/01/01'
    day.mkdir(parents=True)
    native = '22222222-2222-3333-4444-555555555555'
    rows = [{'timestamp': '2026-01-01T00:00:00Z', 'type': 'session_meta', 'payload': {'id': native, 'cwd': r'D:\proj', 'history_mode': 'paginated'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'x'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'dobel'}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': native, 'item': {'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'dobel'}]}}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'sh', 'call_id': 'c', 'arguments': '{"a":1}'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'c', 'output': [{'type': 'input_text', 'text': 'keluar'}]}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': native, 'item': {'type': 'AgentMessage', 'content': [{'type': 'Text', 'text': 'selesai'}]}}}]
    data = '\n'.join(map(json.dumps, rows)) + '\n'
    path = day / f'rollout-2026-01-01T00-00-00-{native}.jsonl.zst'
    path.write_bytes(zstandard.ZstdCompressor().compress(data.encode()))
    (box / 'codex/session_index.jsonl').write_text(json.dumps({'id': native, 'thread_name': 'Nama Codex'}) + '\n')
    assert codex.list()[0].title == 'Nama Codex'
    got = codex.load(native).messages
    assert [(m.role, m.text) for m in got if m.role != 'tool'] == [('user', 'dobel'), ('assistant', 'selesai')], 'item_completed wins per turn'
    assert [m.tool['output'] for m in got if m.role == 'tool'] == ['keluar']
    codex.append(native, [core.Msg('user', 'tambahan'), core.Msg('assistant', 'oke')])
    assert [m.text for m in codex.load(native).messages][-2:] == ['tambahan', 'oke'], 'zstd frame append'
    assert '"ordinal": 7' in zstandard.ZstdDecompressor().stream_reader(path.read_bytes(), read_across_frames=True).read().decode()

    # Hermes: enough schema for the writer; --live repeats this on the real schema.
    (box / 'hermes').mkdir()
    conn = sqlite3.connect(box / 'hermes/state.db')
    conn.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
                            title TEXT, title_source TEXT, cwd TEXT, model TEXT, message_count INT DEFAULT 0, last_activity_at REAL, parent_session_id TEXT);
                          CREATE UNIQUE INDEX t ON sessions(title) WHERE title IS NOT NULL;
                          CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_call_id TEXT,
                            tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INT DEFAULT 1);
                          INSERT INTO sessions(id, source, started_at, title) VALUES('old', 'cli', 1, 'Judul Asli');
                          INSERT INTO messages(session_id, role, content, timestamp) VALUES('old', 'user', 'lama', 1);''')
    conn.commit()
    conn.close()

    # Full route: Claude -> Codex -> Hermes -> Claude, and nothing may multiply.
    state = core.State()
    route = lambda s, d: core.sync_route(state, s, d, 0, None)
    time.sleep(0.01)
    with contextlib.suppress(OSError):
        os.utime(project / f'{sid}.jsonl', (time.time() - 3600, time.time() - 3600))
    assert route(claude, codex)[0][4:] == ('baru', 3)
    made = next(m for m in codex.list() if m.id != native)
    assert made.title == 'Judul Asli' and texts(codex, made.id)[-1] == 'jawaban'
    assert core.digests(core.clean(codex.load(made.id).messages)) == core.digests(core.clean(loaded.messages))
    for rollout in Path(box / 'codex/sessions').rglob('*.jsonl'):
        os.utime(rollout, (time.time() - 3600, time.time() - 3600))
    results = route(codex, hermes)
    assert ('codex', 'hermes', made.id, 'Judul Asli', 'baru', 3) in results
    assert sorted(m.title for m in hermes.list()) == ['Judul Asli', 'Judul Asli (2)'], 'unique title suffix'
    copy = next(m for m in hermes.list() if m.id != 'old')
    assert list((box / 'state/backups').glob('hermes-*.sqlite')), 'backup before first write'
    with sqlite3.connect(box / 'hermes/state.db') as conn:
        old = conn.execute("SELECT content FROM messages WHERE session_id='old'").fetchall()
        conn.execute('UPDATE messages SET timestamp = timestamp - 3600')
    assert old == [('lama',)], 'existing rows untouched'
    copy = hermes.list()
    assert route(hermes, claude) == [] or all(r[2] == 'old' for r in route(hermes, claude))
    assert len(claude.list()) <= 2

    # Append reaches the older Claude file with an unbroken parent chain.
    before = (project / f'{sid}.jsonl').read_text(encoding='utf-8')
    claude.append(sid, [core.Msg('user', 'susulan'), core.Msg('assistant', 'siap')])
    after = (project / f'{sid}.jsonl').read_text(encoding='utf-8')
    assert after.startswith(before), 'append must not rewrite existing lines'
    added = [json.loads(x) for x in after[len(before):].splitlines()]
    assert added[0]['parentUuid'] == 'a2' and added[1]['parentUuid'] == added[0]['uuid']
    assert texts(claude, sid)[-2:] == ['susulan', 'siap']

    # A missing cwd falls back to home. Only a manual move opens the desktop app (checked in check_gui), never auto-sync.
    A.OPENED.clear()
    gone = core.Session('codex', 'g', 'Folder Hilang', cwd=str(box / 'tidak-ada'), messages=[core.Msg('user', 'a'), core.Msg('assistant', 'b')])
    new = claude.create(gone)
    assert not A.OPENED, A.OPENED
    assert claude._file(new).parent.name == re.sub(r'[^a-zA-Z0-9]', '-', str(Path.home())), 'missing cwd falls back to home'
    claude._file(new).unlink()

    # Tool calls land as native tool_use/tool_result pairs, not "[tool x]" text; Codex citations become paths.
    patch = core.Msg('tool', '', 5, {'name': 'apply_patch', 'input': '*** Begin Patch', 'output': 'ok', 'status': 'completed'})
    shell = core.Msg('tool', '', 6, {'name': 'functions.exec', 'input': '{"cmd": "ls"}', 'output': '', 'status': 'failed'})
    said = core.Msg('assistant', 'lihat :codex-file-citation{path="D:/x.md" purpose="output"}', 7)
    new = claude.create(core.Session('codex', 't', 'Tool', cwd=str(box), messages=[core.Msg('user', 'mulai', 4), patch, shell, said]))
    rows = [r for r in A.json_lines(claude._file(new)) if r.get('type') in ('user', 'assistant')]
    blocks = [r['message']['content'] for r in rows]
    assert [b if isinstance(b, str) else b[0]['type'] for b in blocks] == \
        ['mulai', 'tool_use', 'tool_result', 'tool_use', 'tool_result', 'text'], blocks
    assert blocks[1][0]['input'] == {'input': '*** Begin Patch'} and blocks[3][0]['input'] == {'cmd': 'ls'}
    assert blocks[3][0]['name'] == 'functions_exec' and blocks[4][0]['is_error'] and blocks[4][0]['content']
    assert blocks[2][0]['tool_use_id'] == blocks[1][0]['id'] and blocks[5][0]['text'] == 'lihat `D:/x.md`'
    assert all(r['parentUuid'] == p['uuid'] for p, r in zip(rows, rows[1:])), 'unbroken parent chain'
    back = claude.load(new).messages
    assert [m.role for m in back] == ['user', 'tool', 'tool', 'assistant'] and back[1].tool['output'] == 'ok'
    assert core.digests([back[1]]) == core.digests([patch]), 'raw-string tool input round-trips'
    claude._file(new).unlink()

    # Read-only stores.
    folder = box / 'copilot/session-state/abc'
    folder.mkdir(parents=True)
    (folder / 'workspace.yaml').write_text('name: Sesi Copilot\ncwd: D:\\proj\n')
    (folder / 'events.jsonl').write_text('\n'.join(map(json.dumps, [
        {'type': 'user.message', 'timestamp': '2026-01-01T00:00:00Z', 'data': {'content': 'hai'}},
        {'type': 'tool.execution_start', 'data': {'toolCallId': 't', 'toolName': 'ls', 'arguments': {}}},
        {'type': 'tool.execution_complete', 'data': {'toolCallId': 't', 'result': {'content': 'hasil'}}},
        {'type': 'assistant.message', 'data': {'content': 'halo'}}])) + '\n')
    assert copilot.list()[0].title == 'Sesi Copilot' and not copilot.can_write
    assert [m.role for m in copilot.load('abc').messages] == ['user', 'tool', 'assistant']

    # Generic reader: messages found, credential-named files never listed.
    gen = box / 'gen'
    gen.mkdir()
    (gen / 'a.json').write_text(json.dumps({'messages': [{'role': 'user', 'content': 'x'}, {'role': 'model', 'parts': [{'text': 'y'}]}]}))
    (gen / 'auth_token.json').write_text('{"role": "user", "content": "BOCOR"}')
    files = A._session_files(gen)
    assert [p.name for p in files] == ['a.json'] and A._shows_messages(files[0])
    assert texts(A.GenericFiles('x', gen, files), 'a.json') == ['x', 'y']
    with contextlib.suppress(KeyError):
        A.GenericFiles('x', gen, files).load('..\\..\\rahasia.json')
        raise AssertionError('path escape accepted')

    # Delta against a fake CLI: privacy flags, account binding, sequencing, oversize split.
    calls = []

    class FakeDelta(A.Delta):
        account = 'acct-1'

        def run(self, *args, records=None):
            calls.append((args, records))
            if args[:2] == ('thread', 'contract'):
                return {'min': 8, 'max': 8}
            if args[:2] == ('auth', 'status'):
                return {'status': 'signed_in', 'account': {'account_id': self.account}}
            if args[:2] == ('org', 'list'):
                return {'organizations': [{'id': 'org', 'current': True}]}
            if args[:2] == ('thread', 'create'):
                return {'thread_id': 'dl1'}
            if args[:2] == ('thread', 'append'):
                return [{'sequence': json.loads(x)['sequence'], 'disposition': 'recorded'} for x in records.splitlines()]
            raise AssertionError(args)

    delta = FakeDelta()
    long_text = 'k' * (A.Delta.MAX_RECORD // 6 + 5)
    thread = delta.create(core.Session('claude', 'x', 'Judul', messages=[
        core.Msg('user', long_text), core.Msg('tool', '', 0, {'name': 'ls', 'input': {'a': 1}, 'output': 'o', 'status': 'succeeded'})]))
    create = next(a for a, _ in calls if a[:2] == ('thread', 'create'))
    assert create[create.index('--visibility') + 1] == 'private'
    sent = [json.loads(x) for _, r in calls if r for x in r.splitlines()]
    assert [r['sequence'] for r in sent] == [1, 2, 3, 4] and sent[0]['update']['title'] == 'Judul'
    assert sent[3]['update']['status'] == 'completed' and sent[3]['update']['toolCallId']
    delta.append(thread, [core.Msg('assistant', 'lagi')])
    assert json.loads(calls[-1][1])['sequence'] == 5
    delta.account = 'acct-2'
    with contextlib.suppress(RuntimeError):
        delta.append(thread, [core.Msg('assistant', 'salah akun')])
        raise AssertionError('account change must refuse')
    print('ok files')


def rpc_codex(home, *requests):
    """Call the real Codex app-server. No model call happens for list/read/resume."""
    binary = sorted(glob(os.path.join(os.environ['LOCALAPPDATA'], 'OpenAI/Codex/bin/*/codex.exe')))
    if not binary:
        return None
    process = subprocess.Popen([binary[-1], 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               encoding='utf-8', env={**os.environ, 'CODEX_HOME': str(home)}, creationflags=subprocess.CREATE_NO_WINDOW)

    def call(number, method, params):
        process.stdin.write(json.dumps({'method': method, 'id': number, 'params': params}) + '\n')
        process.stdin.flush()
        while True:
            reply = json.loads(process.stdout.readline())
            if reply.get('id') == number:
                return reply
    try:
        call(1, 'initialize', {'clientInfo': {'name': 'bridge-test', 'version': '1'}})
        process.stdin.write(json.dumps({'method': 'initialized'}) + '\n')
        process.stdin.flush()
        return [call(i + 2, method, params) for i, (method, params) in enumerate(requests)]
    finally:
        process.kill()
        process.wait()


def check_live(root):
    import core
    import adapters as A
    box = fresh(root, 'live')
    sample = [core.Msg('user', 'tanya bridge', time.time() - 90), core.Msg('assistant', 'jawab bridge', time.time() - 89),
              core.Msg('tool', '', time.time() - 88, {'name': 'ls', 'input': {'a': 1}, 'output': 'keluaran'})]
    session = core.Session('test', 'x', 'Judul Live', r'D:\TA\Claude', time.time() - 90, messages=sample)

    # Codex: a created thread and an appended turn must both show in the real server, with the title.
    os.environ['CODEX_HOME'] = str(box / 'codex')
    (box / 'codex').mkdir()
    codex = A.Codex()
    tid = codex.create(session)
    codex.append(tid, [core.Msg('user', 'susulan bridge'), core.Msg('assistant', 'susulan jawab')])
    replies = rpc_codex(box / 'codex', ('thread/list', {}), ('thread/read', {'threadId': tid, 'includeTurns': True}), ('thread/resume', {'threadId': tid}))
    if replies:
        rows = replies[0]['result']['data']
        assert [r['id'] for r in rows] == [tid], rows
        print('   codex list name/preview:', rows[0].get('name'), '/', rows[0].get('preview'))
        turns = replies[1]['result']['thread']['turns']
        assert len(turns) == 2, f'codex turns {len(turns)}'
        assert 'result' in replies[2], replies[2]
        # A native-looking paginated thread: append must survive the same server.
        paged = box / 'codex/sessions/2026/01/02'
        paged.mkdir(parents=True)
        native = '33333333-2222-3333-4444-555555555555'
        meta = {'session_id': native, 'id': native, 'timestamp': '2026-01-02T00:00:00.000Z', 'cwd': r'D:\TA\Claude', 'originator': 'x',
                'cli_version': '0.162.0-alpha.17.2', 'source': 'vscode', 'model_provider': 'openai', 'history_mode': 'paginated',
                'context_window': {'window_id': native}}
        (paged / f'rollout-2026-01-02T00-00-00-{native}.jsonl').write_text(
            codex._encode([('session_meta', meta)] + codex._turns(native, sample[:2], True), 0, meta['timestamp']), encoding='utf-8')
        codex.append(native, [core.Msg('user', 'paged susulan'), core.Msg('assistant', 'paged jawab')])
        reply = rpc_codex(box / 'codex', ('thread/resume', {'threadId': native}))[0]
        assert 'result' in reply, reply
        shown = json.dumps(reply['result'])
        assert 'paged susulan' in shown or 'tanya bridge' in shown or reply['result'].get('thread'), reply
        items = rpc_codex(box / 'codex', ('thread/resume', {'threadId': native}), ('thread/turns/list', {'threadId': native}))[1]
        print('   codex paged turns:', len(items['result']['data']))
        assert len(items['result']['data']) == 2, items
        print('ok live codex')
    else:
        print('skip codex: binary not found')

    # OpenCode: official import both ways, in an XDG sandbox.
    for key, name in (('XDG_DATA_HOME', 'data'), ('XDG_CONFIG_HOME', 'cfg'), ('XDG_STATE_HOME', 'st'), ('XDG_CACHE_HOME', 'cache')):
        os.environ[key] = str(box / 'oc' / name)
    (box / 'oc').mkdir()
    opencode = A.OpenCode()
    if opencode.exe:
        sid = opencode.create(session)
        assert [m.id for m in opencode.list()] == [sid] and opencode.list()[0].title == 'Judul Live'
        assert len(texts(opencode, sid)) == 3
        opencode.append(sid, [core.Msg('user', 'lanjut oc'), core.Msg('assistant', 'oke oc')])
        assert texts(opencode, sid)[-1] == 'oke oc' and len(texts(opencode, sid)) == 5
        assert core.digests(core.clean(opencode.load(sid).messages))[:3] == core.digests(core.clean(sample)), 'opencode digest'
        print('ok live opencode')
    else:
        print('skip opencode: binary not found')

    # Hermes: writer against the real schema, read back by the real CLI.
    real = Path.home() / '.hermes/state.db'
    os.environ['HERMES_HOME'] = str(box / 'hermes')
    (box / 'hermes').mkdir()
    if real.exists():
        with contextlib.closing(sqlite3.connect(real.as_uri() + '?mode=ro', uri=True)) as src, \
                contextlib.closing(sqlite3.connect(box / 'hermes/state.db')) as dst:
            pending = src.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND type='table' AND name NOT LIKE '%_fts%' AND name NOT LIKE 'sqlite_%'").fetchall()
            for (sql,) in pending:
                dst.execute(sql)
            for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND type='index' AND name NOT LIKE 'sqlite_%'").fetchall():
                with contextlib.suppress(sqlite3.Error):
                    dst.execute(sql)
            for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall():
                if 'fts' not in sql.lower():
                    dst.execute(sql)
            dst.commit()
        hermes = A.Hermes()
        sid = hermes.create(session)
        second = hermes.create(session)
        assert len({sid, second}) == 2 and len(hermes.list()) == 2
        hermes.append(sid, [core.Msg('user', 'lanjut h'), core.Msg('assistant', 'oke h')])
        assert texts(hermes, sid)[-1] == 'oke h' and len(texts(hermes, sid)) == 5
        assert core.digests(core.clean(hermes.load(sid).messages))[:3] == core.digests(core.clean(sample)), 'hermes digest'
        # The CLI on a fresh HERMES_HOME starts installing its own dependencies, so ask Hermes' SessionDB instead.
        agent = Path.home() / '.hermes/hermes-agent'
        python = next(iter(sorted((Path.home() / '.hermes/tools').glob('python-*/python.exe'))), None)
        if python and agent.exists():
            code = ('import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import hermes_bootstrap;from hermes_state import SessionDB;'
                    'd=SessionDB(Path(sys.argv[2]));r=d.list_sessions_rich(limit=5);'
                    'print(json.dumps({"titles":sorted(str(x.get("title")) for x in r),'
                    '"msgs":[m.get("content") for m in d.get_messages_as_conversation(sys.argv[3])]}))')
            shown = subprocess.run([str(python), '-I', '-c', code, str(agent), str(box / 'hermes/state.db'), sid], capture_output=True, encoding='utf-8', errors='replace',
                                   stdin=subprocess.DEVNULL, timeout=120, env={**os.environ, 'HERMES_HOME': str(Path.home() / '.hermes')}, creationflags=subprocess.CREATE_NO_WINDOW)
            assert shown.returncode == 0, shown.stderr[-500:]
            seen = json.loads(shown.stdout.strip().splitlines()[-1])
            print('   hermes SessionDB:', seen['titles'], len(seen['msgs']))
            assert 'Judul Live' in seen['titles'] and len(seen['msgs']) == 5 and seen['msgs'][-1] == 'oke h', seen
        print('ok live hermes')
    else:
        print('skip hermes: no real schema')

    # Claude: real CLI must find the session. Without credentials it stops before any model call.
    os.environ['CLAUDE_CONFIG_DIR'] = str(box / 'claude')
    claude = A.Claude()
    claude.root.mkdir(parents=True)
    sid = claude.create(session)
    claude.append(sid, [core.Msg('user', 'susulan c'), core.Msg('assistant', 'oke c')])
    assert [m.id for m in claude.list()] == [sid] and claude.list()[0].title == 'Judul Live'
    assert len(texts(claude, sid)) == 5
    assert core.digests(core.clean(claude.load(sid).messages))[:3] == core.digests(core.clean(sample)), 'claude digest'
    exe = shutil.which('claude.cmd')
    if exe:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC', 'CLAUDE_'))}
        env['CLAUDE_CONFIG_DIR'] = str(box / 'claude')
        probe = lambda s: subprocess.run(f'"{exe}" -p --resume {s} --max-turns 1 "ping"', shell=True, capture_output=True, encoding='utf-8', errors='replace',
                                         timeout=120, env=env, cwd=r'D:\TA\Claude', creationflags=subprocess.CREATE_NO_WINDOW)
        known = probe(sid)
        missing = probe('99999999-9999-4999-8999-999999999999')
        text = (known.stdout + known.stderr).strip()
        assert 'No conversation found' in missing.stdout + missing.stderr, 'probe needs the not-found error to compare against'
        assert 'No conversation found' not in text, text[-300:]
        print('   claude resume reply:', text[-120:])
        print('ok live claude')
    else:
        print('skip claude: binary not found')


def check_delta(root):
    """Fake CLI: receipts come one per line, and a failed send resumes its thread instead of opening another."""
    import adapters as A
    import core
    from types import SimpleNamespace as Done
    fresh(root, 'delta')
    created, sent = [], []
    answers = {'contract': {'min': 8, 'max': 8}, 'status': {'status': 'signed_in', 'account': {'account_id': 'u'}},
               'list': {'organizations': [{'id': 'o', 'current': True}]}}

    def fake(cmd, input=None, **_):
        verb = cmd[3]
        if verb == 'create':
            created.append(1)
            return Done(returncode=0, stdout=json.dumps({'thread_id': f't{len(created)}'}), stderr='')
        if verb != 'append':
            return Done(returncode=0, stdout=json.dumps(answers[verb]), stderr='')
        if not sent:
            sent.append(0)
            return Done(returncode=1, stdout='', stderr='down')
        numbers = [json.loads(line)['sequence'] for line in input.splitlines()]
        sent.extend(numbers)
        return Done(returncode=0, stderr='', stdout='\n'.join(
            json.dumps({'sequence': n, 'disposition': 'recorded'}) for n in numbers))

    real, subprocess.run = subprocess.run, fake
    try:
        delta = A.Delta()
        session = core.Session('a', 's', 't', messages=[core.Msg('user', 'x', 1.0), core.Msg('assistant', 'y', 1.0)])
        with contextlib.suppress(RuntimeError):
            delta.create(session)
        assert delta.create(session) == 't1' and len(created) == 1, 'retry opened a second thread'
        assert sent == [0, 1, 2, 3], sent
    finally:
        subprocess.run = real
    print('ok delta')


def check_gui(root):
    """Drives the real window on sandbox stores: copy, bundle out and in, preview, background watcher, login switch."""
    import tkinter as tk
    import winreg
    import adapters as A
    import app
    import core
    box = fresh(root, 'gui')
    os.environ.update(CLAUDE_CONFIG_DIR=str(box / 'claude'), CODEX_HOME=str(box / 'codex'), HERMES_HOME=str(box / 'hermes'),
                      COPILOT_HOME=str(box / 'copilot'), XDG_DATA_HOME=str(box / 'xdg'))
    (box / 'codex/sessions').mkdir(parents=True)
    project = box / 'claude/projects/D--proj'
    project.mkdir(parents=True)

    def seed(sid, age):
        path = project / f'{sid}.jsonl'
        path.write_text('\n'.join(map(json.dumps, old_claude_rows(sid))) + '\n', encoding='utf-8')
        os.utime(path, (time.time() - age,) * 2)

    first, second = '11111111-2222-3333-4444-555555555555', '66666666-2222-3333-4444-555555555555'
    seed(first, 0)
    claude, codex = A.Claude(), A.Codex()
    errors, bundle = [], box / 'keluar.json'

    class Dialogs:
        askyesno = staticmethod(lambda *_: True)
        showinfo = showerror = staticmethod(lambda _title, text: errors.append(text))
        askopenfilename = asksaveasfilename = staticmethod(lambda **_: str(bundle))

    saved = A.registry, app.messagebox, app.filedialog, app.RUN_KEY
    A.registry = lambda scan=True: {'claude': claude, 'codex': codex}  # Keeps real Delta and folder scans out of it.
    app.messagebox = app.filedialog = Dialogs
    app.RUN_KEY = r'Software\SessionBridgeTest\Run'
    window = tk.Tk()
    window.withdraw()
    gui = app.App(window)

    def wait(done, secs=90):
        end = time.time() + secs
        while not done():
            assert not errors, errors
            assert time.time() < end, f'GUI wait timed out; status={gui.status.get()!r} log={said(gui.transfer_log)!r} auto={said(gui.auto_log)!r}'
            window.update()
            time.sleep(.02)

    def said(box_widget):
        return box_widget.get('1.0', 'end')

    def pick_all():
        wait(gui.tree.get_children)
        gui.tree.selection_set(gui.tree.get_children())

    try:
        wait(lambda: gui.harnesses.get_children() and gui.tree.get_children())
        assert list(gui.dst['values']) == [claude.label, codex.label, app.BUNDLE_LABEL]
        gui.src.set(claude.label)
        gui.on_source()
        wait(lambda: gui.count.get() == '1 sesi')
        gui.query.set('tidak ada')
        assert gui.count.get() == '0 dari 1 sesi' and not gui.tree.get_children()
        gui.query.set('')
        pick_all()
        gui.dst.set(codex.label)
        gui.refresh_go()
        assert gui.go['text'].startswith('Pindahkan 1 sesi') and '⇄' in gui.hint.get()
        gui.execute()
        wait(lambda: 'Selesai: 3 pesan' in said(gui.transfer_log))
        assert len(codex.list()) == 1 and [m.title for m in codex.list()] == ['Judul Asli']

        gui.dst.set(app.BUNDLE_LABEL)
        gui.execute()
        wait(lambda: 'tersimpan di' in said(gui.transfer_log))
        assert bundle.exists()

        assert app.BUNDLE_LABEL in gui.src['values'] and app.BUNDLE_LABEL in gui.dst['values']
        gui.src.set(claude.label)
        gui.swap()  # ⇄: bundle becomes the source, Claude the destination.
        assert (gui.src.get(), gui.dst.get()) == (app.BUNDLE_LABEL, claude.label)
        wait(lambda: gui.bundle)
        pick_all()
        gui.execute()
        wait(lambda: '[tetap 0] Judul Asli' in said(gui.transfer_log))  # Back home: already linked, not duplicated.
        wait(lambda: f'sudah ada di {claude.label}' in said(gui.transfer_log))
        assert A.OPENED[-1:] == [claude.list()[0].id], 'a move into Claude opens it in the desktop app, even when already there'
        assert len(claude.list()) == 1 and not gui.list_bar.winfo_manager(), 'loading bar hidden once the list is in'

        seed(second, 3600)
        gui.since.set('0')
        gui.route_src.set(claude.label)
        gui.route_dst.set(codex.label)
        gui.both.set(True)
        gui.add_route()
        assert core.load_cfg()['routes'] == [['claude', 'codex'], ['codex', 'claude']] and gui.routes.size() == 2
        gui.sync(True)
        wait(lambda: 'Akan disalin: 1 sesi, 3 pesan' in said(gui.auto_log))
        assert len(codex.list()) == 1, 'preview writes nothing'

        gui.start_watch()
        wait(lambda: len(codex.list()) == 2)  # The copy comes from the separate --watch process.
        wait(lambda: gui.status.get().endswith(' aktif'))
        gui.stop_watch()
        wait(lambda: gui.status.get().endswith(' mati') and not app.watch_running())  # "otomatis" also contains "mati".

        assert not app.login_enabled()
        app.set_login(True)
        assert app.login_enabled()
        app.set_login(False)
        assert not app.login_enabled()

        gui = gui.set_lang('en')  # Rebuilds the window; the saved language survives a restart.
        assert core.load_cfg()['lang'] == 'en' and gui.status.get().endswith(' off') and gui.go['text'] == 'Choose sessions first'
        gui = gui.set_lang('id')
        assert gui.go['text'] == 'Pilih sesi dulu'
    finally:
        core.LANG = 'id'
        (core.state_dir() / 'STOP').touch()  # A failed run must not leave the watcher behind.
        window.destroy()
        for key in (app.RUN_KEY, app.RUN_KEY.rsplit('\\', 1)[0]):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        A.registry, app.messagebox, app.filedialog, app.RUN_KEY = saved
    print('ok gui')


def check_lang():
    """Every tr() key has an English entry with the same {placeholders}."""
    import ast
    import re
    import adapters as A
    import app
    import core
    keys = {app.DEEPSEEK, app.BUNDLE_LABEL, 'tambah', 'baru', 'sibuk', 'adopsi', 'tetap', 'lewati', 'ekspor'}
    keys |= {getattr(c, k) for c in vars(A).values() for k in ('note', 'after')
             if isinstance(c, type) and isinstance(getattr(c, k, None), str) and getattr(c, k)}
    if not getattr(sys, 'frozen', False):  # The EXE carries no sources to scan.
        for module in (app, core, A):
            for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding='utf-8'))):
                if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'tr' and node.args and isinstance(node.args[0], ast.Constant):
                    keys.add(node.args[0].value)
    assert not keys - set(core.EN), keys - set(core.EN)
    marks = lambda text: set(re.findall(r'\{(\w+)\}', text))
    assert all(marks(k) == marks(v) for k, v in core.EN.items()), [k for k, v in core.EN.items() if marks(k) != marks(v)]
    print('ok lang')


def run(live=False):
    """Returns True on success. Also the body of `SessionBridge.exe --self-test`."""
    logging.disable(logging.CRITICAL)
    root = tempfile.mkdtemp(prefix='bridge-', dir=os.environ.get('BRIDGE_TEST_DIR'))
    saved = dict(os.environ)
    import adapters
    real_open, adapters.OPENED = adapters.desktop_open, []
    adapters.desktop_open = adapters.OPENED.append  # Never the real Claude app.
    try:
        check_lang()
        check_engine(root)
        check_files(root)
        check_delta(root)
        check_gui(root)
        if live:
            check_live(root)
        return True
    finally:
        adapters.desktop_open = real_open
        logging.disable(logging.NOTSET)
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).parent))
    run('--live' in sys.argv)
    print('ALL OK')
