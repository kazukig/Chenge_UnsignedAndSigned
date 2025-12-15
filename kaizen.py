import os
import re
import sys
import subprocess
import tempfile
from clang import cindex

def _get_token_text(node):
    """ノードのトークン文字列を結合して返す（なければ空文字）。"""
    try:
        return "".join([t.spelling for t in node.get_tokens()]).strip()
    except Exception:
        return ""

def get_expr_type(node):
    """
    node の式から型文字列を取り出す。直接型が空の場合は子ノードを辿る。
    INTEGER_LITERAL の場合はトークンのサフィックスを見て unsigned を判定する。
    """
    try:
        if node is None:
            return ""
        if node.kind == cindex.CursorKind.INTEGER_LITERAL:
            tok = _get_token_text(node)
            if not tok:
                return "int"
            if "u" in tok.lower():
                return "unsigned int"
            return "int"
        t = getattr(node, "type", None)
        if t:
            s = t.spelling
            if s:
                return s
    except Exception:
        pass
    for c in node.get_children():
        tt = get_expr_type(c)
        if tt:
            return tt
    return ""

def get_expr_name(node):
    """
    node から変数名（識別子）を取り出す。メンバアクセスやリテラルはトークンで返す。
    """
    try:
        if node is None:
            return ""
        if node.kind == cindex.CursorKind.INTEGER_LITERAL:
            tok = _get_token_text(node)
            if tok:
                return tok
        s = getattr(node, "spelling", "")
        if s:
            return s
        tok = _get_token_text(node)
        if tok:
            # トークンを整形してメンバアクセス等をそのまま返す
            return tok
    except Exception:
        pass
    for c in node.get_children():
        n = get_expr_name(c)
        if n:
            return n
    return ""

def _is_int_or_unsigned_int(type_str: str) -> bool:
    if not type_str:
        return False
    s = type_str.replace("const", "").replace("volatile", "").strip()
    return s in ("int", "unsigned int", "unsigned", "int32_t", "uint32_t")

def func_walk(node, func_name="<unknown>"):
    """
    node 以下を再帰走査して BinaryOperator を行ごとに集約して返す。
    返り値: { line_number: [entry, ...], ... }
    entry: {func,file,line,A_type,B_type,A_name,B_name,op_text}
    """
    results = {}
    if node is None:
        return results
    for child in node.get_children():
        if child.kind == cindex.CursorKind.BINARY_OPERATOR:
            operands = list(child.get_children())
            if len(operands) >= 2:
                lhs, rhs = operands[0], operands[1]
                lhs_type = get_expr_type(lhs).strip()
                rhs_type = get_expr_type(rhs).strip()
                lhs_name = get_expr_name(lhs)
                rhs_name = get_expr_name(rhs)
                op_text = _get_token_text(child)
                loc = child.location
                line = loc.line if loc and loc.line else 0
                file_path = str(loc.file) if loc and loc.file else ""
                entry = {
                    "func": func_name,
                    "file": file_path,
                    "line": line,
                    "A_type": lhs_type,
                    "B_type": rhs_type,
                    "A_name": lhs_name,
                    "B_name": rhs_name,
                    "op_text": op_text
                }
                results.setdefault(line, []).append(entry)
        # 再帰して行ごとに統合
        child_map = func_walk(child, func_name)
        for ln, lst in child_map.items():
            results.setdefault(ln, []).extend(lst)
    return results

def func_defprm(node):
    prmdeflist = []
    for child in node.get_children():
        if child.kind == cindex.CursorKind.PARM_DECL and child.is_definition():
            prmdeflist.append({"prm_name": child.spelling, "prm_type": child.type.spelling})
    return prmdeflist

def func_calcprm(node):
    return 0

