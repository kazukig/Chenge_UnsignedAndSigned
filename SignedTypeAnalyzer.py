import os
import re
import sys
import subprocess
import tempfile
from clang import cindex

class SingedTypeAnalyzer:
    def __init__(self, src_file="example.c", compile_args=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]
        self.preprocessed = None
        self.preproc_map = {}
        self.index = None
        # locate and set libclang, then create index
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

    # --- token / expr helpers ---
    def _get_token_text(self, node):
        try:
            return "".join([t.spelling for t in node.get_tokens()]).strip()
        except Exception:
            return ""

    def get_expr_type(self, node):
        try:
            if node is None:
                return ""
            if node.kind == cindex.CursorKind.INTEGER_LITERAL:
                tok = self._get_token_text(node)
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

    # --- AST walk ---
    def func_walk(self, node, func_name="<unknown>"):
        results = {}
        if node is None:
            return results
        for child in node.get_children():
            if child.kind == cindex.CursorKind.BINARY_OPERATOR:
                operands = list(child.get_children())
                if len(operands) >= 2:
                    lhs, rhs = operands[0], operands[1]
                    lhs_type = self.get_expr_type(lhs).strip()
                    rhs_type = self.get_expr_type(rhs).strip()
                    lhs_name = self.get_expr_name(lhs)
                    rhs_name = self.get_expr_name(rhs)
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
            # recurse
            child_map = self.func_walk(child, func_name)
            for ln, lst in child_map.items():
                results.setdefault(ln, []).extend(lst)
        return results

    def func_defprm(self, node):
        prmdeflist = []
        for child in node.get_children():
            if child.kind == cindex.CursorKind.PARM_DECL and child.is_definition():
                prmdeflist.append({"prm_name": child.spelling, "prm_type": child.type.sp
        return prmdeflist

    def func_calcprm(self, node):
        return 0

    # --- preprocess / mapping ---
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

    # --- main run ---
    # ...existing code...
    def run(self, change_line=None):
        """
        Run analysis and return a JSON-like list (Python list of dicts).
        If failure, return -1.
        If change_line is given (int), call self_change(run_result, change_line) after analysis.
        Each returned dict contains a "line" field with the original source line number.
        """
        results_list = []
        try:
            # preprocess and map
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
                            # build entry and append to results_list (include original line number)
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
            # after successful analysis optionally perform change
            if change_line is not None and results_list:
                try:
                    self.self_change(results_list, change_line)
                except Exception:
                    # ignore change errors but still return analysis result
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

    def self_change(self, run_result, line_x):
        """
        Modify self.src_file by inserting casts to toggle signedness for operands
        present on line `line_x` according to entries in run_result.
        Writes modified source to new.c in current working directory.
        Returns 0 on success, -1 on error.
        Behavior:
          - For each entry at the specified line, choose the RHS (B_name) operand to cast.
          - If A_type and B_type have different signedness, cast the operand that is of
            the opposite signedness to match the other (toggle signedness).
          - If types are same or operand name not found, skip.
        """
        try:
            import re
            if run_result == -1 or not isinstance(run_result, list):
                return -1
            # pick entries for given line
            entries = [e for e in run_result if e.get('line') == line_x]
            if not entries:
                # nothing to do; still output original content as new.c
                with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                    src_lines = f.readlines()
                with open('new.c', 'w', encoding='utf-8') as out:
                    out.writelines(src_lines)
                return 0

            # read original source
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                src_lines = f.readlines()

            def toggle_type(t):
                s = (t or "").replace("const", "").replace("volatile", "").strip()
                if not s:
                    return "unsigned int"
                # handle uint/int Nx types
                m = re.match(r'u?(int\d+_t)$', s)
                if m:
                    base = m.group(1)
                    if s.startswith('u'):
                        return base  # uint32_t -> int32_t
                    else:
                        return 'u' + base  # int32_t -> uint32_t
                if 'unsigned' in s:
                    # remove 'unsigned'
                    ns = s.replace('unsigned', '').strip()
                    return ns if ns else 'int'
                else:
                    return 'unsigned ' + s

            # apply modifications (may be multiple entries on same line)
            idx = line_x - 1
            if idx < 0 or idx >= len(src_lines):
                return -1
            line_text = src_lines[idx]

            for ent in entries:
                A_type = ent.get('A_type', '')
                B_type = ent.get('B_type', '')
                A_name = ent.get('A_name', '')
                B_name = ent.get('B_name', '')

                # determine signedness difference: simple heuristic
                def is_unsigned(t):
                    if not t:
                        return False
                    ts = t.lower()
                    if 'unsigned' in ts or ts.startswith('u') or 'uint' in ts:
                        return True
                    return False

                # choose operand to cast:
                # prefer casting RHS (B). If B and A differ in signedness, cast B to toggle.
                target_name = None
                new_type = None
                if B_name:
                    if is_unsigned(A_type) != is_unsigned(B_type):
                        # cast the one that differs - choose B if B differs
                        if is_unsigned(B_type):
                            new_type = toggle_type(B_type)  # unsigned -> signed
                            target_name = B_name
                        else:
                            new_type = toggle_type(B_type)  # signed -> unsigned
                            target_name = B_name
                    else:
                        # if same signedness, skip
                        target_name = None
                # fallback: try A if B not chosen
                if target_name is None and A_name:
                    if is_unsigned(A_type) != is_unsigned(B_type):
                        new_type = toggle_type(A_type)
                        target_name = A_name

                if not target_name or not new_type:
                    continue

                # insert cast before the first standalone occurrence of target_name on the line
                pattern = r'(?<![\w_])' + re.escape(target_name) + r'(?![\w_])'
                repl = '(' + new_type + ')' + target_name
                (line_text, nsub) = re.subn(pattern, repl, line_text, count=1)
                # continue to next entry; multiple entries may modify the same line

            # write out modified file
            src_lines[idx] = line_text
            with open('new.c', 'w', encoding='utf-8') as out:
                out.writelines(src_lines)
            return 0
        except Exception:
            return -1

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "example.c"
    args = ["-std=c11", "-Iinclude"]
    analyzer = SingedTypeAnalyzer(src_file=src, compile_args=args)
    x = analyzer.run()
    analyzer.self_change(x, 156)