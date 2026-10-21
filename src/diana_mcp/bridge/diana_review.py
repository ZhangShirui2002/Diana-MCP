"""Native modeless review panel for DianaIE 10.5 (Python 3.7, no Qt bindings).

DianaIE itself is Qt based, but its embedded Python installation exposes neither PyQt nor PySide.  This module
therefore creates a small owned Win32 window with read-only review text and native buttons.  The window is owned by
the DianaIE main window, remains modeless so the reviewer can rotate/inspect the 3D model, and writes a decision
only from a real button callback on DianaIE's GUI thread.
"""
import ctypes
import json
import os
import time
import uuid
from ctypes import wintypes


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError("invalid review record")
    return value


def _atomic_write(path, value):
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def apply_decision(record_path, audit_path, action, confirmation):
    """The sole supported decision writer, called by a native DianaIE button callback."""
    record_path, audit_path = str(record_path), str(audit_path)
    confirmation = str(confirmation).strip()
    if not confirmation:
        raise RuntimeError("请先填写人工确认说明")
    record = _load(record_path)
    if record.get("status") != "pending":
        raise RuntimeError("当前对象状态为 %s，不能重复决定" % record.get("status"))
    if record.get("kind") == "review":
        allowed = ("approve_once", "approve_self_check", "approve_baseline", "reject")
    elif record.get("kind") == "campaign":
        allowed = ("approve_campaign", "reject")
    else:
        raise RuntimeError("不支持的待审对象类型：%s" % record.get("kind"))
    if action not in allowed:
        raise RuntimeError("不支持的决定：%s" % action)
    record.update({
        "status": "rejected" if action == "reject" else "approved",
        "decision": action,
        "confirmation": confirmation,
        "decided": time.time(),
        "decision_source": "diana_gui",
    })
    _atomic_write(record_path, record)
    folder = os.path.dirname(audit_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    event = {
        "time": time.time(),
        "event": record["kind"] + ".decided",
        "object_id": record["id"],
        "action": action,
        "source": "diana_gui",
        "confirmation": confirmation,
    }
    with open(audit_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return record


def _pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def render_record(record_path):
    """Full, untruncated review text used by both unit tests and the native panel."""
    record = _load(str(record_path))
    lines = [
        "DIANA-MCP 计算前人工审阅",
        "=" * 72,
        "对象编号：%s" % record.get("id"),
        "状态：%s" % record.get("status"),
        "类型：%s" % record.get("kind"),
    ]
    if record.get("kind") == "campaign":
        lines += [
            "基准模型：%s" % record.get("baseline_id"),
            "方案名称：%s" % record.get("name"),
            "\r\n[允许参数与范围]\r\n" + _pretty(record.get("parameters") or []),
            "\r\n[组合方式]\r\n" + str(record.get("combination") or ""),
            "\r\n[提交前自检]\r\n" + _pretty(record.get("self_checks") or []),
            "\r\n[求解后质量检查]\r\n" + _pretty(record.get("quality_checks") or []),
        ]
        return "\r\n".join(lines)

    lines += [
        "项目：%s" % record.get("project"),
        "分析：%s" % record.get("analysis"),
        "DIANA：%s" % record.get("diana_version"),
        "\r\n[单位]\r\n" + _pretty(record.get("units") or {}),
        "\r\n[validateModel('ALL')]\r\n" + _pretty(record.get("validation_issues") or []),
        "\r\n[相对上次计算的变化]\r\n" + _pretty(record.get("changes") or []),
    ]
    snapshot = _load(record["snapshot"])
    with open(record["dcf"], "r", encoding="utf-8", errors="replace") as stream:
        commands = stream.read()
    lines += [
        "\r\n[完整模型状态]\r\n" + _pretty(snapshot),
        "\r\n[完整求解命令 .dcf]\r\n" + commands,
        "\r\n请保持此窗口打开，并在 DianaIE 三维窗口中旋转、缩放和核对模型后再作决定。",
    ]
    return "\r\n".join(lines)


# Win32 constants.  Defined here instead of importing a GUI package because DianaIE 10.5 ships none.
WM_CLOSE, WM_DESTROY, WM_SIZE, WM_COMMAND = 0x0010, 0x0002, 0x0005, 0x0111
WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_CHILD = 0x00CF0000, 0x10000000, 0x40000000
WS_VSCROLL, WS_HSCROLL = 0x00200000, 0x00100000
ES_MULTILINE, ES_AUTOVSCROLL, ES_AUTOHSCROLL, ES_READONLY = 0x0004, 0x0040, 0x0080, 0x0800
BS_PUSHBUTTON = 0x00000000
SW_SHOW, EM_SETLIMITTEXT = 5, 0x00C5

_WINDOWS = {}
_WNDPROC = None
_CLASS = None
_WIN32_CONFIGURED = False


def _configure_win32(user32, kernel32):
    """Declare pointer-sized Win32 signatures (ctypes otherwise assumes 32-bit ``int`` results)."""
    global _WIN32_CONFIGURED
    if _WIN32_CONFIGURED:
        return
    LRESULT = ctypes.c_ssize_t
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    # Python 3.7's ctypes.wintypes has no HCURSOR alias; cursors are HANDLE-sized.
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _WIN32_CONFIGURED = True


def _main_window(user32):
    result = []
    pid = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            result.append(hwnd)
        return True

    callback = callback_type(collect)
    user32.EnumWindows(callback, 0)
    return result[0] if result else 0


def _window_text(user32, hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _set_status(user32, state, text):
    user32.SetWindowTextW(state["status"], text)


def _layout(user32, hwnd, state):
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = max(600, rect.right), max(500, rect.bottom)
    user32.MoveWindow(state["text"], 10, 10, width - 20, height - 125, True)
    user32.MoveWindow(state["confirmation"], 10, height - 105, width - 20, 24, True)
    actions = state["buttons"]
    button_width = max(120, (width - 20 - 8 * max(0, len(actions) - 1)) // max(1, len(actions)))
    x = 10
    for button in actions.values():
        user32.MoveWindow(button, x, height - 72, button_width, 28, True)
        x += button_width + 8
    user32.MoveWindow(state["status"], 10, height - 35, width - 20, 22, True)


def _ensure_class(user32, kernel32):
    global _WNDPROC, _CLASS
    if _CLASS is not None:
        return _CLASS
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def procedure(hwnd, message, wparam, lparam):
        state = _WINDOWS.get(int(hwnd))
        if message == WM_SIZE and state:
            _layout(user32, hwnd, state)
            return 0
        if message == WM_COMMAND and state:
            control_id = int(wparam) & 0xFFFF
            action = state["action_ids"].get(control_id)
            if action:
                try:
                    confirmation = _window_text(user32, state["confirmation"])
                    record = apply_decision(state["record"], state["audit"], action, confirmation)
                    _set_status(user32, state, "已记录人工决定：%s（可以关闭此窗口）" % record["decision"])
                    for button in state["buttons"].values():
                        user32.EnableWindow(button, False)
                except Exception as exc:
                    _set_status(user32, state, "未记录：%s" % exc)
                return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            _WINDOWS.pop(int(hwnd), None)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    _WNDPROC = WNDPROC(procedure)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    _CLASS = "DianaMcpReviewWindow_%d" % os.getpid()
    wc = WNDCLASSW()
    wc.lpfnWndProc = _WNDPROC
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hCursor = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR))
    wc.hbrBackground = ctypes.c_void_p(5 + 1)
    wc.lpszClassName = _CLASS
    if not user32.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:
        raise ctypes.WinError(ctypes.get_last_error())
    return _CLASS


def show_review_panel(record_path, audit_path):
    """Open or focus a modeless native panel owned by DianaIE. Returns its HWND."""
    if os.name != "nt":
        raise RuntimeError("the DianaIE review panel currently requires Windows")
    record_path, audit_path = str(record_path), str(audit_path)
    record = _load(record_path)
    for hwnd, state in list(_WINDOWS.items()):
        if state["record"] == record_path:
            ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return hwnd

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    _configure_win32(user32, kernel32)
    class_name = _ensure_class(user32, kernel32)
    owner = _main_window(user32)
    title = "DIANA-MCP 计算前审阅 - " + str(record.get("analysis") or record.get("name") or record["id"])
    hwnd = user32.CreateWindowExW(0, class_name, title, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  80, 80, 980, 820, owner, None, kernel32.GetModuleHandleW(None), None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    text = user32.CreateWindowExW(0x00000200, "EDIT", render_record(record_path),
                                  WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_HSCROLL | ES_MULTILINE |
                                  ES_AUTOVSCROLL | ES_AUTOHSCROLL | ES_READONLY,
                                  0, 0, 0, 0, hwnd, wintypes.HMENU(1001), None, None)
    user32.SendMessageW(text, EM_SETLIMITTEXT, 16 * 1024 * 1024, 0)
    confirmation = user32.CreateWindowExW(0x00000200, "EDIT", "我已在 DianaIE 中核对模型和设置",
                                          WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL,
                                          0, 0, 0, 0, hwnd, wintypes.HMENU(1002), None, None)
    if record.get("kind") == "campaign":
        labels = [("批准参数分析方案", "approve_campaign"), ("拒绝", "reject")]
    else:
        labels = [("批准本次调试计算", "approve_once"),
                  ("授权本次自检后计算", "approve_self_check"),
                  ("批准为基准候选并验证求解", "approve_baseline"), ("拒绝", "reject")]
    buttons, action_ids = {}, {}
    for index, (label, action) in enumerate(labels):
        control_id = 2001 + index
        button = user32.CreateWindowExW(0, "BUTTON", label, WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                        0, 0, 0, 0, hwnd, wintypes.HMENU(control_id), None, None)
        buttons[action] = button
        action_ids[control_id] = action
    status = user32.CreateWindowExW(0, "STATIC", "等待人工审阅；关闭窗口不会批准。", WS_CHILD | WS_VISIBLE,
                                    0, 0, 0, 0, hwnd, wintypes.HMENU(1003), None, None)
    state = {"record": record_path, "audit": audit_path, "text": text, "confirmation": confirmation,
             "buttons": buttons, "action_ids": action_ids, "status": status}
    _WINDOWS[int(hwnd)] = state
    _layout(user32, hwnd, state)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(owner or hwnd)
    return hwnd


if globals().get("DIANA_REVIEW_RECORD"):
    print(show_review_panel(globals()["DIANA_REVIEW_RECORD"], globals()["DIANA_REVIEW_AUDIT"]))
