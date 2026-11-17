#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
极简 Endpoint LSP —— 只实现 workspace/symbol，Java(Spring) 与 Python(FastAPI) 共用一套壳。

原理：把每个请求映射拼成完整路径（类/路由级前缀 + 方法级路径），当作「符号」返回。
Zed 的 Cmd+T（Go to Symbol in Project）走 workspace/symbol，于是它直接变成端点导航器。

语言通过 `--lang java|python` 指定，两种语言各注册一个语言服务器，
互不扫描对方的文件，也不会在结果里出现重复项。

只用标准库，走 stdio。用法：
    endpoint_lsp.py --lang python           # 作为 LSP 由 Zed 拉起
    endpoint_lsp.py --dump . --lang python  # 直接打印索引，人工核对用
"""

import json
import os
import re
import sys
from urllib.parse import quote, unquote, urlparse

# ---------------------------------------------------------------- 通用

SKIP_DIRS = {
    "target", "build", "out", "dist", "node_modules", ".git", ".idea",
    "venv", ".venv", "env", ".env", "__pycache__", "site-packages",
    "migrations", "alembic", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

# Python 侧额外跳过测试目录：那里的 FastAPI 实例只是测试夹具，会污染导航结果
PY_SKIP_DIRS = SKIP_DIRS | {"tests", "test"}


def join_path(*parts):
    """拼接多级路径，处理首尾斜杠；空段自动忽略。"""
    segs = []
    for p in parts:
        if not p:
            continue
        segs.extend(s for s in p.strip().strip("/").split("/") if s)
    return "/" + "/".join(segs) if segs else "/"


def _balanced(text, start, opener="(", closer=")"):
    """从 start（指向起始符号之后）取到配平的结束符，返回 (内容串, 结束符位置)。"""
    depth = 1
    i = start
    n = len(text)
    while i < n and depth:
        c = text[i]
        if c in "\"'":  # 跳过字符串字面量，避免里面的括号干扰配平
            quote = c
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                i += 1
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[start:i], i
        i += 1
    return text[start:i], i


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------- Java / Spring

MAPPING_ANNOT = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b")
CLASS_DECL = re.compile(r"\bclass\s+(\w+)")
HTTP_BY_ANNOT = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Delete": "DELETE",
    "Patch": "PATCH",
    "Request": None,  # 靠 method = RequestMethod.X 推断
}


def parse_annotations(text):
    """返回 [(起始偏移, 结束偏移, HTTP 方法或 None, [路径...])]。"""
    out = []
    for m in MAPPING_ANNOT.finditer(text):
        pos = m.end()
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        if pos >= len(text) or text[pos] != "(":
            continue
        args, end = _balanced(text, pos + 1)
        paths = re.findall(r'"((?:[^"\\]|\\.)*)"', args)
        verbs = re.findall(r"RequestMethod\.(\w+)", args)
        http = verbs[0].upper() if verbs else HTTP_BY_ANNOT.get(m.group(1))
        out.append((m.start(), end, http, paths))
    return out


def find_java_method(text, start):
    """从注解结束处往下找方法名：跳过后续注解行，取第一个含 '(' 的签名行。"""
    for line in text[start:].split("\n"):
        s = line.strip()
        if not s or s[0] in "@*/" or s.startswith("//"):
            continue
        m = re.search(r"(\w+)\s*\(", s)
        if m:
            return m.group(1)
        if ";" in s or "{" in s:
            return None
    return None


def index_java_file(path):
    """解析单个 Controller 文件，返回 [(符号名, 容器名, uri, 行号0基)]。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    if "Mapping" not in text:
        return []

    cls = CLASS_DECL.search(text)
    cls_name = cls.group(1) if cls else os.path.splitext(os.path.basename(path))[0]
    split_at = cls.start() if cls else 0

    class_paths, method_annos = [], []
    for start, end, http, paths in parse_annotations(text):
        if start < split_at:  # 类声明之前的 @RequestMapping = 类级前缀
            class_paths.extend(paths or [""])
        else:
            method_annos.append((start, end, http, paths))

    if not method_annos:
        return []
    if not class_paths:
        class_paths = [""]

    uri = "file://" + quote(os.path.abspath(path))
    symbols = []
    for start, end, http, paths in method_annos:
        line0 = text.count("\n", 0, start)
        mname = find_java_method(text, end)
        container = f"{cls_name}#{mname}" if mname else cls_name
        for prefix in class_paths:
            for p in paths or [""]:
                symbols.append((f"{http or 'ANY'} {join_path(prefix, p)}", container, uri, line0))
    return symbols


