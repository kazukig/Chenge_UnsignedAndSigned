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

        返り値（変更後）:
          {
            orig_line(int): {
              "line": orig_line,
              "spelling": "<その行の文字列。抽出式部分は @ に置換>",
              "cursors": [Cursor, Cursor, ...]
            },
            ...
          }

        spelling 仕様:
          (1) 基本は「元ソースのその行」。
          (2) 抽出した式（ここで収集する cursor の token 範囲）に該当する部分を '@' に置き換える。
              - cursor から 'return' 等を探して作らない
              - あくまで「抽出した式の範囲」を行テキスト上で '@' 化する

        例:
          if(a + b)                -> "if(@)"
          switch(func(a+b,5+c))    -> "switch(func(@,@))"
          return (a + b)           -> "return @"
        """
        results = {}
        if node is None:
            return results

        allowed_line = self.check_list  # None ならフィルタしない

        # --- helpers ---
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

        def _get_orig_line_text(orig_ln: int) -> str:
            try:
                with open(self.src_file, "r", errors="ignore") as f:
                    lines = f.read().splitlines()
                if 1 <= orig_ln <= len(lines):
                    return lines[orig_ln - 1]
            except Exception:
                pass
            return ""

        def _cursor_to_line_span(cursor, orig_ln: int):
            """
            cursor の token を使い、orig_ln 行上での [start_col, end_col) を推定する。
            - column は 1-based を想定
            - end_col は「最後のトークン末尾の次」(exclusive)
            失敗したら None を返す。
            """
            try:
                if cursor is None:
                    return None
                toks = list(cursor.get_tokens())
                if not toks:
                    return None

                line_toks = []
                for t in toks:
                    try:
                        loc = getattr(t, "location", None)
                        tl = int(getattr(loc, "line", 0) or 0) if loc else 0
                        mapped = self.preproc_map.get(tl)
                        t_orig_ln = int(mapped[1]) if mapped and len(mapped) >= 2 else tl
                        if t_orig_ln == orig_ln:
                            line_toks.append(t)
                    except Exception:
                        continue

                if not line_toks:
                    return None

                first = line_toks[0]
                last = line_toks[-1]

                start_col = int(getattr(getattr(first, "location", None), "column", 0) or 0)
                if start_col <= 0:
                    return None

                last_col = int(getattr(getattr(last, "location", None), "column", 0) or 0)
                if last_col <= 0:
                    return None

                end_col = last_col + len(getattr(last, "spelling", "") or "")
                if end_col <= start_col:
                    return None

                return (start_col - 1, end_col)
            except Exception:
                return None

        def _make_spelling_by_replace(orig_ln: int, cursor_list):
            """
            orig_ln の行テキストに対し、cursor_list の範囲を '@' に置換して spelling を作る。
            """
            line_text = _get_orig_line_text(orig_ln)
            if not line_text:
                return ""

            spans = []
            for cur in cursor_list or []:
                sp = _cursor_to_line_span(cur, orig_ln)
                if sp is None:
                    continue
                s, e = sp
                s = max(0, min(s, len(line_text)))
                e = max(0, min(e, len(line_text)))
                if e > s:
                    spans.append((s, e))

            if not spans:
                return line_text.strip()

            spans.sort(key=lambda x: (x[0], x[1]))
            merged = []
            for s, e in spans:
                if not merged or s > merged[-1][1]:
                    merged.append([s, e])
                else:
                    merged[-1][1] = max(merged[-1][1], e)

            out = []
            pos = 0
            for s, e in merged:
                out.append(line_text[pos:s])
                out.append("@")
                pos = e
            out.append(line_text[pos:])

            return "".join(out).strip()

        def _canonicalize_spelling(orig_ln: int, cursor_list):
            """
            行頭のキーワードに応じて定型フォーマットへ正規化する。
            - if    -> "if( @ )"   (+ 行末に "{" があれば "if( @ ){" )
            - while -> "while( @ )" (+ 同上 )
            - for   -> "for( @; @; @;)" (+ 行末に "{" があれば "for( @; @; @;){" )
            - return-> "return @;"
            - case  -> "case @:"
            - break -> "break;"
            それ以外は replace 方式の結果を返す。

            追加仕様:
              行が構文キーワード以外に「@ 以外の実体」を含まない場合は "@;" を返す。
              例:
                "@"        -> "@;"
                "(@)"      -> "@;"
                "@ + @;"   -> "@;"
              ただし if/while/for/return/case/break などの構文行はこの規則より優先。
            """
            raw = _get_orig_line_text(orig_ln).strip()
            if not raw:
                return ""

            # ブロック開始の "{" を末尾から検出（スペース類を挟んでもOK）
            has_lbrace = bool(re.search(r'\{\s*$', raw))

            # 行頭キーワード判定（case/break も追加）
            m = re.match(r'^\s*(if|for|while|return|case|break)\b', raw)
            if m:
                kw = m.group(1)
                if kw == "if":
                    return "if( @ ){" if has_lbrace else "if( @ )"
                if kw == "while":
                    return "while( @ ){" if has_lbrace else "while( @ )"
                if kw == "for":
                    # ユーザ指定どおり末尾 ';' を含める（for( @; @; @;)）
                    return "for( @; @; @;){" if has_lbrace else "for( @; @; @;)"
                if kw == "return":
                    return "return @;"
                if kw == "case":
                    return "case @:"
                if kw == "break":
                    return "break;"

            # 構文行以外は、まず置換方式で作る
            rep = _make_spelling_by_replace(orig_ln, cursor_list)

            # 追加仕様:
            # 置換後の文字列が「@ と区切り記号だけ」なら "@;" に落とす
            # - @ の個数や位置は問わない
            # - 構造的な識別子/数値/文字列/演算子が残っていないことを条件にする
            #   例: "if(@)" は if があるので上で処理済み
            #   例: "@ + @" は '+' があるので NG（=> "@;" にする）
            #   ユーザ要望は「構文以外に何もない」ので、演算子も「何もない」に含めない（=> 演算子があれば @;）
            if rep:
                # 許可: 空白, @, 括弧, セミコロン, コロン, カンマ, ブレース
                # これ以外が残る(英数字や演算子等)なら「何かある」
                leftover = re.sub(r'[\s@()\[\]{};:,]', '', rep)
                if leftover == "":
                    return "@;"

            return rep

        # --- main walk ---
        for child in node.get_children():
            try:
                orig_line = _to_orig_line(child)
                if not orig_line:
                    raise Exception("no line")

                if allowed_line is not None and orig_line != allowed_line:
                    raise Exception("filtered")

                if child.kind == cindex.CursorKind.BINARY_OPERATOR:
                    entry = results.setdefault(orig_line, {"line": orig_line, "spelling": "", "cursors": []})
                    entry["cursors"].append(child)

                    def walk_calls(n):
                        try:
                            for ch in n.get_children():
                                if ch.kind == cindex.CursorKind.CALL_EXPR:
                                    for r in _collect_arg_expr_roots(ch):
                                        entry2 = results.setdefault(orig_line, {"line": orig_line, "spelling": "", "cursors": []})
                                        entry2["cursors"].append(r)
                                walk_calls(ch)
                        except Exception:
                            return

                    walk_calls(child)

                    entry["spelling"] = _canonicalize_spelling(orig_line, entry.get("cursors") or [])
                    break

            except Exception:
                pass

            child_map = self.func_walk(child)
            for ln, obj in (child_map or {}).items():
                if not isinstance(obj, dict):
                    continue
                entry = results.setdefault(ln, {"line": ln, "spelling": "", "cursors": []})
                entry["cursors"].extend(obj.get("cursors") or [])
                entry["spelling"] = _canonicalize_spelling(ln, entry.get("cursors") or [])

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
            print(x)
            # DEBUG (around line 375): x の cursor の line / spelling を表示
            #try:
            #    # x は {orig_line: [Cursor, ...], ... } を想定
            #    for orig_ln, cursors in (x or {}).items():
            #        for i, cur in enumerate(cursors or []):
            #            try:
            #                loc = getattr(cur, "location", None)
            #                pre_line = int(getattr(loc, "line", 0) or 0) if loc else 0
            #                spelling = getattr(cur, "spelling", "")
            #                print(f"[DEBUG][compile:x] orig_line={orig_ln} pre_line={pre_line} idx={i} spelling={spelling!r}")
            #            except Exception:
            #                pass
            #except Exception:
            #    pass
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
        spel = ""
        print(x_item.items())  # items は呼び出す

        for orig_ln, obj in x_item.items():
            if not isinstance(obj, dict):
                continue
            spelling = obj.get("spelling", "")
            cursors = obj.get("cursors", [])
            print(spelling)
            spel = spelling

            for cursor in cursors or []:
                t = _build(cursor)
                if t:
                    trees.append(t)

        return {"spelling": spel, "trees": trees}