"""离线测试网络禁用；仅允许 Windows 事件循环的标准库自连管道。"""
from __future__ import annotations

import socket
import sys

_SOCKETPAIR_CODE = getattr(getattr(socket, "_fallback_socketpair", None), "__code__", None)


def prohibit_network(event, arguments):
    if event not in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        return
    if sys.platform == "win32" and event == "socket.connect":
        frame = sys._getframe(1)
        if _SOCKETPAIR_CODE is not None and frame.f_code is _SOCKETPAIR_CODE:
            values = frame.f_locals
            address = (values.get("addr"), values.get("port"))
            if (len(arguments) == 2
                    and arguments[0] is values.get("csock")
                    and arguments[1] == address
                    and address[0] in {"127.0.0.1", "::1"}
                    and values.get("lsock") is not None
                    and values["lsock"].getsockname()[:2] == address):
                return
    raise RuntimeError("offline_test_network_forbidden")