# ---------------------------------------------------------------- Python / FastAPI

PY_DECORATOR = re.compile(
    r"@\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*\.\s*"
    r"(get|post|put|delete|patch|head|options|api_route)\s*\("
)
PY_ROUTER_ASSIGN = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*(?:fastapi\s*\.\s*)?(APIRouter|FastAPI)\s*\(",
    re.M,
)
PY_INCLUDE_ROUTER = re.compile(
    r"([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*\.\s*include_router\s*\("
)
PY_FROM_IMPORT = re.compile(
    r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([\s\S]*?\)|[^\n]+)", re.M
)
PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+)(?:[ \t]+as[ \t]+([A-Za-z_]\w*))?[ \t]*$", re.M)
PY_DEF = re.compile(r"\bdef[ \t]+([A-Za-z_]\w*)[ \t]*\(")
PY_CONST = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?=[ \t]*[rRuUfF]?[\"']([^\"'\n]*)[\"'][ \t]*(?:#.*)?$", re.M
)
# FastMCP 工具：@mcp.tool 或 @mcp.tool(name="...")，后面没有 HTTP 路径
PY_MCP_TOOL = re.compile(
    r"@\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*\.\s*tool\b\s*(\(?)"
)
# Starlette 子应用挂载：app.mount("/mcp/xxx", sub_app)
PY_MOUNT = re.compile(r"([A-Za-z_]\w*)\s*\.\s*mount\s*\(")
# for _path, _sub in MCP_APPS.items():  用于把循环变量还原成字典/元组里的字面量
PY_FOR = re.compile(
    r"^[ \t]*for[ \t]+([\w \t,]+?)[ \t]+in[ \t]+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?:\.items\(\))?[ \t]*:",
    re.M,
)
PY_TRIPLE = re.compile(r"(\"\"\"|''')[\s\S]*?\1")


def _split_top(s):
    """按顶层逗号切分参数串，忽略括号和字符串内部的逗号。"""
    parts, buf, depth, i, n = [], [], 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            q = c
            buf.append(c)
            i += 1
            while i < n and s[i] != q:
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i])
                    i += 1
                buf.append(s[i])
                i += 1
            if i < n:
                buf.append(s[i])
        elif c in "([{":
            depth += 1
            buf.append(c)
        elif c in ")]}":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts]


def _literal(seg):
    """参数段是字符串字面量（含 f-string）时返回内容，否则 None。

    f-string 原样返回模板文本，`f"/{kind}"` 会显示成 `/{kind}`——比丢掉路径更可读。
    """
    m = re.match(r"^([rRuUfF]?)[\"'](.*?)[\"']$", (seg or "").strip(), re.S)
    if not m:
        return None
    body = m.group(2)
    if "f" in m.group(1).lower():
        body = body.replace("{{", "{").replace("}}", "}")
    return body


def _kw_string(args, key):
    m = re.search(r"\b%s\s*=\s*[rRuUfF]?[\"'](.*?)[\"']" % key, args, re.S)
    return m.group(1) if m else None


def _kw_value(args, key, consts):
    """取 key=... 的字符串值，支持直接引用本文件的字符串常量（prefix=PREFIX）。"""
    val = _kw_string(args, key)
    if val is not None:
        return val
    m = re.search(r"\b%s\s*=\s*([A-Za-z_]\w*)\s*(?:,|\)|$)" % key, args, re.M)
    return consts.get(m.group(1), "") if m else ""


