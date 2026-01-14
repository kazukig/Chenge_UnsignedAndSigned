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

if __name__ == "__main__":
    src = "../test_kaizen/example.c"
    args = ["-std=c11", "-Iinclude"]

    # 最初にマクロ表と型表を作成する
    #mtab = MacroTable(src_file=src, compile_args=args).make()
    ttab = TypeTable(src_file=src, compile_args=args).make()

    # コミット/JSON 出力は CommitManager に委譲する
    mgr = CommitManager(repo_path='../test_kaizen', user_name='kazukig', user_email='mannen5656@gmail.com', token="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")
    
    #指摘表([指摘番号,行番号])
    chlist = [[0,103]]
    for coords in chlist:
        
        print("--------------------------[ Analyze Start ] --------------------------")
        #srcファイルを解析して指定行の指摘をキャスト用解析のjsonフォーマットにコンパイルする。
        analyzer = CodeAnalyzer(src_file=src, compile_args=args, check_list=coords[1])
        x = analyzer.compile()
        print("解析結果:", x)

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