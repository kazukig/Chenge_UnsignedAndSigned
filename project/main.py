import os
import re
import sys
import subprocess
import tempfile
import shutil
import json
from clang import cindex
from Git.GitHost import GitHost
from analyzer.CodeAnalyzer import CodeAnalyzer
from fixer.SignedTypeFixer import SignedTypeFixer
from analyzer.MacroTable import MacroTable
from analyzer.TypeTable import TypeTable
from analyzer.FunctionTable import FunctionTable


class CommitManager:
    """
    main 側のコミット / JSON 出力処理を担当するクラス。
    """
    def __init__(self, repo_path='henge_UnsignedAndSigned', user_name='kazukig', user_email='mannen5656@gmail.com', token=None):
        self.repo_path = repo_path
        self.user_name = user_name
        self.user_email = user_email
        self.token = token

    def _compute_column(self, before_line: str, after_line: str) -> int:
        if before_line is None:
            return 1
        try:
            for i, (a, b) in enumerate(zip(before_line, after_line)):
                if a != b:
                    return i + 1
            # 先頭一致だが長さが違う場合は差分の次の位置
            if len(before_line) != len(after_line):
                return min(len(before_line), len(after_line)) + 1
        except Exception:
            pass
        return 1

    def makeOutputFile(self, input_path: str, output_path: str, ln: int, txt: str) -> bool:
        """
        入力パスのファイルをコピーし、ln(1始まり行番号) の行を txt に置換して出力する。

        追加仕様:
          置換対象行(lines[ln-1])の先頭にあるインデント（空白/タブ）を維持する。
          txt の先頭にも同じインデントを付けて、変更前後で先頭位置が揃うようにする。
        """
        try:
            with open(input_path, 'r', encoding='utf-8', errors='ignore') as rf:
                lines = rf.readlines()
        except Exception:
            return False

        new_lines = list(lines)

        try:
            if txt is not None and ln is not None:
                ln = int(ln)
                if ln < 1 or ln > len(new_lines):
                    return False

                # 追加: 元行の先頭インデント（空白/タブ）を抽出して txt に付与
                try:
                    before = new_lines[ln - 1]
                    indent = re.match(r'^[ \t]*', before).group(0)
                except Exception:
                    indent = ""

                # txt 側の先頭インデントは一旦落としてから、元のindentを付ける
                txt_norm = txt.lstrip(' \t')
                txt = indent + txt_norm

                if not txt.endswith("\n"):
                    txt = txt + "\n"
                new_lines[ln - 1] = txt
        except Exception:
            return False

        out_dir = os.path.dirname(output_path) or "."
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass

        try:
            with open(output_path, 'w', encoding='utf-8') as wf:
                wf.writelines(new_lines)
            return True
        except Exception:
            return False
        return False

    def perform(self, commit_flag: str, result, src_path: str, message=None):
        """
        result: fixer.solveSignedTypedConflict の戻り値想定 [id, line, ok_bool, new_line_text]
        src_path: example.c のパス
        message: commit message に使う（None ならデフォルトを生成）
        """
        flag = commit_flag

        # 成功かつコミットフラグが '1' の場合は Git commit & push
        print(result)
        if flag == "1" and isinstance(result, (list, tuple)) and len(result) >= 4 and result[2]:
            try:
                gh = GitHost(
                    repo_path=self.repo_path,
                    user_name=self.user_name,
                    user_email=self.user_email,
                    token=self.token
                )
                try:
                    with open(src_path, 'r', encoding='utf-8') as rf:
                        content = rf.read()
                except Exception as e:
                    return {"ok": False, "reason": f"read source failed: {e}"}

                changes = [{
                    "path": os.path.basename(src_path),
                    "action": "modify",
                    "content": content
                }]

                commit_msg = None
                if message is not None:
                    # message がリスト等なら文字列化して使う
                    if isinstance(message, (list, tuple, dict)):
                        commit_msg = json.dumps(message, ensure_ascii=False)
                    else:
                        commit_msg = str(message)
                else:
                    commit_msg = f"Toggle signedness at line {result[1]}"

                commit_res = gh.commitAndPush(changes, message=commit_msg)
                return commit_res
            except Exception as e:
                return {"ok": False, "reason": f"git failed: {e}"}

        # それ以外は result.json を作成（既存処理）
        try:
            file_name = os.path.basename(src_path)
            before_line = ""
            try:
                with open(src_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                ln = int(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else None
                if ln and 1 <= ln <= len(lines):
                    before_line = lines[ln - 1].rstrip('\n')
            except Exception:
                before_line = ""

            after_line = result[3] if isinstance(result, (list, tuple)) and len(result) > 3 else ""
            col = self._compute_column(before_line, after_line)

            out = {
                "修正するファイル名称": file_name,
                "指摘番号": result[0] if isinstance(result, (list, tuple)) and len(result) > 0 else None,
                "指摘位置_行番号": result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else None,
                "指摘位置_列番号": col,
                "変更前のコード": before_line,
                "変更後のコード": after_line
            }
            with open('result.json', 'w', encoding='utf-8') as jf:
                json.dump(out, jf, ensure_ascii=False, indent=2)
            return {"ok": True, "written": "result.json"}
        except Exception as e:
            return {"ok": False, "reason": f"write result.json failed: {e}"}

def toText(func_table, res, spel=""):
    """
    引数:
      (1) func_table: FunctionTable クラスの変数（getFunctionInfo(name) を持つ）
      (2) res: 197行目のresの返り値（例: [{'type':..., 'text':...}, ...]）
      (3) spel: analyzer 側で作った spelling（"if( @ )" 等。@ を含む想定）

    返り値:
      text(str)

    仕様:
      - まず従来通り out を生成する
      - spel が空でない & '@' を含む場合、spel 内の '@' を out で置換した文字列を返す
      - それ以外は out を返す
    """
    if not res or not isinstance(res, list):
        return ""
    if not isinstance(res[0], dict) or "text" not in res[0]:
        return ""

    base = (res[0].get("text") or "").strip()

    # 変更: 空白だけでなく、(){}[];,: など「関数の前後にありそうな文字」でも分割できるようにする
    # 例: "if(a+b)" -> ["if", "(", "a+b", ")"]
    # 例: "switch(func(x)){" -> ["switch", "(", "func", "(", "x", ")", ")", "{"]
    tokens = [t for t in re.split(r'(\s+|[()\[\]{};,:])', base) if t and not t.isspace()]

    add_idx = 1  # resの引数式の消費位置

    add_idx = 1  # resの引数式の消費位置
    def _extract_func_name(tok: str) -> str:
        t = tok.strip()
        t = re.sub(r'[;,]+$', '', t)

        # "(type)name" 形式
        m = re.match(r'^\([^)]*\)\s*([A-Za-z_]\w*)', t)
        if m:
            return m.group(1)

        # "name(" 形式
        m = re.match(r'^([A-Za-z_]\w*)\s*\(', t)
        if m:
            return m.group(1)

        # 単語そのもの
        if re.fullmatch(r'[A-Za-z_]\w*', t):
            return t

        return ""

    out_tokens = []
    for tok in tokens:
        fname = _extract_func_name(tok)
        if not fname:
            out_tokens.append(tok)
            continue

        try:
            info = func_table.getFunctionInfo(fname)
        except Exception:
            info = []
        # 関数表に無いならそのまま
        if not info or not isinstance(info, dict):
            out_tokens.append(tok)
            continue

        # 引数数
        try:
            argc = int(info.get("argc", 0) or 0)
        except Exception:
            argc = 0
        # 引数テキストを集める（res[1]..から消費）
        args_text = []
        for _ in range(argc):
            if add_idx >= len(res):
                break
            item = res[add_idx]
            add_idx += 1
            if not isinstance(item, dict):
                continue
            a = (item.get("text") or "").strip()
            if a:
                args_text.append(a)

        # 置換: 関数名(...)  ※必ず括弧を付ける
        call_txt = f"{fname}({', '.join(args_text)})"

        # tok が "(int)fname" や "fname(" 等を含む場合もあるので、そこだけ置換して形を崩さない
        # 置換できなければ call_txt をそのまま入れる
        try:
            pos = tok.find(fname)
            if pos >= 0:
                replaced = tok[:pos] + call_txt + tok[pos + len(fname):]
                replaced = re.sub(rf'\b{re.escape(fname)}\({re.escape(", ".join(args_text))}\)\(', call_txt, replaced)
                out_tokens.append(replaced)
            else:
                out_tokens.append(call_txt)
        except Exception:
            out_tokens.append(call_txt)

    out = " ".join(out_tokens)

    # 追加: spel の @ に out を挿入して返す
    try:
        if isinstance(spel, str) and spel and ("@" in spel):
            return spel.replace("@", out, 1)
    except Exception:
        pass

    return out

def MacroApply(pre_src_line: str, new_src_line: str, line_type_table) -> str:
    """
    MacroApply

    引数:
      (1) pre_src_line: 変換前ソースの行（マクロ名を含みうる）
      (2) new_src_line: 変換後ソースの行（マクロが定数へ展開されている想定）
      (3) line_type_table: CodeAnalyzer compile 結果 x["LineTypeTable"]
           形式: [[name, type], [name, type], ...]  または [{"変数or定数","型名"},...] 互換
           ※本関数内で name->type の辞書へ正規化して使う

    返り値:
      return_line(str):
        - new_src_line をコピーした文字列に対し、推定した「定数->マクロ」置換を適用
        - さらに、new_src_line 側のマクロ相当トークンが二項演算子の片側にある場合、
          もう片側オペランドの型に合わせるため "(x)(MACRO)" を付与する
          （x は line_type_table から推定）

    実装方針:
      1) pre/new から「識別子 or 数値リテラル」を抽出して変換用テーブルを作る
      2) 同じ位置のトークン差分からマクロ変換表 (macro -> const) を作る
         - pre==new は除外
         - new がキャスト付きならキャストを剥がして pre と同じなら除外
      3) new_src_line の数値を macro に置換して return_line を作る
      4) return_line を軽くパースし、二項演算子の片側が macro（置換で入った識別子）なら
         反対側の型に合わせたキャスト "(x)(macro)" を付与する
    """
    if not isinstance(pre_src_line, str):
        pre_src_line = "" if pre_src_line is None else str(pre_src_line)
    if not isinstance(new_src_line, str):
        new_src_line = "" if new_src_line is None else str(new_src_line)

    # --- line_type_table を name->type に正規化 ---
    name_to_type = {}
    try:
        if isinstance(line_type_table, list):
            for e in line_type_table:
                if isinstance(e, dict):
                    # {"name": "...", "type": "..."} 形式も来るかもしれないので吸収
                    n = e.get("name") or e.get("var") or e.get("text")
                    t = e.get("type")
                    if n and t:
                        name_to_type[str(n)] = str(t)
                elif isinstance(e, (list, tuple)) and len(e) >= 2:
                    name_to_type[str(e[0])] = str(e[1])
    except Exception:
        name_to_type = {}

    # --- tokenization ---
    tok_re = re.compile(
        r"""
        (?:0[xX][0-9A-Fa-f]+[uUlL]*)
        |(?:\d+[uUlL]*)
        |(?:[A-Za-z_]\w*)
        |(?:==|!=|<=|>=|\+\+|--|->|&&|\|\|)
        |(?:\S)
        """,
        re.VERBOSE,
    )

    def _tokens(s: str):
        return tok_re.findall(s or "")

    def _is_ident(t: str) -> bool:
        return re.fullmatch(r"[A-Za-z_]\w*", t or "") is not None

    def _is_number_like(t: str) -> bool:
        return re.fullmatch(r"(?:0[xX][0-9A-Fa-f]+|\d+)[uUlL]*", t or "") is not None

    # 追加: new_src_line 上で、すでに付与済みのキャスト "( int ) ( ... )" を無視するための正規化
    # 仕様:
    #   - "( int ) ( 3U * b )" のように「キャスト + 括弧式」の外側だけを剥がす
    #   - 内側の式トークン（ここでは "3U", "*", "b"）は残す
    def _normalize_new_tokens_for_table(toks):
        out = []
        i = 0
        while i < len(toks):
            # パターン: "(" TYPE ")" "(" ... ")"  を検出して外側2組の括弧を剥がす
            if toks[i] == "(":
                # 1つ目の')'を探す
                try:
                    j = toks.index(")", i + 1)
                except ValueError:
                    j = -1
                if j != -1:
                    # "(" <type...> ")" "("
                    if j + 1 < len(toks) and toks[j + 1] == "(":
                        # type 部分が「型っぽい」ならキャスト扱い（超簡易）
                        type_chunk = toks[i + 1:j]
                        type_text = "".join(type_chunk).strip()

                        # 型らしさ判定（記号や数値を含まず、識別子と空白/アンダースコア程度）
                        is_cast = bool(type_text) and re.fullmatch(r"[A-Za-z_]\w*(?:\s*\*|\s+[A-Za-z_]\w*|\s*)*", type_text) is not None

                        if is_cast:
                            # 2つ目の括弧式の終端')'を探す（ネスト1段だけ対応）
                            k = j + 1  # ここが '('
                            depth = 0
                            t = k
                            while t < len(toks):
                                if toks[t] == "(":
                                    depth += 1
                                elif toks[t] == ")":
                                    depth -= 1
                                    if depth == 0:
                                        # toks[k+1:t] が中身
                                        out.extend(toks[k + 1:t])
                                        i = t + 1
                                        break
                                t += 1
                            else:
                                # ')' が見つからない場合は通常処理に落とす
                                pass

                            if i != 0 and (i >= len(toks) or (len(out) and out[-1] == toks[i - 1])):
                                # すでに進んでいるので continue
                                continue

            out.append(toks[i])
            i += 1
        return out

    def _strip_cast_prefix_tokens(toks):
        i = 0
        n = len(toks)
        while i + 2 < n and toks[i] == "(":
            try:
                j = toks.index(")", i + 1)
            except ValueError:
                break
            i = j + 1
        return toks[i:]

    # --- 変換用テーブルの作成（要求: ["変数or定数", ...]） ---
    def _extract_table(s: str, normalize_cast_expr: bool = False):
        toks = _tokens(s)
        if normalize_cast_expr:
            toks = _normalize_new_tokens_for_table(toks)

        out = []
        i = 0
        while i < len(toks):
            t = toks[i]
            # "(type)name" を 1要素として扱う： ( ... ) IDENT の形をまとめる
            if t == "(":
                try:
                    j = toks.index(")", i + 1)
                except ValueError:
                    j = -1
                if j != -1 and j + 1 < len(toks) and (_is_ident(toks[j + 1]) or _is_number_like(toks[j + 1])):
                    casted = "".join(toks[i:j + 2])
                    out.append(casted)
                    i = j + 2
                    continue
            if _is_ident(t) or _is_number_like(t):
                out.append(t)
            i += 1
        return out

    pre_table = _extract_table(pre_src_line, normalize_cast_expr=False)
    # ★ここが変更点：new 側は既存キャスト "(int)(...)" を無視してテーブル抽出
    new_table = _extract_table(new_src_line, normalize_cast_expr=True)

    # 追加: 変換用テーブルを出力
    try:
        print("[DEBUG][MacroApply] pre_src_line table =", pre_table)
        print("[DEBUG][MacroApply] new_src_line table =", new_table)
    except Exception:
        pass

    # --- マクロ変換表（macro -> const）推定 ---
    # zip の位置合わせが崩れることがある（例: new 側に "int" が混入）ので、
    # new_table 側の「型っぽいトークン」を除去してから対応付けする。
    type_words = {"int", "signed", "unsigned", "short", "long", "char", "float", "double", "size_t", "ptrdiff_t", "bool"}
    try:
        for _, t in name_to_type.items():
            for w in re.split(r"\s+", str(t or "").strip()):
                if w:
                    type_words.add(w)
    except Exception:
        pass

    new_table2 = []
    for t in new_table:
        if _is_ident(t) and t in type_words:
            continue
        new_table2.append(t)

    macro_to_const = {}
    for a, b in zip(pre_table, new_table2):
        if a == b:
            continue

        # new 側がキャスト付き "(type)X" なら剥がして比較
        b_strip = b
        try:
            bt = _tokens(b)
            bt2 = _strip_cast_prefix_tokens(bt)
            b_strip = "".join(bt2) if bt2 else b
        except Exception:
            b_strip = b

        if b_strip == a:
            continue

        # pre が識別子、new が数値なら macro->const とみなす
        if _is_ident(a) and (_is_number_like(b_strip) or _is_number_like(b)):
            const_val = b_strip if _is_number_like(b_strip) else b
            macro_to_const.setdefault(a, const_val)

    # 追加: マクロ変換表を出力
    try:
        print("[DEBUG][MacroApply] macro_to_const =", macro_to_const)
    except Exception:
        pass

    # 逆引き（const -> macro）を「左から順」で適用する
    # 同一 const が複数 macro に対応しても、pre_table の出現順で 1個ずつ消費する。
    const_to_macros_in_order = []
    try:
        ordered_macros = []
        seen = set()
        for tok in pre_table:
            if tok in macro_to_const and tok not in seen:
                ordered_macros.append(tok)
                seen.add(tok)

        # (const, macro) を pre_table 左から順に積む
        for m in ordered_macros:
            const_to_macros_in_order.append((macro_to_const[m], m))
    except Exception:
        const_to_macros_in_order = []

    # --- new_src_line をベースに、定数をマクロへ戻す（左から順に） ---
    new_toks = _tokens(new_src_line)
    out_toks = list(new_toks)

    # ★重要: 同じ定数(例: "3U")が複数回出る場合、左から順に別 macro を当てる
    i = 0
    k = 0
    while i < len(out_toks) and k < len(const_to_macros_in_order):
        const_val, macro_name = const_to_macros_in_order[k]
        if out_toks[i] == const_val:
            out_toks[i] = macro_name
            k += 1
        i += 1

    # --- 二項演算子上の macro には (x)(MACRO) を付与する ---
    macro_names = set(macro_to_const.keys())

    bin_ops = {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>", "<", ">", "<=", ">=", "==", "!=", "&&", "||"}

    # <<, >> 再合成
    i = 0
    while i < len(out_toks):
        if i + 1 < len(out_toks) and (out_toks[i], out_toks[i + 1]) in (("<", "<"), (">", ">")):
            op = out_toks[i] + out_toks[i + 1]
            if op in bin_ops:
                out_toks[i:i + 2] = [op]
                continue
        i += 1

    def _operand_key(tok: str) -> str:
        """
        型引き用のキーに正規化:
          - "(u8)(x)" / "(int)(x)" のようなキャストを剥がして "x" を返す（単純対応）
          - それ以外はそのまま
        """
        if not isinstance(tok, str):
            return ""
        tt = tok.strip()
        # "(T)(name)" 形式を name に寄せる（ネストは浅く）
        m = re.match(r'^\(\s*[^()]+\s*\)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*$', tt)
        if m:
            return m.group(1)
        # "(T)name" 形式
        m = re.match(r'^\(\s*[^()]+\s*\)\s*([A-Za-z_]\w*)\s*$', tt)
        if m:
            return m.group(1)
        return tt

    def _pick_cast_type_for_macro(macro_name: str, other_operand_tok: str) -> str:
        """
        macro_name の展開定数の型（3U など）を、相手オペランドの型へ合わせる。
        例:
          macro_to_const["ABCDEFG"] == "3U"
          other は "b" で、b の型が "u8" なら "(u8)(ABCDEFG)" を付けたい
        """
        # 相手オペランドの型を得る
        other_key = _operand_key(other_operand_tok)
        other_type = (name_to_type.get(other_key) or "").strip()
        if not other_type:
            return ""  # 型が取れないなら何もしない

        # "unsigned int" や "u8" のような別名が来るので、最小限の正規化だけする
        # ここでは「相手の型名をそのままキャストに使う」方針にする
        return other_type

    # 二項演算子の左右に macro がある場合、その反対側の型に合わせて macro をキャスト
    i = 0
    while i < len(out_toks):
        op = out_toks[i]
        if op in bin_ops:
            li = i - 1
            ri = i + 1
            if 0 <= li < len(out_toks) and 0 <= ri < len(out_toks):
                L = out_toks[li]
                R = out_toks[ri]

                # 右が macro：左の型に合わせる
                if isinstance(R, str) and R in macro_names:
                    cast_t = _pick_cast_type_for_macro(R, L)
                    if cast_t:
                        out_toks[ri] = f"({cast_t})({R})"

                # 左が macro：右の型に合わせる
                if isinstance(L, str) and L in macro_names:
                    cast_t = _pick_cast_type_for_macro(L, R)
                    if cast_t:
                        out_toks[li] = f"({cast_t})({L})"
        i += 1

    # --- 文字列へ戻す（軽い整形） ---
    return_line = "".join(out_toks)
    return_line = re.sub(r"\s+", " ", return_line).strip()
    return_line = re.sub(r"\s*([()\[\]{};,:])\s*", r"\1", return_line)
    return_line = re.sub(r"\s*([+\-*/%<>=!&|^~])\s*", r" \1 ", return_line)
    return_line = re.sub(r"\s+", " ", return_line).strip()

    # 追加: 構造体参照の "->" を崩さない（"t - > a" を "t->a" に戻す）
    # 先に " - > " を詰めてから、残った空白も許容して詰める
    return_line = return_line.replace("- >", "->")
    return_line = re.sub(r"\s*-\s*>\s*", "->", return_line)

    return return_line

if __name__ == "__main__":
    src = "../test_kaizen/example.c"
    args = [
        "-std=c11",
        "-I./include",
        "-DDEBUG=1"
    ]

    # 最初にマクロ表と型表を作成する
    ttab = TypeTable(src_file=src, compile_args=args).make()
    # コミット/JSON 出力は CommitManager に委譲する
    mgr = CommitManager(repo_path='../test_kaizen', user_name='kazukig', user_email='mannen5656@gmail.com', token="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")
    
    #指摘表([指摘番号,行番号])
    chlist = [[0,105]]
    for coords in chlist:
        
        print("--------------------------[ Analyze Start ] --------------------------")
        #srcファイルを解析して指定行の指摘をキャスト用解析のjsonフォーマットにコンパイルする。
        analyzer = CodeAnalyzer(src_file=src, compile_args=args, check_list=coords[1])
        x = analyzer.compile()
        print("解析結果:", x)

        #exit(1)
        #[TBD] 以下でとりあえず関数テーブルを作成したが不必要なものも多い。
        ft = FunctionTable(tu=analyzer.getTu(), srcfile=src, preproc_map=analyzer.getpreprocmap())
        print(ft.make())  # これを追加（関数テーブルを構築して self.data に入れる）
        print("--------------------------[ Analyze Finish ] --------------------------")


        print("--------------------------[ Cast Start ] --------------------------")
        # SignedTypeFixer にテーブルを渡す
        fixer = SignedTypeFixer(src_file=src, compile_args=args, macro_table=None, type_table=ttab)
        # 例: 指摘番号 0, 行 157 を処理
        res = fixer.solveSignedTypedConflict(x["trees"])
        print("修正結果:", res)

        #テキスト変換
        print("--------------------------[ Cast Transform ] --------------------------")
        print("x[\"spelling\"]:", x["spelling"])
        txt = toText(ft,res,x["spelling"])
        print("変換結果:", txt)

        # before_line は src の元行、txt は toText の変換結果
        with open(src, 'r', encoding='utf-8', errors='ignore') as f:
            before_line = f.readlines()[coords[1]-1].rstrip("\n")

        txt2 = MacroApply(before_line, txt, x.get("LineTypeTable", []))
        print("MacroApply:", txt2)

        exit(1)
        print("--------------------------[ Cast Finish ] --------------------------")

        # ここで出力ファイルを生成（入力=出力で上書き）
        wrote = mgr.makeOutputFile(src, src, coords[1], txt)
        print("makeOutputFile wrote:", wrote)


        # ファイル化ができたら perform を呼び出し、res を commit message として渡す
        if wrote:
            op_res = mgr.perform("1", res, src, message=res)
        else:
            op_res = {"ok": False, "reason": "makeOutputFile failed"}

        print("CommitManager result:", op_res)