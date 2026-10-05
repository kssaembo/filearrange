"""Metadata-only scan, conflict planning and journaled no-overwrite moves."""
import ctypes
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .storage import data_dir

HOLD = '판단보류'
EXCLUDE = '제외'


def native(path):
    value = os.path.abspath(path)
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        return '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return value


def signature(path):
    s = os.stat(native(path), follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
        raise ValueError('일반 파일만 처리할 수 있습니다. 링크는 제외합니다.')
    return [s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino]


def identity(path, sig):
    return hashlib.sha256(json.dumps([os.path.normcase(str(path)), sig[:2]], ensure_ascii=False).encode()).hexdigest()


def under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def protected(path):
    roots = [data_dir()]
    if os.name == 'nt':
        roots += [Path(os.environ.get(k, v)) for k, v in (
            ('WINDIR', 'C:\\Windows'), ('ProgramFiles', 'C:\\Program Files'),
            ('ProgramFiles(x86)', 'C:\\Program Files (x86)'), ('ProgramData', 'C:\\ProgramData'))]
        roots += [Path.home() / 'AppData']
    return any(under(path, root) for root in roots)


@dataclass
class FileRow:
    path: Path
    sig: list
    key: str
    selected: bool = False
    ai: str = ''
    confidence: float | None = None
    reason: str = ''
    final: str = HOLD
    status: str = '미분석'
    manual: bool = False
    permanent: bool = False


def scan(root, recursive, hidden, system, destinations, store, cancel, progress):
    root = Path(root).resolve()
    if protected(root):
        raise ValueError('Windows / Program Files / AppData 등 보호 영역은 정리할 수 없습니다.')
    if not root.is_dir():
        raise ValueError('올바른 폴더를 선택하세요.')
    ignored = store.exclusions()
    rows, warnings = [], []
    def skip(p, is_dir=False):
        try:
            s = p.lstat()
            attrs = getattr(s, 'st_file_attributes', 0)
            return (p.is_symlink() or bool(attrs & 0x400) or protected(p)
                or (not hidden and (p.name.startswith('.') or bool(attrs & 2)))
                or (system and bool(attrs & 4))
                or (is_dir and any(under(p, d) for d in destinations)))
        except OSError:
            return True
    def walk_error(error):
        warnings.append(str(error))
    for folder, dirs, files in os.walk(native(root), followlinks=False, onerror=walk_error):
        if cancel.is_set():
            raise InterruptedError('스캔을 취소했습니다.')
        # Strip long-path prefix only for readable local display; native() restores it for I/O.
        display = folder
        if display.startswith('\\\\?\\UNC\\'):
            display = '\\\\' + display[8:]
        elif display.startswith('\\\\?\\'):
            display = display[4:]
        parent = Path(display)
        dirs[:] = [d for d in dirs if recursive and not skip(parent / d, True)]
        for name in files:
            p = parent / name
            if skip(p):
                continue
            try:
                sig = signature(p)
                key = identity(p, sig)
                row = FileRow(p, sig, key)
                if key in ignored:
                    row.permanent, row.final, row.status = True, EXCLUDE, '항상 제외'
                else:
                    choice = store.get(key, None, 'choices')
                    if choice is not None:
                        row.final, row.manual = choice, True
                        row.selected = choice not in (HOLD, EXCLUDE)
                        row.status = '직접 지정'
                rows.append(row)
            except (OSError, ValueError) as e:
                warnings.append(f'{p.name}: {e}')
        progress(f'파일 {len(rows):,}개 확인 중')
    return rows, warnings


def unique_target(path, reserved=None):
    reserved = reserved if reserved is not None else set()
    path = Path(path)
    candidate, n = path, 0
    while os.path.lexists(native(candidate)) or os.path.normcase(str(candidate)) in reserved:
        n += 1
        suffix = f' ({n})'
        # Keep generated component within common NTFS component limits.
        candidate = path.with_name(path.stem[:max(1, 240-len(path.suffix)-len(suffix))] + suffix + path.suffix)
    reserved.add(os.path.normcase(str(candidate)))
    return candidate


@dataclass
class MovePlan:
    row: FileRow
    target: Path
    renamed: bool


def plan_moves(rows, categories):
    destinations = {c['name']: Path(c['path']).resolve() for c in categories}
    reserved, plans = set(), []
    for row in rows:
        if not row.selected or row.final not in destinations or row.permanent:
            continue
        if row.status in ('분석 실패', '이동 완료', '이동 실패'):
            continue
        if not row.manual and not row.ai:
            continue
        if signature(row.path) != row.sig:
            raise ValueError(f'스캔 이후 변경된 파일입니다. 새로고침하세요: {row.path.name}')
        dest = destinations[row.final]
        if protected(dest):
            raise ValueError('보호 영역으로 이동할 수 없습니다.')
        if row.path.parent.resolve() == dest:
            continue
        proposed = dest / row.path.name
        target = unique_target(proposed, reserved)
        plans.append(MovePlan(row, target, target != proposed))
    return plans


def move_no_replace(src, dst):
    """OS move only; never open file contents for classification or hashing.

    Windows MoveFileEx COPY_ALLOWED supports cross-volume moves; REPLACE_EXISTING
    is intentionally absent. Portable test fallback is limited to same-volume.
    """
    if os.name == 'nt':
        fn = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        fn.restype = ctypes.c_int
        if not fn(native(src), native(dst), 2 | 8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.link(native(src), native(dst), follow_symlinks=False)
        os.unlink(native(src))  # Completes a move, never a user-facing deletion operation.


def execute(plans, store, cancel, progress):
    if store.last_job() is not None and any(r['state'] in ('pending','undo_pending','uncertain') for r in store.records()):
        raise ValueError('중단된 이동 기록을 먼저 확인하세요. 마지막 정리 취소를 실행하세요.')
    job = store.new_job()
    results = []
    for i, plan in enumerate(plans):
        if cancel.is_set():
            break
        row, dst = plan.row, plan.target
        mid = None
        try:
            if protected(row.path) or protected(dst) or signature(row.path) != row.sig:
                raise ValueError('파일 또는 경로가 변경되어 이동하지 않았습니다.')
            Path(native(dst.parent)).mkdir(parents=True, exist_ok=True)
            # Journal before any file mutation, including failure-prone cross-volume moves.
            mid = store.journal(job, row.path, dst, row.sig)
            move_no_replace(row.path, dst)
            after = signature(dst)
            if os.path.lexists(native(row.path)):
                raise ValueError('원본과 대상이 함께 존재합니다. 기록 확인이 필요합니다.')
            store.update_move(mid, 'done', after)
            results.append((row.key, '이동 완료', str(dst)))
        except Exception as e:
            if mid is not None:
                state = 'failed' if os.path.lexists(native(row.path)) and not os.path.lexists(native(dst)) else 'uncertain'
                store.update_move(mid, state, error=str(e))
            results.append((row.key, '이동 실패', str(e)))
        progress(f'이동 {i+1}/{len(plans)}')
    return results


def undo(store, cancel, progress):
    job = store.last_job()
    if job is None:
        return ['취소할 이동 기록이 없습니다.']
    messages = []
    for record in store.records(job):
        if cancel.is_set():
            break
        if record['state'] in ('undone', 'failed'):
            continue
        src, dst = Path(record['src']), Path(record['dst'])
        mid = record['id']
        try:
            # Interrupted operations are deliberately never guessed from filename/size alone.
            if record['state'] in ('pending', 'uncertain'):
                if src.exists() and not dst.exists() and signature(src) == json.loads(record['before']):
                    store.update_move(mid, 'failed', error='이동 전 중단됨')
                    continue
                raise ValueError('중단된 작업: 이동 로그의 두 경로를 탐색기에서 확인해야 합니다.')
            if record['state'] == 'undo_pending':
                raise ValueError('복원 중 중단됨: 로그의 복원 경로를 직접 확인해야 합니다.')
            if protected(src) or protected(dst):
                raise ValueError('보호 경로로 변경되어 복원을 중지했습니다.')
            if signature(dst) != json.loads(record['after']):
                raise ValueError('이동 후 수정되거나 바뀐 파일입니다. 자동 복원을 생략합니다.')
            Path(native(src.parent)).mkdir(parents=True, exist_ok=True)
            restore = unique_target(src)
            store.update_move(mid, 'undo_pending', restored=restore)
            try:
                move_no_replace(dst, restore)
            except Exception:
                if dst.exists() and not restore.exists():
                    store.update_move(mid, 'done')
                raise
            store.update_move(mid, 'undone', restored=restore)
            messages.append(f'복원: {restore}' + (' (이름 충돌로 번호 추가)' if restore != src else ''))
        except Exception as e:
            messages.append(f'복원 보류: {dst.name}: {e}')
        progress(f'복원 확인: {dst.name}')
    return messages
