import os
import re
import sys
import subprocess
import tempfile
from clang import cindex
from GitHost import GitHost

class SingedTypeAnalyzer:
    def __init__(self, src_file="example.c", compile_args=None):
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
# ...existing code...

    # 古い self_change は削除しました（別クラスとして実装します）


# 新しいクラス: 署名付き/非署名の衝突を解決するための修正器
class SignedTypeFixer:
    def __init__(self, src_file="example.c", compile_args=None, macro_table=None, type_table=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]
        # マクロ表と型表を private に保持
        self._macro_table = macro_table or []
        self._type_table = type_table or []

    def solveSignedTypedConflict(self, run_result, line_pair):
        """
        変更:
        - line_pair は [指摘番号, 行番号] のみ（単一）。
        - 戻り値は [指摘番号, 行番号, 成功フラグ, 修正後の行または None]
        - new.c の作成・コンパイルは行わない。
        - ファイル更新はここで行うが、Git の commit/push は行わない（呼び出し側で行う）。
        """
        try:
            # 基本バリデーション
            if run_result == -1 or not isinstance(run_result, list):
                return [line_pair[0], line_pair[1], False, None]

            if not isinstance(line_pair, (list, tuple)) or len(line_pair) < 2:
                return [None, None, False, None]
            idx_id, ln = line_pair[0], int(line_pair[1])

            # 元ソースを読み込む
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                original_lines = f.readlines()

            if ln <= 0 or ln > len(original_lines):
                return [idx_id, ln, False, None]

            # ルックアップ用: 行番号 -> エントリのリスト
            line_map = {}
            for e in run_result:
                try:
                    lno = int(e.get('line', -1))
                except Exception:
                    lno = -1
                if lno >= 0:
                    line_map.setdefault(lno, []).append(e)

            entries = line_map.get(ln, [])
            if not entries:
                return [idx_id, ln, False, None]

            # ワーキングコピー
            working_lines = original_lines[:]
            line_idx = ln - 1
            original_line_text = working_lines[line_idx]
            new_line_text = original_line_text

            # 各エントリに対して逐次的に修正
            for ent in entries:
                A_type = ent.get('A_type', '')
                B_type = ent.get('B_type', '')
                A_name = ent.get('A_name', '')
                B_name = ent.get('B_name', '')

                resolved_A = self._resolve_type(A_type)
                resolved_B = self._resolve_type(B_type)

                target_name = None
                new_type = None

                if B_name:
                    if self._is_unsigned(resolved_A) != self._is_unsigned(resolved_B):
                        new_type = self._toggle_type(resolved_B)
                        target_name = B_name
                if not target_name and A_name:
                    if self._is_unsigned(resolved_A) != self._is_unsigned(resolved_B):
                        new_type = self._toggle_type(resolved_A)
                        target_name = A_name

                if not target_name or not new_type:
                    continue

                if self._is_integer_literal_token(target_name):
                    new_line_text, did = self._replace_literal_with_toggled(new_line_text, target_name, new_type)
                else:
                    new_line_text, did = self._replace_var_with_cast(new_line_text, target_name, new_type)

            # 変更がなければ失敗
            if new_line_text == original_line_text:
                return [idx_id, ln, False, None]

            # ファイル更新（example.c のみ） — Git は呼び出し側で行う
            working_lines[line_idx] = new_line_text
            try:
                with open(self.src_file, 'w', encoding='utf-8') as outf:
                    outf.writelines(working_lines)
            except Exception:
                # 書き込みに失敗したら元に戻して失敗を返す
                try:
                    with open(self.src_file, 'w', encoding='utf-8') as outf:
                        outf.writelines(original_lines)
                except Exception:
                    pass
                return [idx_id, ln, False, None]

            # 成功（Git commit/push は呼び出し側で行う）
            return [idx_id, ln, True, new_line_text]
        except Exception:
            return [line_pair[0] if isinstance(line_pair, (list, tuple)) and len(line_pair) > 0 else None,
                    line_pair[1] if isinstance(line_pair, (list, tuple)) and len(line_pair) > 1 else None,
                    False, None]
# ...existing code...
class McroTable:
    """
    マクロ展開表（現時点では作成するが使用は保留）
    返却形式: [ [マクロ名, 実際の値, [関連マクロ], [使用ファイル名, [使用行番号,...]] ], ... ]
    コンストラクタ引数は (src_file=..., compile_args=...) 固定。
    make() を呼ぶと上記リストを返す。
    """
    def __init__(self, src_file="example.c", compile_args=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]

    def make(self):
        # ソースを直接解析して簡易的なマクロ表を作成する
        macros = {}      # name -> raw value
        defines_lines = {}  # name -> line_no
        try:
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    m = re.match(r'^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$', line)
                    if m:
                        name = m.group(1)
                        val = m.group(2).strip()
                        macros[name] = val
                        defines_lines[name] = i
        except Exception:
            return []

        # 関連マクロを抽出し、実際の値を再帰展開（簡易）
        resolved = {}
        related = {}

        token_re = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
        def resolve(name, seen=None, depth=0):
            if name in resolved:
                return resolved[name]
            if seen is None:
                seen = set()
            if depth > 50:
                return macros.get(name, "")
            if name not in macros:
                return ""
            val = macros[name]
            seen.add(name)
            rels = set()
            def repl(tok):
                if tok in macros and tok not in seen:
                    rels.add(tok)
                    return resolve(tok, seen.copy(), depth+1)
                return tok
            # すべてのトークンについて潜在的に置換 (簡易)
            new_val = token_re.sub(lambda mo: repl(mo.group(1)), val)
            resolved[name] = new_val
            related[name] = sorted(list(rels))
            return new_val

        for n in list(macros.keys()):
            resolve(n)

        # 使用箇所を探す（ソース内での出現）
        usages = {}
        try:
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    for name in macros.keys():
                        if re.search(r'(?<![\w_])' + re.escape(name) + r'(?![\w_])', line):
                            usages.setdefault(name, []).append(i)
        except Exception:
            pass

        table = []
        for name in macros.keys():
            actual = resolved.get(name, macros.get(name, ""))
            rels = related.get(name, [])
            file_and_lines = [self.src_file, usages.get(name, [])]
            table.append([name, actual, rels, file_and_lines])
        return table

