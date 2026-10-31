import json
import os
import subprocess
import sys

from conftest import make_coordinator
from ke.leader import LeaderLock, pid_alive


def test_leader_takeover_after_ttl(tmp_path):
    a, b = LeaderLock(tmp_path / "l"), LeaderLock(tmp_path / "l")
    assert a.acquire(1000) and not b.acquire(1050)
    assert b.acquire(1000 + 121)


def test_acquire_is_idempotent_and_renews_for_the_holder(tmp_path):
    a, b = LeaderLock(tmp_path / "l"), LeaderLock(tmp_path / "l")
    assert a.acquire(1000)
    assert a.acquire(1100)  # still ours, refreshes the heartbeat
    assert not b.acquire(1200)  # 100 s since the refresh: not stale yet
    assert b.acquire(1221)


def test_renew_extends_the_lease(tmp_path):
    a, b = LeaderLock(tmp_path / "l"), LeaderLock(tmp_path / "l")
    assert a.acquire(1000)
    a.renew(1100)
    assert not b.acquire(1150)


def test_renew_by_non_holder_does_not_steal(tmp_path):
    a, b = LeaderLock(tmp_path / "l"), LeaderLock(tmp_path / "l")
    assert a.acquire(1000)
    b.renew(1010)
    assert not b.acquire(1020)
    assert a.acquire(1030)


def test_release_frees_the_lock(tmp_path):
    a, b = LeaderLock(tmp_path / "l"), LeaderLock(tmp_path / "l")
    assert a.acquire(1000)
    a.release()
    assert b.acquire(1001)


def test_release_by_non_holder_keeps_the_lock(tmp_path):
    a, b = LeaderLock(tmp_path / "l"), LeaderLock(tmp_path / "l")
    assert a.acquire(1000)
    b.release()
    assert not b.acquire(1001)


def test_takeover_when_holder_pid_is_dead(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    path = tmp_path / "l"
    path.write_text(json.dumps({"pid": p.pid, "token": "ghost", "ts": 1000}))
    assert not pid_alive(p.pid)
    assert LeaderLock(path).acquire(1001)  # heartbeat is fresh, but the pid is gone


def test_corrupt_lock_file_is_taken_over(tmp_path):
    path = tmp_path / "l"
    path.write_text("not json")
    assert LeaderLock(path).acquire(1000)


def test_holder_pid_reports_live_holder_only(tmp_path):
    a = LeaderLock(tmp_path / "l")
    assert a.holder_pid(1000) is None
    assert a.acquire(1000)
    assert LeaderLock(tmp_path / "l").holder_pid(1010) == os.getpid()
    assert LeaderLock(tmp_path / "l").holder_pid(1000 + 500) is None  # stale heartbeat


def test_pid_alive_for_own_process_and_garbage():
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-5)


def test_pid_alive_does_not_kill_the_target():
    # On Windows os.kill(pid, 0) would TerminateProcess; the check must be side-effect free.
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(p.pid)
        assert pid_alive(p.pid)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()
    assert not pid_alive(p.pid)


def test_only_one_coordinator_evaluates(tmp_path, clock):
    c1 = make_coordinator(tmp_path, clock=clock)
    c2 = make_coordinator(tmp_path, clock=clock)
    calls = []
    c1.register_step("waits", lambda now: calls.append("c1"))
    c2.register_step("waits", lambda now: calls.append("c2"))
    c1.on_tick("matrix", result=None)
    clock.advance(31)
    c2.on_tick("matrix", result=None)  # c1 holds the lock
    assert calls == ["c1"]


def test_fallback_probe_without_psutil():
    from ke.leader import _pid_alive_fallback

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _pid_alive_fallback(os.getpid())
        assert _pid_alive_fallback(p.pid)
        assert p.poll() is None  # probing must not terminate the target
    finally:
        p.kill()
        p.wait()
    assert not _pid_alive_fallback(p.pid)


def test_absent_lock_has_exactly_one_winner(tmp_path):
    import threading

    path = tmp_path / "l"
    locks = [LeaderLock(path) for _ in range(8)]
    barrier = threading.Barrier(len(locks))
    results = []

    def go(lock):
        barrier.wait()
        results.append(lock.acquire(1000))

    threads = [threading.Thread(target=go, args=(lk,)) for lk in locks]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_failed_write_leaves_no_tmp_file(tmp_path, monkeypatch):
    import ke.leader as leader

    path = tmp_path / "l"
    lock = LeaderLock(path)
    assert lock.acquire(1000)

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(leader.os, "replace", boom)
    assert lock.acquire(1010) is False  # replace path fails -> not leader, no exception
    assert [p.name for p in tmp_path.iterdir()] == ["l"]
