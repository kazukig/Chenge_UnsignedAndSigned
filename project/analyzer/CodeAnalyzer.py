import os
import re
import sys
import subprocess
import tempfile
from clang import cindex

# 追加: デバッグON/OFF（要求: 7行めに DEBUG=1 を置いて if で print）
DEBUG = 1

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

                        # 変更: directive 行も「元ファイル/元行」に寄せる
                        mapping[pre_ln] = (last_directive_file, last_directive_orig)
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

    # ---------------------------------------------------------------------
    # CodeAnalyzer.md 仕様反映: all_AST / func_walk / makeLineMacroData / getTargetInfo
    # ---------------------------------------------------------------------

    # --- small utils ------------------------------------------------------

    _DELIMS = set([
        ' ', '\t', '\r', '\n',
        ')', '(', ';', ',', '+', '-', '*', '/', '%', '&', '|', '^', '!', '~',
        '<', '>', '=', '?', ':', '[', ']', '{', '}'
    ])

    def _read_src_line(self, path: str, line_no: int) -> str:
        try:
            with open(path, "r", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if i == line_no:
                        return line.rstrip("\n")
        except Exception:
            pass
        return ""

    def _get_real_location(self, cursor):
        """
        cursor.location はプリプロセス後ファイルを指すことがある。
        preproc_map を使って「実ソース側 (file,line)」に寄せる。
        """
        try:
            loc = cursor.location
            if not loc or not loc.file:
                return None, None, None
            file_path = str(loc.file)
            line_no = int(loc.line)
            col_no = int(loc.column)
            # プリプロセス一時ファイルなら preproc_map で実ソースへ
            if self.preprocessed and os.path.abspath(file_path) == os.path.abspath(self.preprocessed):
                mapped = self.preproc_map.get(line_no)
                if mapped:
                    # col はそのまま（厳密な列変換は困難なので tokenize で補正する方針）
                    file_path, line_no = mapped
            return file_path, line_no, col_no
        except Exception:
            return None, None, None

    def _safe_tokenize(self, cursor):
        """
        仕様A/B/D: tokenize 必須。失敗時は空。
        """
        try:
            return list(cursor.get_tokens())
        except Exception:
            return []

    def _token_cols(self, tok):
        """
        token の begin/end 列(1始まり)を返す。
        clang token は end が取れない場合があるので spelling 長で推定する。
        """
        try:
            b = tok.extent.start.column
        except Exception:
            b = None
        try:
            e = tok.extent.end.column
        except Exception:
            e = None
        if b is None:
            return None, None
        if e is None:
            # end が無い場合: 文字数推定（Cのtokenは概ねこれでOK）
            e = b + max(len(getattr(tok, "spelling", "") or ""), 1) - 1
        return int(b), int(e)

    def _cursor_kind_is_function_call_head(self, cursor) -> bool:
        try:
            return cursor.kind == cindex.CursorKind.CALL_EXPR
        except Exception:
            return False

    def _format_call_expr(self, call_cursor) -> str:
        """
        仕様: funcName(arg1, arg2, ...) 形式。
        引数は AST の引数ノード単位で spelling を使う（a+b は 1引数として保持）。
        """
        try:
            fname = call_cursor.spelling or ""
            args = []
            for ch in call_cursor.get_children():
                # CALL_EXPR の子: 関数参照 + 引数... なので、最初の子を除外するのが無難
                args.append(ch.spelling)
            # 先頭が関数名参照っぽい場合は除去（重複回避）
            if args and (args[0] == fname or args[0] == "" or args[0].endswith(fname)):
                args = args[1:]
            return f"{fname}(" + ", ".join([a for a in args if a is not None]) + ")"
        except Exception:
            return (call_cursor.spelling or "") + "(...)"

    def _extract_identifier_at(self, line: str, col_1based: int) -> str:
        """
        実ソース行の col を起点に識別子を切り出す（macro 判定用）。
        col が式先頭を指している想定。空白はスキップ。
        """
        if not line:
            return ""
        i = max(col_1based - 1, 0)
        n = len(line)
        while i < n and line[i] in [' ', '\t']:
            i += 1
        if i >= n:
            return ""
        # identifier: [A-Za-z_][A-Za-z0-9_]*
        if not (line[i].isalpha() or line[i] == "_"):
            return ""
        j = i + 1
        while j < n and (line[j].isalnum() or line[j] == "_"):
            j += 1
        return line[i:j]

    # --- macro table ------------------------------------------------------

    def _parse_macro_definition(self, cursor):
        """
        MacroTable 要素を作る（簡易）。
        kind: object=0 / function=1
        func_op: 関数マクロ引数名リスト（取れない場合は []）
        """
        try:
            name = cursor.spelling or ""
            toks = self._safe_tokenize(cursor)
            if not name:
                return None

            spellings = [t.spelling for t in toks if getattr(t, "spelling", None) is not None]
            # ざっくり: name の次が "(" なら関数マクロとみなす
            kind = 0
            func_op = []
            val = ""

            # "name" の位置
            try:
                i = spellings.index(name)
            except ValueError:
                i = 0

            if i + 1 < len(spellings) and spellings[i + 1] == "(":
                kind = 1
                # 引数抽出 (name ( a , b ) val...)
                k = i + 2
                cur = []
                depth = 1
                while k < len(spellings) and depth > 0:
                    s = spellings[k]
                    if s == "(":
                        depth += 1
                    elif s == ")":
                        depth -= 1
                        if depth == 0:
                            if cur:
                                func_op.append("".join(cur).strip())
                            break
                    elif depth == 1 and s == ",":
                        func_op.append("".join(cur).strip())
                        cur = []
                    else:
                        cur.append(s)
                    k += 1
                # 残りが val
                val = " ".join(spellings[k + 1:]).strip()
            else:
                # オブジェクトマクロ: name の後ろ全部が val
                val = " ".join(spellings[i + 1:]).strip()

            return {
                "name": name,
                "kind": kind,
                "val": val,
                "func_op": func_op if kind == 1 else [],
                "name_length": len(name),
            }
        except Exception:
            return None

    # --- makeLineMacroData (pre/post) ------------------------------------

    def makeLineMacroData(self, pre_line: str, post_line: str, macroTable: list):
        """
        仕様: postベースの展開領域 (post_col_start/end: 1始まり) を返す。
        失敗時は握って [] を返す。
        """
        results = []
        pre_col = 0
        post_col = 0

        # lookup
        macro_by_name = {m.get("name"): m for m in (macroTable or [])}

        try:
            while pre_col < len(pre_line) and post_col < len(post_line):
                if pre_line[pre_col] == post_line[post_col]:
                    pre_col += 1
                    post_col += 1
                    continue

                # 不一致: pre_line から macro 名抽出（区切りまで）
                start = pre_col
                while pre_col < len(pre_line) and pre_line[pre_col] not in self._DELIMS:
                    pre_col += 1
                target_macro = pre_line[start:pre_col]
                if not target_macro or target_macro not in macro_by_name:
                    # 仕様: try/catchで握って終了してよい
                    break

                m = macro_by_name[target_macro]
                r_data = {"macro_name": target_macro, "post_col_start": post_col + 1}

                # post_col_end 決定
                if int(m.get("kind", 0)) == 0:
                    # object macro: val と一致する最初の範囲を探す
                    val = m.get("val", "")
                    if val:
                        idx = post_line.find(val, post_col)
                        if idx >= 0:
                            end0 = idx + len(val) - 1
                            r_data["post_col_end"] = end0 + 1
                            post_col = end0
                        else:
                            # 見つからない場合は次の区切りまで
                            k = post_col
                            while k < len(post_line) and post_line[k] not in self._DELIMS:
                                k += 1
                            r_data["post_col_end"] = k
                            post_col = max(k - 1, post_col)
                    else:
                        r_data["post_col_end"] = post_col + 1
                else:
                    # function macro: 括弧バランスで範囲確定
                    k = post_col
                    # '(' を探す
                    while k < len(post_line) and post_line[k] != "(" and post_line[k] not in [';', ',', ')']:
                        k += 1
                    if k < len(post_line) and post_line[k] == "(":
                        depth = 0
                        while k < len(post_line):
                            if post_line[k] == "(":
                                depth += 1
                            elif post_line[k] == ")":
                                depth -= 1
                                if depth == 0:
                                    r_data["post_col_end"] = k + 1
                                    post_col = k
                                    break
                            k += 1
                        else:
                            r_data["post_col_end"] = min(len(post_line), post_col + 1)
                    else:
                        # '(' が無い: 次の区切りまで
                        k2 = post_col
                        while k2 < len(post_line) and post_line[k2] not in self._DELIMS:
                            k2 += 1
                        r_data["post_col_end"] = k2
                        post_col = max(k2 - 1, post_col)

                results.append(r_data)

                # [7] increment
                pre_col += 1
                post_col += 1
        except Exception:
            return []

        return results

    def makeLineMacroData_pre(self, pre_line: str, macroTable: list):
        """
        仕様: preベースのマクロ呼び出し領域 (pre_col_start/end: 1始まり) を返す。
        除外判定に使う。
        """
        results = []
        if not pre_line:
            return results
        names = [m.get("name") for m in (macroTable or []) if m.get("name")]
        if not names:
            return results

        # identifier を走査して macro name が出たら範囲を確定
        i = 0
        n = len(pre_line)
        while i < n:
            ch = pre_line[i]
            if ch.isalpha() or ch == "_":
                j = i + 1
                while j < n and (pre_line[j].isalnum() or pre_line[j] == "_"):
                    j += 1
                ident = pre_line[i:j]
                if ident in names:
                    # object macro: ident 範囲
                    pre_start = i + 1
                    pre_end = j
                    # function macro: ident 〜 対応する ) まで
                    m = next((x for x in macroTable if x.get("name") == ident), None)
                    if m and int(m.get("kind", 0)) == 1:
                        k = j
                        while k < n and pre_line[k] in [' ', '\t']:
                            k += 1
                        if k < n and pre_line[k] == "(":
                            depth = 0
                            while k < n:
                                if pre_line[k] == "(":
                                    depth += 1
                                elif pre_line[k] == ")":
                                    depth -= 1
                                    if depth == 0:
                                        pre_end = k + 1
                                        break
                                k += 1
                        else:
                            # '(' 無し: 次の区切りまで
                            k2 = j
                            while k2 < n and pre_line[k2] not in self._DELIMS:
                                k2 += 1
                            pre_end = k2
                    results.append({"macro_name": ident, "pre_col_start": pre_start, "pre_col_end": pre_end})
                i = j
            else:
                i += 1
        return results

    def _is_in_macro_region_pre(self, operator_col: int, macroLineData_pre: list) -> bool:
        for r in macroLineData_pre or []:
            try:
                if int(r["pre_col_start"]) <= int(operator_col) <= int(r["pre_col_end"]):
                    return True
            except Exception:
                continue
        return False

    # --- all_AST ----------------------------------------------------------

    def all_AST(self, analyzeInfo: dict) -> dict:
        """
        CodeAnalyzer.md: all_AST
        TU の子を走査して macroTable を構築し、対象行 child に対し func_walk を呼ぶ。
        """
        line = int(analyzeInfo.get("line", 0) or 0)
        pre_line = self._read_src_line(self.src_file, line)
        src_abs = os.path.abspath(self.src_file) if self.src_file else None

        self._dbg("all_AST enter", f"line={line}", f"src={self.src_file}", f"src_abs={src_abs}", f"pre_line={pre_line!r}")
        self._dbg("analyzeInfo", analyzeInfo)

        def _empty():
            out = {
                "analizeID": analyzeInfo.get("data", {}).get("analizeID"),
                "line": line,
                "spelling": pre_line,
                "chenge_spelling": pre_line,
                "eval_datas": [],
            }
            self._dbg("all_AST empty return", out)
            return out

        tu = getattr(self, "tu", None)
        self._dbg("tu is None?", tu is None)
        if tu is None:
            return _empty()

        # まず macroTable は TU直下から集める（量が多いのでここは従来のまま）
        macroTable = []
        try:
            for child in tu.cursor.get_children():
                try:
                    if child.kind == cindex.CursorKind.MACRO_DEFINITION:
                        m = self._parse_macro_definition(child)
                        if m:
                            macroTable.append(m)
                            if len(macroTable) <= 10:
                                self._dbg("macro add", m.get("name"), "kind", m.get("kind"))
                except Exception:
                    pass
        except Exception:
            pass

        # 重要: TU全体を preorder で走査して、file+line 一致の「行の中のノード」を拾う
        visited = 0
        matched = 0
        picked = None

        try:
            for node in tu.cursor.walk_preorder():
                visited += 1

                f, ln, col = self._get_real_location(node)
                if ln != line:
                    continue

                f_abs = os.path.abspath(f) if f else None
                if src_abs and f_abs and f_abs != src_abs:
                    continue

                # 同じ行に複数ノードがあるので、とりあえず「その行の Statement/Expr っぽいもの」を優先
                matched += 1
                if picked is None:
                    picked = node
                else:
                    try:
                        # より内側（深い）を優先するため、token が取れる/長い方を採用
                        a = len(self._safe_tokenize(picked))
                        b = len(self._safe_tokenize(node))
                        if b >= a:
                            picked = node
                    except Exception:
                        pass

                if matched <= 5:
                    self._dbg_cursor("line-matched node", node)
        except Exception as e:
            self._dbg("walk_preorder exception", e)

        self._dbg("walk_preorder done", f"visited={visited}", f"matched={matched}", f"picked={'yes' if picked else 'no'}")

        if picked is None:
            return _empty()

        # picked を起点に func_walk（ここで + の候補が拾えるはず）
        try:
            out = self.func_walk(picked, analyzeInfo, macroTable, pre_line=pre_line)
            self._dbg("func_walk returned", "eval_datas_len", len(out.get("eval_datas", [])))
            return out
        except Exception as e:
            self._dbg("func_walk exception", e)
            return _empty()

    # --- func_walk --------------------------------------------------------

    def _all_cols_in_src(self, line_str: str, token_spelling: str) -> list:
        """line_str 中の token_spelling 出現列(1-based)を昇順で返す。"""
        if not line_str or not token_spelling:
            return []
        cols = []
        start = 0
        while True:
            k = line_str.find(token_spelling, start)
            if k < 0:
                break
            cols.append(k + 1)
            # 重なり検出を避ける（例: '>>' を 1文字ずつずらして誤検出しない）
            start = k + max(len(token_spelling), 1)
        return cols

    def _extract_expr_around_operator(self, line_str: str, operator_col_1based: int) -> str:
        """
        実ソース行から operator_col を中心に `lhs op rhs` を切り出す（マクロ展開に依存しない）。

        仕様:
          - 対象は **2項演算子** 1個分（同一深度の次の2項演算子は含めない）
            例: a + b + c の 1個目 '+' → "a + b"
                a + b * c の '+'       → "a + b"
          - 括弧は一塊: a + (b + c) の '+' → "a + (b + c)"
          - 引数区切り ',' は境界（同一深度で ',' に当たったら止める）
            例: f(EFGHIJK + a, b) の '+' → "EFGHIJK + a"
          - 複数文字の2項演算子 (==, <=, >=, !=, &&, ||, <<, >>, +=, -=, *=, /=, %=, &=, |=, ^=, <<=, >>= など) に対応
          - 評価順/優先順位は厳密に追わない（境界切り出しのみ）
        """
        if not line_str or operator_col_1based <= 0:
            return ""

        s = line_str
        n = len(s)
        op_i = operator_col_1based - 1
        if op_i < 0 or op_i >= n:
            return ""

        # --- 演算子候補（長いもの優先）---
        OPS_3 = {"<<=", ">>="}
        OPS_2 = {
            "==", "!=", "<=", ">=",
            "&&", "||",
            "<<", ">>",
            "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
            "->",
        }
        OPS_1 = set(list("+-*/%&|^<>!=?:"))

        STOP_CHARS = set(list(";,"))
        ARG_SEP = ","
        OPEN_CHARS = set(list("([{"))

        def _peek_op_at(pos: int) -> str:
            """pos を先頭として演算子文字列（最大3文字）を返す。無ければ ''。"""
            if pos < 0 or pos >= n:
                return ""
            if pos + 3 <= n and s[pos:pos + 3] in OPS_3:
                return s[pos:pos + 3]
            if pos + 2 <= n and s[pos:pos + 2] in OPS_2:
                return s[pos:pos + 2]
            if s[pos] in OPS_1:
                return s[pos]
            return ""

        def _is_unary_pm(pos: int, op_str: str) -> bool:
            """
            '+' or '-' が単項っぽい場合 True（rhs 境界として扱わない）。
            厳密でなくていいので、直前非空白が演算子/区切り/開き括弧なら単項とみなす。
            """
            if op_str not in {"+", "-"}:
                return False
            k = pos - 1
            while k >= 0 and s[k] in " \t":
                k -= 1
            if k < 0:
                return True
            prev = s[k]
            if prev in STOP_CHARS or prev in OPEN_CHARS or prev == ARG_SEP:
                return True
            if _peek_op_at(k):
                return True
            if prev in "=!?:":  # ざっくり
                return True
            return False

        # --- operator本体(文字列長)を確定（開始列は operator_col_1based 前提）---
        op_str_here = _peek_op_at(op_i)
        if not op_str_here:
            # operator_col が 2文字目を指していた等のズレ救済
            if op_i - 1 >= 0:
                op_str_here = _peek_op_at(op_i - 1)
                if op_str_here and len(op_str_here) >= 2:
                    op_i = op_i - 1
        op_len = len(op_str_here) if op_str_here else 1

        # --- 左側開始探索（同一深度での直前演算子/区切りの直後）---
        i = op_i - 1
        while i >= 0 and s[i] in " \t":
            i -= 1

        depth_paren = depth_brack = depth_brace = 0
        left_start = 0

        while i >= 0:
            ch = s[i]

            # 左方向の深度更新（逆走査）
            if ch == ")":
                depth_paren += 1
            elif ch == "(":
                if depth_paren > 0:
                    depth_paren -= 1
                else:
                    # ★深度0の '(' は「呼び出し/キャスト/グルーピングの開始」。
                    # 今回の f(EFGHIJK + a, b) のようなケースで lhs を f( から切り離す。
                    left_start = i + 1
                    break
            elif ch == "]":
                depth_brack += 1
            elif ch == "[":
                if depth_brack > 0:
                    depth_brack -= 1
            elif ch == "}":
                depth_brace += 1
            elif ch == "{":
                if depth_brace > 0:
                    depth_brace -= 1

            if depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
                # ★引数区切り
                if ch == ARG_SEP:
                    left_start = i + 1
                    break

                # 文/代入境界
                if ch in ";=":
                    left_start = i + 1
                    break

                # 「この位置を末尾とする」演算子(2/3文字)も境界にする
                prev2 = s[i - 1:i + 1] if i - 1 >= 0 else ""
                prev3 = s[i - 2:i + 1] if i - 2 >= 0 else ""

                op_end = ""
                if prev3 in OPS_3:
                    op_end = prev3
                    op_begin = i - 2
                elif prev2 in OPS_2:
                    op_end = prev2
                    op_begin = i - 1
                else:
                    op1 = _peek_op_at(i)
                    if op1 and len(op1) == 1:
                        op_end = op1
                        op_begin = i
                    else:
                        op_begin = None

                if op_end:
                    if op_end in {"+", "-"} and _is_unary_pm(op_begin, op_end):
                        i -= 1
                        continue
                    left_start = op_begin + len(op_end)
                    break

            i -= 1

        while left_start < n and s[left_start] in " \t":
            left_start += 1

        # --- 右側終了探索（同一深度での次演算子/区切り/閉じ括弧直前）---
        j = op_i + op_len
        while j < n and s[j] in " \t":
            j += 1

        depth_paren = depth_brack = depth_brace = 0
        right_end = n  # exclusive

        while j < n:
            ch = s[j]

            # 右方向の深度更新（順走査）
            if ch == "(":
                depth_paren += 1
            elif ch == ")":
                if depth_paren > 0:
                    depth_paren -= 1
                else:
                    # 呼び出し引数の終端など（深度0で閉じ括弧に当たったらそこで止める）
                    right_end = j
                    break
            elif ch == "[":
                depth_brack += 1
            elif ch == "]":
                if depth_brack > 0:
                    depth_brack -= 1
                else:
                    right_end = j
                    break
            elif ch == "{":
                depth_brace += 1
            elif ch == "}":
                if depth_brace > 0:
                    depth_brace -= 1
                else:
                    right_end = j
                    break

            if depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
                # ★引数区切り
                if ch == ARG_SEP:
                    right_end = j
                    break

                # 文終端/区切り
                if ch in STOP_CHARS:
                    right_end = j
                    break

                # 次の2項演算子で止める
                op_next = _peek_op_at(j)
                if op_next:
                    if op_next in {"+", "-"} and _is_unary_pm(j, op_next):
                        j += 1
                        continue
                    right_end = j
                    break

            j += 1

        return s[left_start:right_end].strip()

    def func_walk(self, child, analyzeInfo: dict, macroTable: list, pre_line: str = "") -> dict:
        """
        CodeAnalyzer.md: func_walk
        - child を 1回走査し、BinaryOperator/CompoundAssignOperator の候補を収集
        - operator/exitcolnum で target を確定
        - getTargetInfo で eval_info を作り r.eval_datas に入れる
        """
        line = int(analyzeInfo.get("line", 0) or 0)
        op = analyzeInfo.get("data", {}).get("op", {}) or {}
        target_operator = op.get("operator", "")
        exitnum = int(op.get("operator_exitcolnum", 1) or 1)

        self._dbg("func_walk enter", f"line={line}", f"target_operator={target_operator!r}", f"exitnum={exitnum}")
        self._dbg_cursor("func_walk root child", child)

        r = {
            "analizeID": analyzeInfo.get("data", {}).get("analizeID"),
            "line": line,
            "spelling": pre_line,
            "chenge_spelling": pre_line,
            "eval_datas": [],
        }

        macroLineData_pre = self.makeLineMacroData_pre(pre_line, macroTable)
        self._dbg("macroLineData_pre", macroLineData_pre)

        # 追加: pre_line 上の target_operator の出現列（これが“本当の列”）
        src_op_cols = self._all_cols_in_src(pre_line, target_operator)
        self._dbg("src_op_cols", src_op_cols)

        candidates = []
        seen_same_col = {}  # token列が同じ候補が複数ある場合のインデックス付け

        def walk_once(cur):
            stack = [cur]
            visited = 0
            while stack:
                node = stack.pop()
                visited += 1

                try:
                    k = node.kind
                except Exception:
                    k = None

                is_bin = (k == cindex.CursorKind.BINARY_OPERATOR)
                is_cas = (k == cindex.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR)

                if (is_bin or is_cas) and target_operator:
                    try:
                        if (node.spelling or "") == target_operator:
                            self._dbg_cursor("candidate node", node)
                            self._dbg_tokens("candidate node", node, limit=60)

                            tok_col = self._get_operator_col_from_tokens(node, target_operator)
                            self._dbg("operator_col_from_tokens", tok_col)

                            if tok_col is not None:
                                in_macro = self._is_in_macro_region_pre(tok_col, macroLineData_pre)
                                self._dbg("in_macro_region?", in_macro)
                                if not in_macro:
                                    # ★ここが重要: token列が同じ候補を区別して、pre_line上のN個目の列を割り当てる
                                    idx = seen_same_col.get(tok_col, 0)
                                    seen_same_col[tok_col] = idx + 1
                                    src_col = src_op_cols[idx] if idx < len(src_op_cols) else None

                                    candidates.append({
                                        "child": node,
                                        "col": tok_col,          # 旧: 展開後列（参考）
                                        "src_col": src_col,      # 新: 実ソース(pre_line)列（これで選ぶ）
                                        "src_index": idx + 1,    # 1-based: 何個目の'+'
                                    })
                    except Exception:
                        pass

                try:
                    stack.extend(list(node.get_children()))
                except Exception:
                    pass

            self._dbg("walk_once done", f"visited={visited}", f"candidates={len(candidates)}")

        walk_once(child)

        # ★ソートは src_col 優先（None は末尾）
        candidates.sort(key=lambda x: (x.get("src_col") is None, x.get("src_col", 10**9)))
        self._dbg("candidates sorted src_cols", [(c.get("src_col"), c.get("src_index")) for c in candidates])

        # 追加: src_col が同じ候補は重複なので 1件に絞る（= pre_line 上の + 出現数と合わせる）
        deduped = []
        seen_cols = set()
        for c in candidates:
            sc = c.get("src_col")
            if sc is None:
                deduped.append(c)
                continue
            if sc in seen_cols:
                continue
            seen_cols.add(sc)
            deduped.append(c)

        candidates = deduped
        self._dbg("candidates deduped src_cols", [(c.get("src_col"), c.get("src_index")) for c in candidates])

        if exitnum <= 0 or exitnum > len(candidates):
            self._dbg("NO TARGET", f"exitnum={exitnum}", f"len(candidates)={len(candidates)}")
            return r

        # ★選択は “pre_line 上の + のn個目”
        target_data = candidates[exitnum - 1]
        self._dbg("target picked", {"src_col": target_data.get("src_col"), "src_index": target_data.get("src_index"), "tok_col": target_data.get("col")})

        eval_info = self.getTargetInfo(target_data, line, macroTable, pre_line=pre_line)
        self._dbg("getTargetInfo returned None?", eval_info is None)

        if eval_info:
            r["eval_datas"].append(eval_info)
            r["chenge_spelling"] = eval_info.get("_chenge_spelling_line", r["chenge_spelling"])
            # 追加: spelling も置換後にする
            r["spelling"] = eval_info.get("_spelling_line", r["spelling"])
            if "_chenge_spelling_line" in r["eval_datas"][0]:
                r["eval_datas"][0].pop("_chenge_spelling_line", None)
            if "_spelling_line" in r["eval_datas"][0]:
                r["eval_datas"][0].pop("_spelling_line", None)

        self._dbg("func_walk return", "eval_datas_len", len(r["eval_datas"]))
        return r

    def _get_operator_col_from_tokens(self, cursor, operator_spelling: str):
        toks = self._safe_tokenize(cursor)
        self._dbg("get_operator_col_from_tokens", f"operator={operator_spelling!r}", f"tokens={len(toks)}", f"cursor_sp={getattr(cursor,'spelling',None)!r}")
        for t in toks:
            try:
                if t.spelling == operator_spelling:
                    b, _ = self._token_cols(t)
                    return b
            except Exception:
                continue
        return None

    def _guess_operator_token(self, cursor, operator_col: int) -> str:
        # token 列から operator_col の token を拾う（保険）
        toks = self._safe_tokenize(cursor)
        for t in toks:
            b, _ = self._token_cols(t)
            if b == operator_col:
                return t.spelling
        return getattr(cursor, "spelling", "") or ""

    def _decide_kind(self, node, pre_line: str, col_1based: int, macroTable: list):
        """
        仕様: function → macro → val
        """
        # function
        if self._cursor_kind_is_function_call_head(node):
            return "function", self._format_call_expr(node)

        # macro: 実ソース行から識別子切り出し
        ident = self._extract_identifier_at(pre_line, col_1based)
        if ident:
            for m in macroTable or []:
                if m.get("name") == ident:
                    return "macro", [{"macro_name": m.get("name"), "macro_val": m.get("val")}]

        return "val", None

    def _restore_macros_in_eval(
        self,
        pre_line: str,
        left_col: int,
        right_col: int,
        operator_col: int,
        operator_spelling: str,
        macroTable: list,
        left_spelling: str,
        right_spelling: str,
    ):
        """
        仕様[3.3]: マクロが含まれる場合はマクロ名に戻す。
        実装は簡易: left_col/right_col 位置の先頭識別子が macro 名なら置換する。
        """
        macro_by = {m.get("name"): m for m in (macroTable or []) if m.get("name")}
        l_ident = self._extract_identifier_at(pre_line, left_col)
        r_ident = self._extract_identifier_at(pre_line, right_col)

        if l_ident in macro_by:
            left_spelling = l_ident
        if r_ident in macro_by:
            right_spelling = r_ident

        eval_spelling = f"{left_spelling} {operator_spelling} {right_spelling}".strip()
        return eval_spelling, left_spelling, right_spelling

    def _apply_at1_replace(self, line_str: str, eval_spelling: str, eval_col: int, left_col: int, right_end_col: int) -> str:
        """
        置換仕様（更新）:
        - 代入文 "lhs = rhs;" の場合は、rhs 全体ではなく
          「対象演算子の式範囲(left_col..right_end_col)」だけを @1 に置換する
        例:
          1つ目の '+'（EFGHIJK + compare...）→  s = @1 + (int)b;
          2つ目の '+'（compare... + (int)b）→ s = EFGHIJK + @1;
        """
        if not line_str:
            return line_str

        # 1) 代入文の rhs 内だけを部分置換（最優先）
        try:
            s = line_str.rstrip()
            semi = ""
            if s.endswith(";"):
                semi = ";"
                s = s[:-1]

            if "=" in s:
                lhs_raw, rhs_raw = s.split("=", 1)

                lhs_part = lhs_raw.rstrip()
                # rhs の「line_str上の begin列(1-based)」を求める
                # 例: "    s = XXX" の '=' は lhs_raw の末尾にあるので、rhs_raw の先頭までの空白分も加味
                rhs_leading_ws = len(rhs_raw) - len(rhs_raw.lstrip(" \t"))
                rhs_begin_col = len(lhs_raw) + 1 + rhs_leading_ws + 1  # '=' の次(1-based) + ws + 1-based補正
                rhs_part = rhs_raw.lstrip(" \t")

                # left_col/right_end_col（line_str全体の列）を rhs_part の index に変換
                a = left_col - rhs_begin_col
                b = right_end_col - rhs_begin_col

                # ガード（rhs_part の範囲内に収める）
                if a < 0:
                    a = 0
                if b < 0:
                    b = 0
                if a > len(rhs_part):
                    a = len(rhs_part)
                if b > len(rhs_part):
                    b = len(rhs_part)

                if a < b:
                    new_rhs = rhs_part[:a] + "@1" + rhs_part[b:]
                else:
                    # 範囲が壊れている場合は rhs 全体を @1（最終フォールバック）
                    new_rhs = "@1"

                return f"{lhs_part} = {new_rhs.strip()}{semi}"
        except Exception:
            pass

        # 2) 文字列一致で置換（代入文以外）
        try:
            idxs = []
            start = 0
            while True:
                k = line_str.find(eval_spelling, start)
                if k < 0:
                    break
                idxs.append(k)
                start = k + 1

            if idxs:
                target = idxs[0]
                for k in idxs:
                    span_start_col = k + 1
                    span_end_col = k + len(eval_spelling)
                    if span_start_col <= eval_col <= span_end_col:
                        target = k
                        break
                return line_str[:target] + "@1" + line_str[target + len(eval_spelling):]
        except Exception:
            pass

        # 3) フォールバック: left_col〜right_end_col を @1
        try:
            a = max(left_col - 1, 0)
            b = min(right_end_col, len(line_str))
            if 0 <= a < b:
                return line_str[:a] + "@1" + line_str[b:]
        except Exception:
            pass

        return line_str

    def _tokens_to_c_expr(self, toks) -> str:
        """token列をCの見た目に近い形へ整形する（最小）。"""
        sp = [t.spelling for t in (toks or []) if getattr(t, "spelling", None) is not None]
        if not sp:
            return ""
        out = []
        for s in sp:
            if not out:
                out.append(s)
                continue
            prev = out[-1]
            if s in [")", "]", ",", ";"]:
                out.append(s)
            elif prev in ["(", "[", ","]:
                out.append(s)
            else:
                out.append(" " + s)
        return "".join(out).strip()

    def _src_slice_by_cols(self, line_str: str, begin_col: int, end_col_exclusive: int) -> str:
        """
        実ソース行から列(1始まり)で部分文字列取得。
        end は exclusive（token の end列に合わせる）。
        """
        if not line_str:
            return ""
        a = max(begin_col - 1, 0)
        b = max(end_col_exclusive - 1, 0)
        return line_str[a:b]

    def _c_normalize_min(self, s: str) -> str:
        """
        実ソース断片を最小限に正規化する。
        要求:
          - 'EFGHIJK + (int)b' の形
          - ') b' の空白を落とす
          - '(int) b' も '(int)b' にする
        """
        if not s:
            return s
        # 連続空白を1つに
        s = re.sub(r"[ \t]+", " ", s)

        # '( int )' -> '(int)'
        s = re.sub(r"\(\s*", "(", s)
        s = re.sub(r"\s*\)", ")", s)

        # '(int) b' / ') b' -> '(int)b' / ')b'（識別子だけでなく数値/式の先頭も対象にする）
        # 例: ') b' ') 123' ') *p' などを最小限に詰める
        s = re.sub(r"\)\s+([A-Za-z_0-9\*])", r")\1", s)

        return s.strip()

    # ---------------------------------------------------------------------
    # Debug helpers（必須：_dbg が無いと AttributeError になる）
    # ※ 環境変数は使わない。ファイル先頭の DEBUG=1 のときだけ print。
    # ---------------------------------------------------------------------
    def _dbg(self, *args):
        if DEBUG == 1:
            print("[CodeAnalyzerDBG]", *args)

    def _dbg_cursor(self, label: str, cursor):
        if DEBUG != 1:
            return
        try:
            f, ln, col = self._get_real_location(cursor)
        except Exception:
            f, ln, col = None, None, None
        try:
            kind = cursor.kind
        except Exception:
            kind = None
        try:
            sp = cursor.spelling
        except Exception:
            sp = None
        self._dbg(f"{label}: kind={kind} spelling={sp!r} loc=({f},{ln},{col})")

    def _dbg_tokens(self, label: str, cursor, limit: int = 80):
        if DEBUG != 1:
            return
        toks = self._safe_tokenize(cursor)
        parts = []
        for t in toks[:limit]:
            try:
                b, e = self._token_cols(t)
                parts.append(f"{t.spelling}@{b}-{e}")
            except Exception:
                parts.append(f"{getattr(t,'spelling',None)}@?")
        self._dbg(f"{label}: tokens({len(toks)}): " + " ".join(parts) + ("" if len(toks) <= limit else " ..."))

    # 追加: pre_line（マクロ含む実ソース）上での token 位置検索
    def _find_token_col_in_src(self, line_str: str, token_spelling: str, near_col_1based=None):
        """実ソース行で token_spelling が現れる列(1始まり)を返す。near_col があれば最寄りを返す。"""
        if not line_str or not token_spelling:
            return None
        hits = []
        start = 0
        while True:
            k = line_str.find(token_spelling, start)
            if k < 0:
                break
            hits.append(k + 1)
            start = k + 1
        if not hits:
            return None
        if near_col_1based is None:
            return hits[0]
        near = int(near_col_1based)
        return min(hits, key=lambda c: abs(c - near))

    def _extract_lhs_rhs_from_extend(self, expr_extend: str, operator_spelling: str):
        """'LHS op RHS' を最初の operator_spelling で split。"""
        if not expr_extend or not operator_spelling or operator_spelling not in expr_extend:
            return "", ""
        lhs, rhs = expr_extend.split(operator_spelling, 1)
        return lhs.strip(), rhs.strip()

    def _lookup_object_macro_value(self, name: str, macroTable: list):
        """
        name が macroTable にあればその値を返す。
        ※要求: left_val_spelling と同名があれば macro 扱いにするため、
          見つかったが値不明の場合は "" を返す。
        """
        if not name or not macroTable:
            return None
        for m in macroTable:
            try:
                if m.get("name") == name:
                    v = m.get("val", None)
                    if v is None:
                        v = m.get("value", None) or m.get("expansion", None) or m.get("body", None) or m.get("spelling", None)
                    if v is None:
                        return ""
                    return str(v).strip()
            except Exception:
                continue
        return None

    # 置換: 「代入の rhs を丸ごと @1」にしていたのが誤り。
    # 要求どおり「rhs の中で target_expr だけを @1 に置換」する。
    def _replace_in_assignment_rhs(self, pre_line: str, target_expr: str, replacement: str) -> str:
        """
        pre_line 内の target_expr を replacement に置換する。

        - 代入文 "lhs = rhs;" の場合: rhs 内だけを置換（従来互換）
        - '=' が無い行でも置換できるようにする（例: '| (a + b)' -> '| (@1)'）
        - target_expr がそのまま見つからない場合: '(target_expr)' も試す
        """
        s = pre_line.rstrip("\n")

        # --- 非代入文: 行全体で柔軟に置換 ---
        if "=" not in s:
            if not target_expr:
                return s

            # 1) そのまま
            idx = s.find(target_expr)
            if idx >= 0:
                return s[:idx] + replacement + s[idx + len(target_expr):]

            # 2) 括弧付き
            par = f"({target_expr})"
            idx = s.find(par)
            if idx >= 0:
                return s[:idx] + replacement + s[idx + len(par):]

            # 3) 空白正規化なしでは見つからないケース向けに、
            #    target_expr の空白を潰した版で再探索（軽いフォールバック）
            try:
                s2 = re.sub(r"[ \t]+", "", s)
                t2 = re.sub(r"[ \t]+", "", target_expr)
                if t2:
                    k = s2.find(t2)
                    if k >= 0:
                        # 元文字列の対応位置復元は難しいので、ここは最後の手段として諦める
                        # （必要ならトークン単位置換へ）
                        return s
            except Exception:
                pass

            return s

        # --- 代入文: rhs 内だけを置換（従来ロジック）---
        semi = ""
        s_strip = s.rstrip()
        if s_strip.endswith(";"):
            semi = ";"
            s_strip = s_strip[:-1]

        lhs_raw, rhs_raw = s_strip.split("=", 1)

        indent = re.match(r"^\s*", lhs_raw).group(0)
        lhs_name = lhs_raw.strip()
        rhs_part = rhs_raw.strip()

        # target_expr が rhs に無ければ変更しない（ただし括弧付きも試す）
        idx = rhs_part.find(target_expr) if target_expr else -1
        if idx < 0 and target_expr:
            idx = rhs_part.find(f"({target_expr})")
            if idx >= 0:
                target_expr = f"({target_expr})"

        if idx < 0:
            # 置換できない場合は元の行（整形した形）を返す
            return f"{indent}{lhs_name} = {rhs_part}{semi}"

        new_rhs = rhs_part[:idx] + replacement + rhs_part[idx + len(target_expr):]
        new_rhs = re.sub(r"\s*\+\s*", " + ", new_rhs)
        new_rhs = re.sub(r"\s+", " ", new_rhs).strip()

        r_val = f"{indent}{lhs_name} = {new_rhs}{semi}"
        self._dbg("r_val:", r_val)
        return r_val

    def getTargetInfo(self, target_data: dict, line: int, macroTable: list, pre_line: str = ""):
        cursor = target_data.get("child")
        self._dbg("cursor:", getattr(cursor, "spelling", None))

        op_str = ""
        try:
            op_str = (cursor.spelling or "")
        except Exception:
            op_str = ""

        operator_col = target_data.get("src_col")
        if operator_col is None:
            operator_col_hint = int(target_data.get("col", 0) or 0)
            operator_col = self._find_token_col_in_src(pre_line, op_str, near_col_1based=operator_col_hint) or operator_col_hint

        children = list(cursor.get_children())
        if len(children) < 2:
            return None
        left_node = children[0]
        right_node = children[1]

        # ------------------------------------------------------------------
        # A) eval_spelling_extend（= 展開前 / 元ソース）
        # ------------------------------------------------------------------
        self._dbg("operator_col:", operator_col)
        raw_target_expr_pre = self._extract_expr_around_operator(pre_line, operator_col).strip()
        self._dbg("raw_target_expr(pre):", raw_target_expr_pre)

        eval_spelling_extend = self._c_normalize_min(raw_target_expr_pre)
        self._dbg("eval_spelling_extend(pre,norm):", eval_spelling_extend)

        # ------------------------------------------------------------------
        # B) eval_spelling（= 展開後）
        #    ポイント: op_col_tok を「展開後行文字列上の列」に再計算する
        # ------------------------------------------------------------------
        # 1) 展開後ファイル上の（プリプロセス後）同一行の文字列を取得する
        post_line = ""
        try:
            # cursor.location はプリプロセス後ファイルを指す想定
            loc = cursor.location
            post_file = str(loc.file) if loc and loc.file else None
            post_ln = int(loc.line) if loc else None
            if post_file and post_ln:
                post_line = self._read_src_line(post_file, post_ln)
        except Exception:
            post_line = ""

        # 2) token列で得た op の「列」（プリプロセス後ファイル上の列）
        op_col_tok = self._get_operator_col_from_tokens(cursor, op_str)

        # 3) op_col_tok を「展開後の演算子位置」として確定させる（= 再計算）
        #    - 基本は post_line に対して op_col_tok を採用 (token column と一致する前提)
        #    - ただし安全のため検証し、ズレていたら近傍で op_str を探索して補正する
        op_col_post = None
        if post_line and op_col_tok is not None:
            # op_col_tok が指す位置に op_str がいるか軽く確認（1-based）
            i0 = op_col_tok - 1
            if 0 <= i0 < len(post_line) and post_line[i0:i0 + len(op_str)] == op_str:
                op_col_post = op_col_tok
            else:
                # 近傍で op_str を探す（最寄りを採用）
                op_col_post = self._find_token_col_in_src(post_line, op_str, near_col_1based=op_col_tok)

        # ここで要件: 1248行目の op_col_tok は「展開後の演算子位置に再計算」された値にする
        op_col_tok = op_col_post
        self._dbg("op_col_tok(recomputed_on_post_line):", op_col_tok)

        # 4) 展開後文字列（post_line）から対象1個分を切り出して eval_spelling にする
        if post_line and op_col_tok is not None:
            raw_target_expr_post = self._extract_expr_around_operator(post_line, op_col_tok).strip()
            eval_spelling = self._c_normalize_min(raw_target_expr_post)
        else:
            # フォールバック: 最低限 tokens 文字列から '+' の位置を検索して切る
            expr_toks = self._safe_tokenize(cursor)
            tokens_expr = self._tokens_to_c_expr(expr_toks).strip()
            k = tokens_expr.find(op_str) if (tokens_expr and op_str) else -1
            if k >= 0:
                raw_target_expr_post = self._extract_expr_around_operator(tokens_expr, k + 1).strip()
                eval_spelling = self._c_normalize_min(raw_target_expr_post)
            else:
                eval_spelling = ""

        self._dbg("eval_spelling(post,norm):", eval_spelling)

        # ------------------------------------------------------------------
        # 以降: lhs/rhs・kind 判定など（表示用は元ソース基準のまま）
        # ------------------------------------------------------------------
        lhs_pre, rhs_pre = self._extract_lhs_rhs_from_extend(eval_spelling_extend, op_str)
        self._dbg(" lhs_pre(extend/pre):", lhs_pre)
        self._dbg(" rhs_pre(extend/pre):", rhs_pre)

        lhs_post, rhs_post = self._extract_lhs_rhs_from_extend(eval_spelling, op_str)
        self._dbg(" lhs_post(eval/post):", lhs_post)
        self._dbg(" rhs_post(eval/post):", rhs_post)

        left_val_spelling = lhs_pre.strip()
        right_val_spelling = rhs_pre.strip()

        left_val_kind = "val"
        left_kind_op = None
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", left_val_spelling):
            mv2 = self._lookup_object_macro_value(left_val_spelling, macroTable)
            if mv2 is not None:
                left_val_kind = "macro"
                try:
                    left_kind_op = int(mv2, 0) if mv2 != "" else None
                except Exception:
                    left_kind_op = mv2

        right_val_kind = "val"
        right_kind_op = None
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", right_val_spelling):
            mv2 = self._lookup_object_macro_value(right_val_spelling, macroTable)
            if mv2 is not None:
                right_val_kind = "macro"
                try:
                    right_kind_op = int(mv2, 0) if mv2 != "" else None
                except Exception:
                    right_kind_op = mv2

        left_col = self._find_token_col_in_src(pre_line, left_val_spelling, near_col_1based=operator_col - 1) or 1
        right_col = self._find_token_col_in_src(pre_line, right_val_spelling, near_col_1based=operator_col + 1) or (operator_col + 1)

        spelling = self._replace_in_assignment_rhs(pre_line, raw_target_expr_pre, "@1")

        # 要件: chenge_spelling = eval_spelling_extend
        # spelling は「@1 を含む行テンプレ」にする
        spelling = self._replace_in_assignment_rhs(pre_line, raw_target_expr_pre, "@1")

        # chenge_spelling は eval_spelling_extend（式断片のみ）
        chenge_spelling = eval_spelling_extend

        return {
            "eval_col": operator_col,
            "eval_spelling": eval_spelling,                 # 展開後（例: 5 + compare...）
            "eval_spelling_extend": eval_spelling_extend,   # 展開前（例: EFGHIJK + compare...）
            "operator_spelling": op_str,
            "operator_cursor": cursor,
            "operator_col": operator_col,
            "left_val_spelling": left_val_spelling,
            "left_val_cursor_head": left_node,
            "left_val_kind": left_val_kind,
            "left_kind_op": left_kind_op,
            "left_col": left_col,
            "left_spel_insert_id": 1,
            "right_val_spelling": right_val_spelling,
            "right_val_cursor_head": right_node,
            "right_val_kind": right_val_kind,
            "right_kind_op": right_kind_op,
            "right_col": right_col,
            "right_spel_insert_id": 1,
            "_chenge_spelling_line": chenge_spelling,
            "_spelling_line": spelling,
        }