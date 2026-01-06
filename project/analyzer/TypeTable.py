import re

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
                    m = re.match(r'^\s*typedef\s+(.*?)\s+([A-Za-z_][A-ZaZ0-9_]*)\s*;\s*$', line)
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
        for t in ("int8_t","int16_t","int32_t","int64_t",
                  "uint8_t","uint16_t","uint32_t","uint64_t",
                  "int","unsigned int","unsigned",
                  "float","double","bool","char","signed char","unsigned char"):
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