# 追加: typedef テーブル（型別名テーブル）
class TypeTable:
    """
    型変換（typedef）テーブル
    返却形式: [ [型名, 実際の型, [関連型], [使用ファイル名, [使用行番号,...]] ], ... ]
    コンストラクタ引数は (src_file=..., compile_args=...) 固定。
    make() を呼ぶと上記リストを返す。
    typedef の入れ子（別名→別名）も解決する。
    """
    def __init__(self, src_file="example.c", compile_args=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]

    def make(self):
        typedefs = {}    # alias -> base textual
        def_lines = {}   # alias -> def line
        try:
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    # 単純系 typedef: "typedef <base> <alias>;" にマッチ
                    m = re.match(r'^\s*typedef\s+(.*?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*;\s*$', line)
                    if m:
                        base = m.group(1).strip()
                        alias = m.group(2).strip()
                        typedefs[alias] = base
                        def_lines[alias] = i
                    else:
                        # struct/enum typedef なども単純に拾う（例: typedef struct X Y;）
                        m2 = re.match(r'^\s*typedef\s+(struct|enum)\b(.*)\b([A-Za-z_][A-Za_z0-9_]*)\s*;\s*$', line)
                        if m2:
                            alias = m2.group(3).strip()
                            base = ' '.join([m2.group(1), m2.group(2).strip()]).strip()
                            typedefs[alias] = base
                            def_lines[alias] = i
        except Exception:
            pass

        # 基本的な整数型を自分自身に紐付けておく（見つからない場合にも対応）
        for t in ("int8_t","int16_t","int32_t","int64_t","uint8_t","uint16_t","uint32_t","uint64_t","int","unsigned int","unsigned"):
            if t not in typedefs:
                typedefs[t] = t
                # def_lines は空にしておく

        # 依存解決: 別名→実体 を再帰的に解決
        resolved = {}
        related = {}

        name_token = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
        def resolve_type(name, seen=None, depth=0):
            # name は textual 型表記または alias
            if depth > 50:
                return name
            # もし name が alias そのものなら解決開始
            if name in resolved:
                return resolved[name]
            if seen is None:
                seen = set()
            # 直接 alias
            if name in typedefs and typedefs[name] != name:
                base = typedefs[name]
                seen.add(name)
                rels = set()
                def repl(tok):
                    if tok in typedefs and tok not in seen:
                        rels.add(tok)
                        return resolve_type(tok, seen.copy(), depth+1)
                    return tok
                new_base = name_token.sub(lambda mo: repl(mo.group(1)), base)
                resolved[name] = new_base
                related[name] = sorted(list(rels))
                return new_base
            # 文字列として解析して既知の alias が含まれる場合、それらを展開
            s = name
            rels = set()
            def repl2(tok):
                if tok in typedefs and tok not in seen:
                    rels.add(tok)
                    return resolve_type(tok, seen.copy(), depth+1)
                return tok
            new_s = name_token.sub(lambda mo: repl2(mo.group(1)), s)
            return new_s

        for alias in list(typedefs.keys()):
            resolve_type(alias)

        # 使用箇所をソースから収集
        usages = {}
        try:
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    for alias in typedefs.keys():
                        if re.search(r'(?<![\w_])' + re.escape(alias) + r'(?![\w_])', line):
                            usages.setdefault(alias, []).append(i)
        except Exception:
            pass

        table = []
        for alias in typedefs.keys():
            actual = resolved.get(alias, typedefs.get(alias, alias))
            rels = related.get(alias, [])
            file_and_lines = [self.src_file, usages.get(alias, [])]
            table.append([alias, actual, rels, file_and_lines])
        return table


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "example.c"
    args = ["-std=c11", "-Iinclude"]

    # 最初にマクロ表と型表を作成する
    mtab = McroTable(src_file=src, compile_args=args).make()
    ttab = TypeTable(src_file=src, compile_args=args).make()

    analyzer = SingedTypeAnalyzer(src_file=src, compile_args=args)
    x = analyzer.run()
    # SignedTypeFixer にテーブルを渡す
    fixer = SignedTypeFixer(src_file=src, compile_args=args, macro_table=mtab, type_table=ttab)
    # 例: 指摘番号 0, 行 157 を処理
    res = fixer.solveSignedTypedConflict(x, [0,160])
    print("修正結果:", res)

    # commit/push は main 側で行う（example.c のみ）
    try:
        if isinstance(res, (list, tuple)) and len(res) >= 3 and res[2]:
            gh = GitHost(
                repo_path='henge_UnsignedAndSigned',
                user_name='kazukig',
                user_email='mannen5656@gmail.com'
            )
            # example.c の最新内容を読み込んでコミット
            try:
                with open(src, 'r', encoding='utf-8') as rf:
                    content = rf.read()
            except Exception:
                content = None

            if content is not None:
                changes = [{
                    "path": "example.c",
                    "action": "modify",
                    "content": content
                }]
                commit_res = gh.commitAndPush(changes, message=f"Toggle signedness at line {res[1]}")
                print("Git push result:", commit_res)
    except Exception as e:
        print("Git commit/push failed:", e)