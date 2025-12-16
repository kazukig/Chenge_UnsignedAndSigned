import os
import subprocess
from typing import List, Dict, Optional


class GitHost:
    """
    Git操作をコマンドベースで行うヘルパークラス。

    コンストラクタで以下を行う:
    - 指定リポジトリの存在確認
    - user.name / user.email の設定（引数があれば）
    - ブランチのチェックアウト（存在しなければ作成してチェックアウト）
    - （任意）HTTPS token を使った remote URL の設定（username と token を渡した場合）

    commitAndPush(changes, message=None) の引数 changes は dict のリストで構成:
      [
        {"path": "src/a.txt", "action": "add",    "content": "..."},   # 新規または上書き
        {"path": "src/b.txt", "action": "modify", "content": "..."},   # 上書き
        {"path": "src/c.txt", "action": "delete"}                      # 削除
      ]
    action は "add" | "modify" | "delete" をサポート。add と modify は content が必須。
    """
    def __init__(
        self,
        repo_path: str,
        branch: str = "main",
        remote: str = "origin",
        user_name: Optional[str] = None,
        user_email: Optional[str] = None,
        username: Optional[str] = None,
        token: Optional[str] = None,
    ):
        self.repo_path = os.path.abspath(repo_path)
        self.branch = branch
        self.remote = remote
        self._orig_remote_url = None

        if not os.path.isdir(self.repo_path):
            raise FileNotFoundError(f"Repository path not found: {self.repo_path}")

        # 基本的な git 動作確認
        try:
            self._run_git(["rev-parse", "--is-inside-work-tree"])
        except subprocess.CalledProcessError as e:
            raise RuntimeError("指定パスは git リポジトリではありません") from e

        # user config が与えられていれば設定
        if user_name:
            self._run_git(["config", "user.name", user_name])
        if user_email:
            self._run_git(["config", "user.email", user_email])

        # remote に token を埋め込む（必要なら）
        if username and token:
            try:
                cur = self._run_git(["remote", "get-url", self.remote]).strip()
                self._orig_remote_url = cur
                # only handle https remote URL type here
                if cur.startswith("https://"):
                    # embed credentials
                    # 例: https://github.com/owner/repo.git -> https://username:token@github.com/owner/repo.git
                    without_proto = cur[len("https://") :]
                    new = f"https://{username}:{token}@{without_proto}"
                    self._run_git(["remote", "set-url", self.remote, new])
                else:
                    # 非 https の場合は変更しない
                    pass
            except subprocess.CalledProcessError:
                pass

        # ブランチチェックアウト（なければ作成）
        try:
            self._run_git(["rev-parse", "--verify", self.branch])
            # branch exists locally
            self._run_git(["checkout", self.branch])
        except subprocess.CalledProcessError:
            # try to checkout from remote, else create local branch
            try:
                self._run_git(["checkout", "-b", self.branch])
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"ブランチ {self.branch} の作成/チェックアウトに失敗しました") from e

    def commitAndPush(self, changes: List[Dict], message: Optional[str] = None) -> Dict:
        """
        changes を適用して git add -> commit -> push を行う。
        戻り値: {"ok": bool, "commit": str or None, "push_output": str}
        """
        if not isinstance(changes, list):
            raise TypeError("changes must be a list of dicts")

        changed_files = []
        for c in changes:
            if not isinstance(c, dict) or "path" not in c or "action" not in c:
                raise ValueError("each change must be a dict with 'path' and 'action'")

            path = os.path.join(self.repo_path, c["path"])
            action = c["action"]
            if action in ("add", "modify"):
                content = c.get("content")
                if content is None:
                    raise ValueError(f"'{action}' requires 'content' for {c['path']}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                changed_files.append(c["path"])
            elif action == "delete":
                if os.path.exists(path):
                    os.remove(path)
                changed_files.append(c["path"])
            else:
                raise ValueError("action must be 'add', 'modify' or 'delete'")

        if not changed_files:
            return {"ok": False, "commit": None, "push_output": "no changes"}

        try:
            # git add
            self._run_git(["add", "--"] + changed_files)

            # commit
            if not message:
                message = self._make_commit_message(changed_files)
            self._run_git(["commit", "-m", message])

            # get last commit hash
            commit_hash = self._run_git(["rev-parse", "HEAD"]).strip()

            # push
            push_out = self._run_git(["push", self.remote, self.branch])

            return {"ok": True, "commit": commit_hash, "push_output": push_out}
        except subprocess.CalledProcessError as e:
            return {"ok": False, "commit": None, "push_output": getattr(e, "output", str(e))}
        finally:
            # もしコンストラクタで remote を書き換えていたら元に戻す（安全のため）
            if self._orig_remote_url:
                try:
                    self._run_git(["remote", "set-url", self.remote, self._orig_remote_url])
                    self._orig_remote_url = None
                except subprocess.CalledProcessError:
                    pass

    def _make_commit_message(self, files: List[str]) -> str:
        summary = []
        for p in files:
            summary.append(os.path.basename(p))
        return "Update: " + ", ".join(summary)

    def _run_git(self, args: List[str]) -> str:
        """repo_path をカレントにして git コマンドを実行し、標準出力を返す。例外は呼び出し元で処理。"""
        cmd = ["git"] + args
        res = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True)
        if res.returncode != 0:
            # include stderr for debugging
            err = res.stderr.strip()
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout + ("\n" + err if err else ""))
        return res.stdout