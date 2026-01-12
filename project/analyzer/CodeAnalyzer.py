import os
import re
import sys
import subprocess
import tempfile
from clang import cindex

class CodeAnalyzer:
    # compile:      指定された行の式を分析用のjsonデータにコンパイルする
    # decompile:    分析用のjsonデータを式に逆コンパイルする
    def __init__(self, src_file=None, compile_args=None, check_list=None):
        # check_list は 1行のみ(int)を想定（None の場合は全行）
        try:
            self.check_list = int(check_list) if check_list is not None else None
        except Exception:
            self.check_list = None

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
        try:
            # プリプロセスとマッピング構築
            self.preprocessed = self._preprocess_file(self.src_file, self.compile_args)
            self.preproc_map = self._build_preprocessed_line_map(self.preprocessed)

            self.tu = self.index.parse(self.preprocessed, args=[], options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        except Exception:
            sys.stderr.write("parse fault\n")
            raise RuntimeError("parse error")

    def getpreprocessed(self):
        return self.preprocessed

    def getpreprocmap(self):
        return self.preproc_map

    def getTu(self):
        return self.tu

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

            # 追加: 構造体/共用体のメンバ参照なら「参照先メンバ」の型を返す
            # 例: a.b / a->b なら b の型（intなど）
            if node.kind in (
                cindex.CursorKind.MEMBER_REF_EXPR,
                cindex.CursorKind.MEMBER_REF,
            ):
                try:
                    ref = getattr(node, "referenced", None)
                    if ref is not None:
                        rt = getattr(ref, "type", None)
                        if rt and rt.spelling:
                            return rt.spelling
                except Exception:
                    pass

            # 宣言への参照なら参照先の型を優先して取得する
            if node.kind == cindex.CursorKind.DECL_REF_EXPR:
                ref = getattr(node, "referenced", None)
                if ref is not None:
                    rt = getattr(ref, "type", None)
                    if rt:
                        s = rt.spelling
                        if s:
                            return s

            # キャスト系:
            # ここは「元の型」ではなく「キャスト後の型」を返すのが自然なので、
            # node.type を優先し、取れなければ子へフォールバックする。
            if node.kind in (
                cindex.CursorKind.IMPLICIT_CAST_EXPR,
                cindex.CursorKind.CSTYLE_CAST_EXPR,
                cindex.CursorKind.CXX_STATIC_CAST_EXPR,
                cindex.CursorKind.CXX_CONST_CAST_EXPR,
                cindex.CursorKind.CXX_REINTERPRET_CAST_EXPR,
            ):
                t = getattr(node, "type", None)
                if t and t.spelling:
                    return t.spelling
                for c in node.get_children():
                    tt = self.get_expr_type(c)
                    if tt:
                        return tt

            # UNEXPOSED は子を辿る
            if node.kind == cindex.CursorKind.UNEXPOSED_EXPR:
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

            # 整数リテラル
            if node.kind == cindex.CursorKind.INTEGER_LITERAL:
                tok = self._get_token_text(node)
                if tok:
                    return tok

            # 追加: 構造体メンバ参照は "." / "->" を省略しない（トークン列をそのまま返す）
            # 例: a.b / a->b を "b" ではなく "a.b" / "a->b" として返したい
            if node.kind in (
                cindex.CursorKind.MEMBER_REF_EXPR,
                cindex.CursorKind.MEMBER_REF,
            ):
                tok = self._get_token_text(node)
                if tok:
                    return tok

            # 通常: spelling 優先
            s = getattr(node, "spelling", "")
            if s:
                return s

            # フォールバック: トークン
            tok = self._get_token_text(node)
            if tok:
                return tok
        except Exception:
            pass

        # 子を再帰的に探索
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
    def func_walk(self, node):
        """
        node 配下を再帰的に走査し、BINARY_OPERATOR の Cursor を収集する。

        戻り値:
          { orig_line(int): [Cursor, ...], ... }

        追加仕様:
          関数呼び出しの引数内にある式も「別式」として収集する。
          例: a = func(c+d, e+f)
            - 行全体の式: a=func(c+d,e+f)  （BINARY_OPERATOR: '='）
            - 引数式1: c+d
            - 引数式2: e+f
          を同じ orig_line の child 要素として返す。
        """
        results = {}
        if node is None:
            return results

        allowed_line = self.check_list  # None ならフィルタしない

        # (helper) preprocessed line -> orig_line
        def _to_orig_line(cursor) -> int:
            try:
                loc = getattr(cursor, "location", None)
                pre_line = int(getattr(loc, "line", 0) or 0) if loc else 0
                if not pre_line:
                    return 0
                mapped = self.preproc_map.get(pre_line)
                return int(mapped[1]) if mapped and len(mapped) >= 2 else pre_line
            except Exception:
                return 0

        # (helper) subtree 内の BINARY_OPERATOR を列挙
        def _collect_binops_in_subtree(root_cursor):
            out = []

            def walk(n):
                try:
                    for ch in n.get_children():
                        if ch.kind == cindex.CursorKind.BINARY_OPERATOR:
                            out.append(ch)
                        walk(ch)
                except Exception:
                    return

            if root_cursor is not None:
                walk(root_cursor)
            return out

        # (helper) build側で root を取れるように、argごとに extent 最大のBINARY_OPERATORを返す
        def _collect_arg_expr_roots(call_expr):
            roots = []
            try:
                args = list(call_expr.get_arguments())
            except Exception:
                args = []

            def span_len(c):
                try:
                    ex = c.extent
                    return int(ex.end.offset) - int(ex.start.offset)
                except Exception:
                    return 0

            for a in args:
                binops = _collect_binops_in_subtree(a)
                if not binops:
                    continue
                try:
                    roots.append(max(binops, key=span_len))
                except Exception:
                    roots.append(binops[0])
            return roots

        for child in node.get_children():
            try:
                orig_line = _to_orig_line(child)
                if not orig_line:
                    raise Exception("no line")

                if allowed_line is not None and orig_line != allowed_line:
                    raise Exception("filtered")

                # (1) 行全体の式（BINARY_OPERATOR）は必ず追加（従来動作）
                if child.kind == cindex.CursorKind.BINARY_OPERATOR:
                    results.setdefault(orig_line, []).append(child)

                    # (2) 追加: 行全体の式の中に CALL_EXPR があれば、その引数式も「別式」として追加
                    def walk_calls(n):
                        try:
                            for ch in n.get_children():
                                if ch.kind == cindex.CursorKind.CALL_EXPR:
                                    # 引数ごとに root を追加（c+d, e+f の root になる BINARY_OPERATOR）
                                    for r in _collect_arg_expr_roots(ch):
                                        results.setdefault(orig_line, []).append(r)
                                walk_calls(ch)
                        except Exception:
                            return

                    walk_calls(child)
                    break

            except Exception:
                pass

            # 再帰（break しない：子側で別のBINARY_OPERATOR/CALL_EXPRを見つけるため）
            child_map = self.func_walk(child)
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

    def all_AST(self, tu):
        results_list = []
        if tu is None:
            return results_list

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

                line_map = self.func_walk(cursor)
                if line_map:
                    results_list = line_map
                    break
        return results_list

    # --- メイン実行 ---
    def compile(self):
        """
        解析を実行して JSON 風のリスト（辞書のリスト）を返す。
        失敗したら -1 を返す。
        返却される各辞書には元のソースの行番号を "line" として含む。
        """
        results_list = []
        try:
            x = self.all_AST(self.tu)

            # DEBUG (around line 375): x の cursor の line / spelling を表示
            try:
                # x は {orig_line: [Cursor, ...], ... } を想定
                for orig_ln, cursors in (x or {}).items():
                    for i, cur in enumerate(cursors or []):
                        try:
                            loc = getattr(cur, "location", None)
                            pre_line = int(getattr(loc, "line", 0) or 0) if loc else 0
                            spelling = getattr(cur, "spelling", "")
                            print(f"[DEBUG][compile:x] orig_line={orig_ln} pre_line={pre_line} idx={i} spelling={spelling!r}")
                        except Exception:
                            pass
            except Exception:
                pass
            
            trees = self.build_expr_trees_from_all_ast_item(x)

            return trees
        except Exception:
            return -1
        finally:
            if self.preprocessed and os.path.exists(self.preprocessed):
                try:
                    os.unlink(self.preprocessed)
                except Exception:
                    pass

    def build_expr_trees_from_all_ast_item(self, x_item):
        """
        all_AST() の結果 x の 1要素（例: x[0]={orig_line:[Cursor(BINARY_OPERATOR)...],...}）から、
        {operator, A, B, kakko} を持つ式木のリストを返す。

        仕様:
          - leaf は {"type","text"} のみ（kakko は持たせない）
          - node は {"operator","A","B","type","kakko"}（kakko はこの二項式が括弧で囲まれているか）
            例: (a + b) の '+' ノード -> kakko=True
        """
        if not x_item or not isinstance(x_item, dict):
            return []

        def _token_text(n):
            try:
                return "".join([t.spelling for t in n.get_tokens()]).strip()
            except Exception:
                return ""

        def is_wrapped_by_single_outer_parens(cursor) -> bool:
            """
            cursor.extent のトークン列が、単一の最外括弧 (...) で全体が包まれているか判定する。
            例: "(a+b)" -> True, "a+(b)" -> False, "((a))" -> True（外側は1組として判定）
            """
            try:
                if cursor is None:
                    return False
                tu = getattr(cursor, "translation_unit", None)
                ex = getattr(cursor, "extent", None)
                if tu is None or ex is None:
                    return False

                toks = [t.spelling for t in tu.get_tokens(extent=ex)]
                # デバッグしたいならこの print を残す/必要に応じてコメントアウト
                # print("toks=", toks)

                if len(toks) < 2 or toks[0] != "(" or toks[-1] != ")":
                    return False

                depth = 0
                for i, s in enumerate(toks):
                    if s == "(":
                        depth += 1
                    elif s == ")":
                        depth -= 1
                        # 外側の括弧が末尾より前で閉じたら NG
                        if depth == 0 and i != len(toks) - 1:
                            return False

                return depth == 0
            except Exception:
                return False

        def _has_outer_paren_expr(n) -> bool:
            try:
                return is_wrapped_by_single_outer_parens(n)
            except Exception:
                return False

        def _op_symbol_between(cur, lhs, rhs):
            fallback_txt = _token_text(cur)
            try:
                if cur is None or lhs is None or rhs is None:
                    raise ValueError("missing operands")

                l_end = int(lhs.extent.end.offset)
                r_start = int(rhs.extent.start.offset)
                if l_end >= r_start:
                    raise ValueError("invalid extent order")

                ops = []
                for t in cur.get_tokens():
                    try:
                        off = int(t.location.offset)
                    except Exception:
                        continue
                    if l_end <= off < r_start:
                        ops.append(t.spelling)

                if ops:
                    mid = "".join(ops).strip()
                    m = re.search(r'(>>|<<|==|!=|<=|>=|\|\||&&|\+|-|\*|/|%|=)', mid)
                    if m:
                        return m.group(1)

                m2 = re.findall(r'(>>|<<|==|!=|<=|>=|\|\||&&|\+|-|\*|/|%|=)', fallback_txt)
                return m2[-1] if m2 else ""
            except Exception:
                m2 = re.findall(r'(>>|<<|==|!=|<=|>=|\|\||&&|\+|-|\*|/|%|=)', fallback_txt)
                return m2[-1] if m2 else ""

        def _unwrap(n):
            try:
                if n is None:
                    return None
                while n is not None and n.kind in (cindex.CursorKind.PAREN_EXPR, cindex.CursorKind.UNEXPOSED_EXPR):
                    ch = list(n.get_children())
                    if len(ch) != 1:
                        break
                    n = ch[0]
                return n
            except Exception:
                return n

        def _is_simple_term_text(s: str) -> bool:
            if not s:
                return False
            s = s.strip()

            # "->" を含む場合は '-' があっても「メンバ参照」として扱い、ここでは False にしない
            # （ただし他の演算子や括弧などが含まれる場合は従来通り False）
            s2 = s.replace("->", "")

            # "." は元から許可（regexに含めない）
            if re.search(r'[\(\)\+\-\*/%=\[\],<>]', s2):
                return False
            return True

        def _leaf(n):
            n2 = _unwrap(n)
            t = ""
            s = ""
            try:
                t = (self.get_expr_type(n2) or "").strip()
            except Exception:
                t = ""
            try:
                s = (self.get_expr_name(n2) or "").strip()
            except Exception:
                s = ""
            if not s:
                s = _token_text(n2)
            if not t:
                t = "x"
            print("s = ", s)
            s = s.strip()
            if not _is_simple_term_text(s):
                tok = _token_text(n2).strip()
                if _is_simple_term_text(tok):
                    s = tok
                else:
                    s = (getattr(n2, "spelling", "") or tok or "x").strip()
                    if not _is_simple_term_text(s):
                        s = "x"
                        t = "x"
            print("s = ", s)
            # op: 変数=0 / マクロで定義した変数=1 / 関数=2 / 定数=3 / それ以外=4
            def _classify_op(cursor, text: str) -> int:
                try:
                    txt = (text or "").strip()
                    if not txt:
                        return "None"

                    # 定数
                    if cursor is not None and getattr(cursor, "kind", None) == cindex.CursorKind.INTEGER_LITERAL:
                        return "Constant"
                    if re.fullmatch(r'(0[xX][0-9A-Fa-f]+|0[0-7]*|[0-9]+)([uUlL]{0,3})', txt):
                        return "Constant"

                    # 関数（呼び出し）
                    if cursor is not None and getattr(cursor, "kind", None) == cindex.CursorKind.CALL_EXPR:
                        return "Function"
                    # 簡易フォールバック: foo(...)
                    if re.match(r'^[A-Za-z_]\w*\s*\(.*\)$', txt):
                        return "Function"

                    # マクロ（事前にマクロテーブルがある場合だけ 1）
                    macro_table = getattr(self, "_macro_table", None)
                    if isinstance(macro_table, dict) and txt in macro_table:
                        return "Macro"

                    # 変数（識別子のみ）
                    if re.fullmatch(r'[A-Za-z_]\w*', txt):
                        return "Variable"

                    return "Other"
                except Exception:
                    return "Other"

            op = _classify_op(n2, s)

            # 追加: op が Function の場合、type が "ret (args...)" の形式なら ret のみ残す
            # 例: "u8 (u8, i8)" -> "u8"
            if op == "Function":
                try:
                    # 先頭から最初の '(' までを返り値型として採用
                    i = t.find("(")
                    if i > 0:
                        t = t[:i].strip()
                except Exception:
                    pass

            # leaf は kakko を持たない（代わりに op/sub を持たせる）
            return {"type": t, "text": s, "op": op, "sub": ""}

        def _build(cur):
            if cur is None:
                return None

            # DEBUG (around line 513): いまチェックしている cur を出力
            #try:
            #    loc = getattr(cur, "location", None)
            #    file_name = getattr(getattr(loc, "file", None), "name", None) if loc else None
            #    line_no = getattr(loc, "line", None) if loc else None
            #    col_no = getattr(loc, "column", None) if loc else None
            #    kind = getattr(cur, "kind", None)
            #    txt = _token_text(cur)
            #    #print(f"[DEBUG][_build] cur.kind={kind} loc={file_name}:{line_no}:{col_no} text={txt!r}")
            #except Exception:
            #    pass

            # この二項式自体が (...) で包まれているか（unwrap 前に判定）
            node_kakko = _has_outer_paren_expr(cur)

            cur_u = _unwrap(cur)
            if cur_u is None:
                return None

            # 追加: (int)(a+b) のような明示キャストは、キャスト「中身」を build 対象にする
            # 例: CSTYLE_CAST_EXPR の子に BINARY_OPERATOR が居ればそれを辿る
            if cur_u.kind == cindex.CursorKind.CSTYLE_CAST_EXPR:
                try:
                    ch_cast = list(cur_u.get_children())
                    # 一般的には [TYPE_REF or TYPEDEF_DECL..., <expr>] のように並ぶので、
                    # 最後の「式」側の子を優先して辿る
                    expr_child = ch_cast[-1] if ch_cast else None
                    if expr_child is not None:
                        return _build(expr_child)
                except Exception:
                    pass
                # 子が取れない場合はフォールバックで leaf 化
                return _leaf(cur)

            if cur_u.kind != cindex.CursorKind.BINARY_OPERATOR:
                return _leaf(cur)

            ch = list(cur_u.get_children())
            lhs_raw = ch[0] if len(ch) >= 1 else None
            rhs_raw = ch[1] if len(ch) >= 2 else None

            lhs = _unwrap(lhs_raw)
            rhs = _unwrap(rhs_raw)

            # 追加: lhs/rhs がキャスト式なら、その中身を辿って build する（leaf に潰さない）
            if lhs is not None and lhs.kind == cindex.CursorKind.CSTYLE_CAST_EXPR:
                try:
                    lhs_cast_children = list(lhs.get_children())
                    lhs_expr = lhs_cast_children[-1] if lhs_cast_children else None
                    if lhs_expr is not None:
                        lhs_raw = lhs_expr
                        lhs = _unwrap(lhs_raw)
                except Exception:
                    pass

            if rhs is not None and rhs.kind == cindex.CursorKind.CSTYLE_CAST_EXPR:
                try:
                    rhs_cast_children = list(rhs.get_children())
                    rhs_expr = rhs_cast_children[-1] if rhs_cast_children else None
                    if rhs_expr is not None:
                        rhs_raw = rhs_expr
                        rhs = _unwrap(rhs_raw)
                except Exception:
                    pass

            print("lhs.kind=", lhs.kind)
            print("rhs.kind=", rhs.kind)

            A = _build(lhs_raw) if lhs is not None and lhs.kind == cindex.CursorKind.BINARY_OPERATOR else _leaf(lhs_raw)
            B = _build(rhs_raw) if rhs is not None and rhs.kind == cindex.CursorKind.BINARY_OPERATOR else _leaf(rhs_raw)

            print("A=", A)
            print("B=", B)
            op = _op_symbol_between(cur_u, lhs, rhs) or ""

            at = (A.get("type", "x") if isinstance(A, dict) else "x") or "x"
            bt = (B.get("type", "x") if isinstance(B, dict) else "x") or "x"
            node_type = at if (at != "x" and bt != "x" and at == bt) else "x"

            return {"operator": op, "A": A, "B": B, "type": node_type, "kakko": bool(node_kakko)}

        trees = []
        for _, cursors in x_item.items():
            if not cursors:
                continue
            for cursor in cursors:
                t = _build(cursor)
                if t:
                    trees.append(t)

        return trees