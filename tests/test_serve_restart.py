# -*- coding: utf-8 -*-
"""serve 实例检测/自动重启（单实例守护）测试。"""
import socket
import subprocess
import sys
import time

import pytest

from cn_llm_router.serve import (
    _find_pid_on_port,
    _is_router_process,
    _kill_pid,
    _port_in_use,
    _restart_existing_if_needed,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestPortInUse:
    def test_free_port(self):
        port = _free_port()
        assert _port_in_use("127.0.0.1", port) is False

    def test_occupied_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            assert _port_in_use("127.0.0.1", port) is True
        finally:
            s.close()


class TestFindPid:
    def test_find_own_listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            pid = _find_pid_on_port(port)
            assert pid is not None
            assert pid == __import__("os").getpid() or pid > 0
        finally:
            s.close()

    def test_free_port_no_pid(self):
        port = _free_port()
        assert _find_pid_on_port(port) is None


class TestIsRouterProcess:
    def test_marker_process(self):
        # 起一个命令行含 "cn_llm_router serve" 标记的短命子进程
        code = (
            "import sys,time; "
            "print('cn_llm_router serve marker'); "
            "sys.stdout.flush(); time.sleep(5)"
        )
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Popen 返回后子进程可能尚未完成 exec，/proc/<pid>/cmdline 可能为空——轮询等待
            ok = False
            for _ in range(50):
                if _is_router_process(p.pid):
                    ok = True
                    break
                time.sleep(0.1)
            assert ok
        finally:
            _kill_pid(p.pid)

    def test_non_router_process(self):
        # 自身（pytest）命令行不含 serve 标记
        assert _is_router_process(__import__("os").getpid()) is False


class TestRestartExisting:
    def test_free_port_noop(self):
        port = _free_port()
        _restart_existing_if_needed("127.0.0.1", port, restart=True)  # 不抛异常

    def test_occupied_by_router_killed(self):
        """端口被真实 serve 子进程占用 → 自动关闭后重启（进程被终止）。"""
        port = _free_port()
        # 起一个真实 serve 实例（新代码自带重启逻辑；这里用 run_server 子进程）
        proc = subprocess.Popen(
            [sys.executable, "-m", "cn_llm_router", "serve", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # 等待 bind
            for _ in range(50):
                if _port_in_use("127.0.0.1", port):
                    break
                time.sleep(0.1)
            assert _port_in_use("127.0.0.1", port)
            pid = _find_pid_on_port(port)
            assert pid is not None and _is_router_process(pid)
            _restart_existing_if_needed("127.0.0.1", port, restart=True)
            # 旧进程应已被终止，端口释放
            for _ in range(50):
                if not _port_in_use("127.0.0.1", port):
                    break
                time.sleep(0.1)
            assert _port_in_use("127.0.0.1", port) is False
        finally:
            _kill_pid(proc.pid)

    def test_occupied_by_other_raises(self):
        """端口被非本应用进程占用 → SystemExit。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            with pytest.raises(SystemExit):
                _restart_existing_if_needed("127.0.0.1", port, restart=True)
        finally:
            s.close()
