"""Durable job status, one host browser at a time, owner-scoped results."""
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from engine import research, validate_input
from web_sampling import collect, SamplingError, query_plan
from analysis_core import attach_search_suggestions


class JobBusy(Exception):
    pass


@contextmanager
def connect(path, **kwargs):
    connection = sqlite3.connect(path, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class JobManager:
    def __init__(self, database, data_dir, adapter_file, collector=None, sessions=None):
        self.database, self.data_dir, self.adapter_file = database, data_dir, adapter_file
        self.collector = collector or collect
        self.sessions = sessions
        self.lock = threading.RLock()
        self.thread = None
        self.cancelled = threading.Event()
        self.active = None
        self.next_start = 0
        with connect(database) as db:
            db.execute('CREATE TABLE IF NOT EXISTS sampling_jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS job_owner_created ON sampling_jobs(owner,created DESC)')
            for job_id, payload in db.execute("SELECT id,payload FROM sampling_jobs WHERE status IN ('queued','running')").fetchall():
                state = json.loads(payload)
                state.update(status='failed', code='server_restarted', message='服务已重启，之前的采样未完成。请手动重新提交。')
                db.execute('UPDATE sampling_jobs SET status=?,payload=? WHERE id=?', ('failed', json.dumps(state, ensure_ascii=False), job_id))

    def get(self, job_id, owner):
        with connect(self.database) as db:
            row = db.execute('SELECT payload FROM sampling_jobs WHERE id=? AND owner=?', (job_id, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, owner):
        with connect(self.database) as db:
            row = db.execute('SELECT payload FROM sampling_jobs WHERE owner=? ORDER BY created DESC,id DESC LIMIT 1', (owner,)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, job_id, **changes):
        with connect(self.database, timeout=10) as db:
            state = json.loads(db.execute('SELECT payload FROM sampling_jobs WHERE id=?', (job_id,)).fetchone()[0])
            state.update(changes)
            db.execute('UPDATE sampling_jobs SET status=?,payload=? WHERE id=?', (state['status'], json.dumps(state, ensure_ascii=False), job_id))

    def submit(self, owner, value):
        value, _ = validate_input(value)
        if value['mode'] != 'online':
            raise ValueError('网页采样任务只接受在线模式。')
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise JobBusy('部署电脑正在执行一个采样任务，请稍后重试。词包拓展和导入不受影响。')
            if time.monotonic() < self.next_start:
                raise JobBusy('网页采样需间隔至少60秒，请稍后重试。')
            created = datetime.now(timezone.utc).isoformat()
            job_id = secrets.token_hex(16)
            state = {'id': job_id, 'input': value, 'createdAt': created, 'status': 'queued',
                     'message': '等待专用浏览器启动', 'plan': query_plan(value)}
            with connect(self.database, timeout=10) as db:
                if db.execute('SELECT COUNT(*) FROM sampling_jobs').fetchone()[0] >= 2000:
                    raise JobBusy('已达到2000次采样任务上限，请管理员先备份，再扩容。')
                if db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] >= 5000:
                    raise JobBusy('历史记录已达到5000次上限，请先备份再扩容。')
                db.execute('INSERT INTO sampling_jobs VALUES (?,?,?,?,?)', (job_id, owner, created, 'queued', json.dumps(state, ensure_ascii=False)))
            self.cancelled = threading.Event()
            self.active = (job_id, owner)
            self.next_start = time.monotonic() + 60
            self.thread = threading.Thread(target=self.execute, args=(state, owner), daemon=True)
            self.thread.start()
            return state

    def cancel(self, job_id, owner):
        with self.lock:
            if self.active != (job_id, owner) or not self.thread or not self.thread.is_alive():
                return False
            self.cancelled.set()
            return True

    def cancel_owner(self, owner):
        """Atomically cancel this owner's currently running job, if any."""
        with self.lock:
            if not self.active or self.active[1] != owner or not self.thread or not self.thread.is_alive():
                return False
            self.cancelled.set()
            return True

    def execute(self, state, owner):
        job_id = state['id']
        try:
            self.update(job_id, status='running', message='正在检查小红书网页访问状态')
            value = {**state['input'], '_window':state['plan']['window']}
            progress = lambda msg: self.update(job_id, message=msg)
            if self.sessions:
                notes, sampling = self.sessions.sample(owner, value, progress, self.cancelled)
            else:
                notes, sampling = self.collector(value, self.data_dir, self.adapter_file, progress, self.cancelled)
            result = research(state['input'], notes, now=datetime.fromisoformat(state['createdAt']))
            attach_search_suggestions(result, sampling)
            partial = sampling.get('complete') is False
            if partial:
                result['status'] = 'partial'
                for row in result['rows']:
                    row['rank'] = None
                    row['index'] = None
            final_status = 'partial' if partial else 'completed'
            final_message = '部分样本已保存，不能作为完整热度榜' if partial else '采样分析已保存'
            result.update(id=secrets.token_hex(16), createdAt=datetime.now(timezone.utc).isoformat(),
                          provider=sampling.get('provider', '小红书网页可见样本（无API）'), sampling=sampling)
            with self.lock:
                if self.cancelled.is_set():
                    raise SamplingError('cancelled', '任务已取消，没有保存不完整结果。')
                # Atomic result and job completion, no half-saved histories.
                with connect(self.database, timeout=10) as db:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] >= 5000:
                        raise SamplingError('storage_full', '历史记录已满，请管理员备份并扩容。')
                    db.execute('INSERT INTO runs VALUES (?,?,?,?)', (result['id'], owner, result['createdAt'], json.dumps(result, ensure_ascii=False)))
                    state.update(status=final_status, message=final_message, resultId=result['id'], sampling=sampling)
                    db.execute('UPDATE sampling_jobs SET status=?,payload=? WHERE id=?', (final_status, json.dumps(state, ensure_ascii=False), job_id))
        except SamplingError as error:
            self.update(job_id, status='cancelled' if error.code == 'cancelled' else 'failed', code=error.code, message=str(error))
        except Exception:
            # Never return browser internals, page credentials or profile paths to LAN clients.
            self.update(job_id, status='failed', code='sampling_failed', message='网页采样或历史保存失败，没有生成新榜单。请检查部署电脑网络、浏览器和磁盘空间。')
        finally:
            with self.lock:
                self.active = None
