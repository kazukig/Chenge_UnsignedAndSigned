class MacroTable:
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