def _kw_string_list(args, key):
    m = re.search(r"\b%s\s*=\s*[\[(]([^\])]*)[\])]" % key, args, re.S)
    if not m:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))


def _parse_constants(text):
    """模块级字符串常量，用来解析 prefix=PREFIX / @router.get(API_PATH) 这类写法。"""
    return {m.group(1): m.group(2) for m in PY_CONST.finditer(text)}


def _loop_vars(text):
    """循环变量 -> 被遍历的容器名，用于 mount 里 `app.mount(_path, ...)` 这种写法。"""
    out = {}
    for m in PY_FOR.finditer(text):
        for var in m.group(1).split(","):
            var = var.strip()
            if var:
                out[var] = m.group(2)
    return out


def _iterable_strings(text, name):
    """取模块级 dict 的键或 list/tuple 的字符串元素。"""
    m = re.search(
        r"^[ \t]*%s[ \t]*(?::[^=\n]+)?=[ \t]*([\{\[\(])" % re.escape(name), text, re.M
    )
    if not m:
        return []
    opener = m.group(1)
    closer = {"{": "}", "[": "]", "(": ")"}[opener]
    body, _ = _balanced(text, m.end(), opener, closer)
    out = []
    for seg in _split_top(body):
        seg = seg.strip()
        lit = _literal(seg.split(":", 1)[0]) if opener == "{" else _literal(seg)
        if lit is not None:
            out.append(lit)
    return out


def _parse_imports(text):
    """alias -> (module, imported_name, level)。imported_name 为 None 表示 import 模块。"""
    imports = {}
    for m in PY_FROM_IMPORT.finditer(text):
        dots, module, names = m.group(1), m.group(2), m.group(3)
        for nm in _split_top(names.strip("()")):
            nm = nm.strip()
            if not nm or nm == "*":
                continue
            orig, _, alias = nm.partition(" as ")
            orig = orig.strip()
            alias = alias.strip() or orig
            if orig:
                imports[alias] = (module, orig, len(dots))
    for m in PY_IMPORT.finditer(text):
        module, alias = m.group(1), m.group(2)
        imports[alias or module.split(".")[0]] = (module, None, 0)
    return imports