def _preprocess_file(src_path, extra_args):
    """
    clang -E でプリプロセスして一時ファイルを返す。
    extra_args: list of additional args (e.g. ['-Iinclude','-D...'])
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(src_path)[1])
    tmp.close()
    cmd = ['clang', '-E', src_path, '-o', tmp.name] + (extra_args or [])
    subprocess.check_call(cmd)
    return tmp.name

def _build_preprocessed_line_map(pre_path):
    """
    プリプロセス済みファイルの # <lineno> "filename" を解析して
    preprocessed 行 -> (orig_file, orig_line) のマップを返す。
    """
    mapping = {}
    last_directive_pre = None
    last_directive_orig = 0
    last_directive_file = pre_path
    try:
        with open(pre_path, 'r', errors='ignore') as f:
            for pre_ln, line in enumerate(f, 1):
                s = line.lstrip()
                m = re.match(r'#\s*(\d+)\s+"([^"]+)"', s)
                if m:
                    last_directive_pre = pre_ln
                    last_directive_orig = int(m.group(1))
                    last_directive_file = m.group(2)
                    mapping[pre_ln] = (pre_path, pre_ln)
                else:
                    if last_directive_pre is not None and pre_ln > last_directive_pre:
                        orig_ln = last_directive_orig + (pre_ln - last_directive_pre - 1)
                        mapping[pre_ln] = (last_directive_file, orig_ln)
                    else:
                        mapping[pre_ln] = (pre_path, pre_ln)
    except Exception:
        return {}
    return mapping

def _locate_libclang():
    env_path = os.environ.get("LIBCLANG_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        "/opt/homebrew/opt/llvm/lib/libclang.dylib",
        "/usr/local/opt/llvm/lib/libclang.dylib",
        "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
        "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
        "/usr/lib/libclang.dylib",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    try:
        out = subprocess.check_output(["clang", "--print-file-name=libclang.dylib"], text=True).strip()
        if out and os.path.isfile(out):
            return out
    except Exception:
        pass
    return None

# libclang を見つけて設定
_libclang_path = _locate_libclang()
if _libclang_path:
    try:
        cindex.Config.set_library_file(_libclang_path)
    except Exception:
        pass
else:
    sys.stderr.write("libclang not found. Set LIBCLANG_PATH or install llvm (Homebrew).\n")
    raise RuntimeError("libclang not found")

index = cindex.Index.create()

# パース対象ファイル（元ソース）
src_file = "example.c"
compile_args = ["-std=c11", "-Iinclude"]

preprocessed = None
preproc_map = {}
try:
    # プリプロセスしてマクロ展開済みソースで型評価を行う
    preprocessed = _preprocess_file(src_file, compile_args)
    preproc_map = _build_preprocessed_line_map(preprocessed)

    tu = index.parse(preprocessed, args=[], options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

    # トップレベル関数を走査。ただし「関数定義が元の src_file にあるもの」のみ処理する
    for cursor in tu.cursor.get_children():
        if cursor.kind == cindex.CursorKind.FUNCTION_DECL and cursor.is_definition():
            start = cursor.extent.start
            # start.file/line はプリプロセス後ファイルを指す -> マップして元ファイルを得る
            mapped = preproc_map.get(start.line) if preproc_map else None
            orig_file = mapped[0] if mapped else None
            if not orig_file:
                continue
            # 正規化して比較（同一ファイルのみ処理）
            try:
                if os.path.abspath(orig_file) != os.path.abspath(src_file):
                    # 元ソース以外で定義される関数はスキップ
                    continue
            except Exception:
                continue

            # 関数は対象: func_walk で行ごとに集める
            line_map = func_walk(cursor, cursor.spelling)

            # 出力: 各 entry のファイル/行を元ソースに戻して表示
            for line in sorted(line_map.keys()):
                for m in line_map[line]:
                    file_path = m.get('file', '')
                    line_no = m.get('line', 0)
                    orig_file2, orig_line2 = file_path, line_no
                    try:
                        if preprocessed and os.path.abspath(file_path) == os.path.abspath(preprocessed):
                            mapped2 = preproc_map.get(line_no)
                            if mapped2:
                                orig_file2, orig_line2 = mapped2
                    except Exception:
                        pass
                    # 表示対象は元ソースファイルの行のみ（保証: 関数自体は元ソースだが念のためチェック）
                    try:
                        if os.path.abspath(orig_file2) != os.path.abspath(src_file):
                            continue
                    except Exception:
                        continue
                    print(f"[{m['func']}] {orig_file2}:{orig_line2} - op=\"{m.get('op_text','')}\" A_type={m['A_type']} (A_name={m['A_name']}), B_type={m['B_type']} (B_name={m['B_name']})")

finally:
    if preprocessed and os.path.exists(preprocessed):
        try:
            os.unlink(preprocessed)
        except Exception:
            pass

# for cursor in tu.cursor.get_children():
#    # print(cursor.kind)
#     if cursor.kind == cindex.CursorKind.FUNCTION_DECL and cursor.is_definition():
#         start = cursor.extent.start
#         end = cursor.extent.end
#         print(f"Function: {cursor.spelling}  開始行: {start.line}, 終了行: {end.line}")

#         # 関数内部を走査して行ごとのオペランド情報を取得
#         line_map = func_walk(cursor, cursor.spelling)

#         # 行順で表示（その行に複数マッチがあればそれぞれ表示）
#         for line in sorted(line_map.keys()):
#             for m in line_map[line]:
#                 print(f"[{m['func']}] {m['file']}:{m['line']} - op=\"{m['op_text']}\" A_type={m['A_type']} (A_name={m['A_name']}), B_type={m['B_type']} (B_name={m['B
