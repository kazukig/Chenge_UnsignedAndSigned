import os
import re
import sys
import subprocess
import tempfile
from clang import cindex

class CodeAnalyzer:
    def __init__(self, src_file=None, compile_args=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]
        self.preprocessed = None
        self.preproc_map = {}
        self.index = None
        # libclang を探して設定し、Index を作成する
        lib = self._locate_libclang()
        if lib:
            try:
                cindex.Config.set_library_file(lib)
            except Exception:
                pass
        else:
            sys.stderr.write("libclang not found. Set LIBCLANG_PATH or install llvm (Homebrew).\n")
            raise RuntimeError("libclang not found")
        self.index = cindex.Index.create()
        
    def _resolve_type(self, type_str: str) -> str:
        """
        typedef テーブルを使って与えられた型記述を再帰的に展開する。
        self._type_table の各要素は [alias, actual, rels, [file, [lines...]]] という形を想定。
        深さ制限を入れて無限ループを防ぐ。
        """
        if not type_str:
            return type_str

        # type_table を辞書化(alias -> actual)
        type_map = {}
        try:
            for entry in self._type_table:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    alias = str(entry[0])
                    actual = str(entry[1]) if entry[1] is not None else ""
                    type_map[alias] = actual
        except Exception:
            return type_str

        token_re = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')

        sys.setrecursionlimit(max(1000, sys.getrecursionlimit()))

        def resolve_token(tok: str, seen: set, depth: int) -> str:
            if depth > 50:
                return tok
            if tok in seen:
                return tok
            if tok in type_map and type_map[tok] and type_map[tok] != tok:
                seen.add(tok)
                base = type_map[tok]
                return token_re.sub(lambda m: resolve_token(m.group(1), seen.copy(), depth + 1), base)
            return tok

        try:
            resolved = token_re.sub(lambda m: resolve_token(m.group(1), set(), 0), type_str)
            return " ".join(resolved.split())
        except Exception:
            return type_str
            
    # --- トークン / 式 ヘルパー ---
    def _get_token_text(self, node):
        try:
            return "".join([t.spelling for t in node.get_tokens()]).strip()
        except Exception:
            return ""

    def get_expr_type(self, node):
        try:
            if node is None:
                return ""
            # 整数リテラル
            if node.kind == cindex.CursorKind.INTEGER_LITERAL:
                tok = self._get_token_text(node)
                if not tok:
                    return "int"
                if "u" in tok.lower():
                    return "unsigned int"
                return "int"

            # 宣言への参照なら参照先の型を優先して取得する
            if node.kind == cindex.CursorKind.DECL_REF_EXPR:
                ref = getattr(node, "referenced", None)
                if ref is not None:
                    rt = getattr(ref, "type", None)
                    if rt:
                        s = rt.spelling
                        if s:
                            return s

            # キャストや未露出ノードの場合は子ノードを辿って元の型を取得する
            if node.kind in (
                cindex.CursorKind.IMPLICIT_CAST_EXPR,
                cindex.CursorKind.CSTYLE_CAST_EXPR,
                cindex.CursorKind.CXX_STATIC_CAST_EXPR,
                cindex.CursorKind.CXX_CONST_CAST_EXPR,
                cindex.CursorKind.CXX_REINTERPRET_CAST_EXPR,
                cindex.CursorKind.UNEXPOSED_EXPR,
            ):
                for c in node.get_children():
                    tt = self.get_expr_type(c)
                    if tt:
                        return tt

            # フォールバック: node.type を使う
            t = getattr(node, "type", None)
            if t:
                s = t.spelling
                if s:
                    return s
        except Exception:
            pass

        # 最終フォールバック: 子を再帰的に探索
        for c in node.get_children():
            tt = self.get_expr_type(c)
            if tt:
                return tt
        return ""

    def get_expr_name(self, node):
        try:
            if node is None:
                return ""
            if node.kind == cindex.CursorKind.INTEGER_LITERAL:
                tok = self._get_token_text(node)
                if tok:
                    return tok
            s = getattr(node, "spelling", "")
            if s:
                return s
            tok = self._get_token_text(node)
            if tok:
                return tok
        except Exception:
            pass
        for c in node.get_children():
            n = self.get_expr_name(c)
            if n:
                return n
        return ""

    def _is_int_or_unsigned_int(self, type_str: str) -> bool:
        if not type_str:
            return False
        s = type_str.replace("const", "").replace("volatile", "").strip()
        return s in ("int", "unsigned int", "unsigned", "int32_t", "uint32_t")

    # --- AST 走査 ---
    def func_walk(self, node, func_name="<unknown>"):
        results = {}
        if node is None:
            return results

        # 追加: DeclRefExpr を深く探して参照先の型/名前を取得するユーティリティ
        def _get_declref_type(n):
            try:
                if n is None:
                    return ""
                if n.kind == cindex.CursorKind.DECL_REF_EXPR:
                    ref = getattr(n, "referenced", None)
                    if ref is not None:
                        rt = getattr(ref, "type", None)
                        if rt:
                            s = rt.spelling
                            if s:
                                return s
                for c in n.get_children():
                    t = _get_declref_type(c)
                    if t:
                        return t
            except Exception:
                pass
            return ""

        def _get_declref_name(n):
            try:
                if n is None:
                    return ""
                if n.kind == cindex.CursorKind.DECL_REF_EXPR:
                    ref = getattr(n, "referenced", None)
                    if ref is not None:
                        name = getattr(ref, "spelling", "")
                        if name:
                            return name
                for c in n.get_children():
                    nm = _get_declref_name(c)
                    if nm:
                        return nm
            except Exception:
                pass
            return ""

        for child in node.get_children():
            if child.kind == cindex.CursorKind.BINARY_OPERATOR:
                operands = list(child.get_children())
                if len(operands) >= 2:
                    lhs, rhs = operands[0], operands[1]
                    # まず DeclRefExpr を深く探して参照先型を取得（暗黙キャストによる型変化を避けるため）
                    lhs_type = _get_declref_type(lhs) or self.get_expr_type(lhs).strip()
                    rhs_type = _get_declref_type(rhs) or self.get_expr_type(rhs).strip()
                    lhs_name = _get_declref_name(lhs) or self.get_expr_name(lhs)
                    rhs_name = _get_declref_name(rhs) or self.get_expr_name(rhs)
                    op_text = self._get_token_text(child)
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
            # 再帰
            child_map = self.func_walk(child, func_name)
            for ln, lst in child_map.items():
                results.setdefault(ln, []).extend(lst)
        return results

    def func_calcprm(self, node):
        return 0

    # --- プリプロセス / マッピング ---
    def _preprocess_file(self, src_path, extra_args):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(src_path)[1])
        tmp.close()
        cmd = ['clang', '-E', src_path, '-o', tmp.name] + (extra_args or [])
        subprocess.check_call(cmd)
        return tmp.name

    def _build_preprocessed_line_map(self, pre_path):
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

    def _locate_libclang(self):
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

    # --- メイン実行 ---
    def run(self, change_line=None):
        """
        解析を実行して JSON 風のリスト（辞書のリスト）を返す。
        失敗したら -1 を返す。
        change_line が与えられた場合は解析後に変更処理（外部で呼ぶ）を行う。
        返却される各辞書には元のソースの行番号を "line" として含む。
        """
        results_list = []
        try:
            # プリプロセスとマッピング構築
            self.preprocessed = self._preprocess_file(self.src_file, self.compile_args)
            self.preproc_map = self._build_preprocessed_line_map(self.preprocessed)

            tu = self.index.parse(self.preprocessed, args=[], options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

            for cursor in tu.cursor.get_children():
                if cursor.kind == cindex.CursorKind.FUNCTION_DECL and cursor.is_definition():
                    start = cursor.extent.start
                    mapped = self.preproc_map.get(start.line) if self.preproc_map else None
                    orig_file = mapped[0] if mapped else None
                    if not orig_file:
                        continue
                    try:
                        if os.path.abspath(orig_file) != os.path.abspath(self.src_file):
                            continue
                    except Exception:
                        continue

                    line_map = self.func_walk(cursor, cursor.spelling)

                    for line in sorted(line_map.keys()):
                        for m in line_map[line]:
                            file_path = m.get('file', '')
                            line_no = m.get('line', 0)
                            orig_file2, orig_line2 = file_path, line_no
                            try:
                                if self.preprocessed and os.path.abspath(file_path) == os.path.abspath(self.preprocessed):
                                    mapped2 = self.preproc_map.get(line_no)
                                    if mapped2:
                                        orig_file2, orig_line2 = mapped2
                            except Exception:
                                pass
                            try:
                                if os.path.abspath(orig_file2) != os.path.abspath(self.src_file):
                                    continue
                            except Exception:
                                continue
                            # エントリを構築して results_list に追加（元の行番号を含む）
                            entry = {
                                "func": m.get('func', ''),
                                "file": orig_file2,
                                "line": orig_line2,
                                "A_type": m.get('A_type', ''),
                                "B_type": m.get('B_type', ''),
                                "A_name": m.get('A_name', ''),
                                "B_name": m.get('B_name', ''),
                                "op_text": m.get('op_text', '')
                            }
                            results_list.append(entry)
            # 解析後の変更（外部で呼ぶことを想定）
            if change_line is not None and results_list:
                try:
                    # 互換性のため一応呼んでみる（実際は新しいクラスを使うことを推奨）
                    self.self_change(results_list, change_line)
                except Exception:
                    pass
            return results_list
        except Exception:
            return -1
        finally:
            if self.preprocessed and os.path.exists(self.preprocessed):
                try:
                    os.unlink(self.preprocessed)
                except Exception:
                    pass