def index_py_file(path, root):
    """解析单个 FastAPI 文件，返回跨文件拼前缀所需的中间结构；无内容时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    # 把三引号字符串（文档字符串）等长替换成空格，避免示例代码被当成真实路由
    text = PY_TRIPLE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), raw)
    if (
        "APIRouter" not in text
        and "FastAPI" not in text
        and not PY_DECORATOR.search(text)
        and not PY_MCP_TOOL.search(text)
        and not PY_MOUNT.search(text)
    ):
        return None

    rel = os.path.relpath(path, root).replace(os.sep, "/")
    module_parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
    is_pkg = module_parts[-1] == "__init__"
    if is_pkg:
        module_parts = module_parts[:-1]

    consts = _parse_constants(text)
    loops = _loop_vars(text)
    info = {
        "path": os.path.abspath(path),
        "rel": rel,
        "module": ".".join(module_parts),
        "module_parts": module_parts,
        "is_pkg": is_pkg,
        "routers": {},    # 变量名 -> 自身 prefix
        "endpoints": [],  # (行号0基, [HTTP 方法], [路径], 函数名, 接收者变量)
        "includes": [],   # (父变量, 子表达式, include 前缀)
        "mounts": [],     # (行号0基, 挂载路径, 宿主变量)
        "tools": [],      # (行号0基, 工具名, 函数名)
        "imports": _parse_imports(text),
    }

    for m in PY_ROUTER_ASSIGN.finditer(text):
        args, _ = _balanced(text, m.end())
        info["routers"][m.group(1)] = _kw_value(args, "prefix", consts)

    for m in PY_INCLUDE_ROUTER.finditer(text):
        args, _ = _balanced(text, m.end())
        child = _split_top(args)[0] if args.strip() else ""
        info["includes"].append((m.group(1).replace(" ", ""), child, _kw_value(args, "prefix", consts)))

    for m in PY_DECORATOR.finditer(text):
        args, end = _balanced(text, m.end())
        segs = _split_top(args)
        path = _literal(segs[0]) if segs else None
        if path is None and segs:
            path = consts.get(segs[0].strip())
        if path is None:
            path = _kw_value(args, "path", consts) or None
        verb = m.group(2)
        if verb == "api_route":
            methods = [v.upper() for v in _kw_string_list(args, "methods")] or ["ANY"]
        else:
            methods = [verb.upper()]
        fn = PY_DEF.search(text, end)
        line0 = text.count("\n", 0, m.start())
        info["endpoints"].append((line0, methods, [path or ""], fn.group(1) if fn else "", m.group(1).replace(" ", "")))

    for m in PY_MOUNT.finditer(text):
        args, _ = _balanced(text, m.end())
        segs = _split_top(args)
        if not segs:
            continue
        target = segs[0].strip()
        paths = []
        lit = _literal(target)
        if lit is not None:
            paths = [lit]
        elif target in loops:
            paths = _iterable_strings(text, loops[target])
        elif target in consts:
            paths = [consts[target]]
        line0 = text.count("\n", 0, m.start())
        for p in paths:
            info["mounts"].append((line0, join_path(p), m.group(1)))

    for m in PY_MCP_TOOL.finditer(text):
        name = None
        end = m.end()
        if m.group(2) == "(":
            tool_args, end = _balanced(text, end)
            name = _kw_string(tool_args, "name")
        fn = PY_DEF.search(text, end)
        tool = name or (fn.group(1) if fn else "")
        if not tool:
            continue
        line0 = text.count("\n", 0, m.start())
        info["tools"].append((line0, tool, fn.group(1) if fn else ""))

    if not info["endpoints"] and not info["includes"] and not info["routers"] \
            and not info["mounts"] and not info["tools"]:
        return None  # 既没端点也没路由定义，与 FastAPI 无关
    return info


class PyIndexer:
    """FastAPI 的路由前缀可能要跨文件拼接，所以先收集单文件信息，再统一构图。"""

    def __init__(self, root):
        self.root = root
        self.by_path = {}  # 绝对路径 -> FileInfo
        self.empty = {}    # 绝对路径 -> sig：已确认与 FastAPI 无关的文件，避免每次重读

    def _module_file(self, module):
        if not module:
            return None
        exact = [f for f in self.by_path.values() if f["module"] == module]
        if exact:
            return exact[0]
        suffix = "." + module
        tail = [f for f in self.by_path.values() if f["module"].endswith(suffix)]
        if tail:
            return min(tail, key=lambda f: len(f["module"]))
        return None

    def _resolve_module(self, info, module, level):
        if not level:
            return module
        pkg = list(info["module_parts"] if info["is_pkg"] else info["module_parts"][:-1])
        up = level - 1
        if up:
            pkg = pkg[:-up] if up <= len(pkg) else []
        if module:
            pkg += module.split(".")
        return ".".join(pkg)

    def _resolve_ref(self, info, expr):
        """把 `router`、`users.router`、`module.router` 解析成 (文件绝对路径, 变量名)。"""
        parts = [p for p in (s.strip() for s in expr.split(".")) if p]
        if not parts:
            return None
        head, rest = parts[0], parts[1:]

        if head in info["routers"] and not rest:
            return (info["path"], head)

        imp = info["imports"].get(head)
        if not imp:
            return None
        module, name, level = imp
        base = self._resolve_module(info, module, level)

        if name:  # from X import name
            child = self._module_file((base + "." + name) if base else name)
            if child is not None:  # name 本身是模块
                return (child["path"], rest[-1]) if rest else None
            owner = self._module_file(base)
            return (owner["path"], name) if owner is not None and not rest else None

        if not rest:  # import X，后面没属性，取不到 router
            return None
        owner = self._module_file(base)
        return (owner["path"], rest[-1]) if owner is not None else None

    def symbols(self):
        routers, edges = {}, {}
        for info in self.by_path.values():
            for var, prefix in info["routers"].items():
                routers[(info["path"], var)] = prefix
            for parent, child_expr, inc_prefix in info["includes"]:
                child = self._resolve_ref(info, child_expr)
                if child:
                    edges.setdefault(child, []).append(((info["path"], parent), inc_prefix))

        memo = {}

        def prefixes(node, stack=()):
            if node in memo:
                return memo[node]
            own = routers.get(node, "")
            parents = edges.get(node)
            if not parents or node in stack:
                memo[node] = [own]
                return memo[node]
            out = []
            for parent, inc in parents:
                for base in prefixes(parent, stack + (node,)):
                    out.append(join_path(base, inc, own))
            memo[node] = _dedupe(out)[:16] or [own]
            return memo[node]

        symbols = []
        for info in self.by_path.values():
            uri = "file://" + quote(info["path"])
            for line0, methods, paths, fn, receiver in info["endpoints"]:
                node = (info["path"], receiver)
                pres = prefixes(node) if node in routers else [""]
                container = f'{info["rel"]}:{fn}' if fn else info["rel"]
                for http in methods:
                    for pre in pres:
                        for p in paths or [""]:
                            symbols.append((f"{http} {join_path(pre, p)}", container, uri, line0))
            for line0, path, owner in info["mounts"]:
                symbols.append((f"MOUNT {path}", f'{info["rel"]}:{owner}.mount', uri, line0))
            for line0, tool, fn in info["tools"]:
                symbols.append((f"TOOL {tool}", f'{info["rel"]}:{fn}', uri, line0))
        return _dedupe(symbols)

    def update(self, files):
        alive = set()
        for path in files:
            alive.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig = (st.st_mtime, st.st_size)
            hit = self.by_path.get(path)
            if hit and hit["sig"] == sig:
                continue
            if self.empty.get(path) == sig:
                continue
            info = index_py_file(path, self.root)
            if info is None:
                self.by_path.pop(path, None)
                self.empty[path] = sig
            else:
                self.empty.pop(path, None)
                info["sig"] = sig
                self.by_path[path] = info
        for path in list(self.by_path):
            if path not in alive:
                del self.by_path[path]
        for path in list(self.empty):
            if path not in alive:
                del self.empty[path]
        return self.symbols()


# ---------------------------------------------------------------- 索引

class Index:
    """目录只 walk 一次，之后每次请求只 stat 已知文件做增量。"""

    def __init__(self, lang="both"):
        self.lang = lang
        self.root = None
        self.files = []      # 绝对路径列表
        self.cache = {}      # path -> (mtime, size, symbols)  仅 Java
        self.symbols = []    # 扁平化结果
        self._java_symbols = []
        self.py = None

    def set_lang(self, lang):
        self.lang = lang

    def set_root(self, root):
        if root and os.path.isdir(root):
            self.root = os.path.abspath(root)
            self.py = PyIndexer(self.root)

    def _want(self, fn):
        java = self.lang in ("java", "both") and fn.endswith("Controller.java")
        py = self.lang in ("python", "both") and fn.endswith(".py")
        return java or py

    def scan_files(self):
        found = []
        if not self.root:
            return found
        skip = PY_SKIP_DIRS if self.lang in ("python", "both") else SKIP_DIRS
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
            for fn in filenames:
                if self._want(fn):
                    found.append(os.path.join(dirpath, fn))
        return found

    def refresh(self, force=False):
        if force or not self.files:
            self.files = self.scan_files()
        java_syms = self._refresh_java() if self.lang in ("java", "both") else []
        py_syms = self.py.update(self.files) if (self.lang in ("python", "both") and self.py) else []
        if self.lang == "both":
            self.symbols = sorted(java_syms + py_syms, key=lambda s: (s[2], s[3]))
        elif self.lang == "python":
            self.symbols = py_syms
        else:
            self.symbols = java_syms
        return self.symbols

    def _refresh_java(self):
        changed = False
        alive = set()
        for path in self.files:
            if not path.endswith("Controller.java"):
                continue
            alive.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig = (st.st_mtime, st.st_size)
            hit = self.cache.get(path)
            if hit and hit[0] == sig:
                continue
            self.cache[path] = (sig[0], sig[1], index_java_file(path))
            changed = True
        for path in list(self.cache):
            if path not in alive:
                del self.cache[path]
                changed = True
        if not changed:
            return self._java_symbols
        flat = []
        for path in sorted(self.cache):
            flat.extend(self.cache[path][2])
        self._java_symbols = flat
        return flat

    def search(self, query):
        self.refresh()
        q = (query or "").strip().lower()
        if not q:
            return self.symbols
        # 空格分词，全部命中才算（支持 "get user" 这类组合搜）
        terms = q.split()
        return [
            s for s in self.symbols
            if all(t in (s[0] + " " + s[1]).lower() for t in terms)
        ]


INDEX = Index()


# ---------------------------------------------------------------- LSP 协议

def read_message(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        k, _, v = line.decode("ascii", "replace").partition(":")
        headers[k.strip().lower()] = v.strip()
    try:
        length = int(headers.get("content-length", 0))
    except ValueError:
        return None
    if length <= 0:
        return None
    return json.loads(stream.read(length))


def send(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    out.write(b"Content-Length: %d\r\n\r\n" % len(body))
    out.write(body)
    out.flush()


def uri_to_path(uri):
    if not uri:
        return None
    return unquote(urlparse(uri).path)


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        root = uri_to_path(params.get("rootUri"))
        if not root:
            folders = params.get("workspaceFolders") or []
            if folders:
                root = uri_to_path(folders[0].get("uri"))
        INDEX.set_root(root)
        INDEX.refresh(force=True)  # 启动即扫一遍，后续增量
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "capabilities": {
                "workspaceSymbolProvider": True,
                "textDocumentSync": 0,  # 不要文档同步，省开销
            },
            "serverInfo": {"name": f"endpoint-nav-{INDEX.lang}", "version": "0.3.0"},
        }})
        return

    if method == "workspace/symbol":
        items = []
        for name, container, uri, line0 in INDEX.search(params.get("query", "")):
            items.append({
                "name": name,
                "kind": 6,  # SymbolKind.Method
                "containerName": container,
                "location": {
                    "uri": uri,
                    "range": {
                        "start": {"line": line0, "character": 0},
                        "end": {"line": line0, "character": 0},
                    },
                },
            })
        send({"jsonrpc": "2.0", "id": mid, "result": items})
        return

    if method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
        return

    if method == "exit":
        sys.exit(0)

    # 其他请求（我们没声明能力，理论上不会来）一律空响应，避免客户端卡住
    if mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": None})


def main():
    argv = sys.argv[1:]
    lang = "both"
    if "--lang" in argv:
        i = argv.index("--lang")
        if i + 1 < len(argv):
            lang = argv[i + 1]
    INDEX.set_lang(lang)

    if "--dump" in argv:
        i = argv.index("--dump")
        root = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else "."
        INDEX.set_root(root)
        syms = INDEX.refresh(force=True)
        for name, container, uri, line0 in syms:
            print(f"{name:<45} {container:<40} {unquote(uri)[7:]}:{line0 + 1}")
        print(f"\n共 {len(syms)} 个端点，{len(INDEX.files)} 个候选文件 (lang={lang})", file=sys.stderr)
        return

    while True:
        try:
            msg = read_message(sys.stdin.buffer)
        except Exception as e:  # 协议层坏了就退出，别把 Zed 卡住
            print(f"read error: {e}", file=sys.stderr)
            break
        if msg is None:
            break
        try:
            handle(msg)
        except Exception as e:
            print(f"handle error on {msg.get('method')}: {e}", file=sys.stderr)
            if msg.get("id") is not None:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": None})


if __name__ == "__main__":
    main()
